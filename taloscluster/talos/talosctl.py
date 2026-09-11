"""Thin subprocess wrapper around talosctl.

talosctl has no stable python client, and it already IS a project dependency, so
we shell out -- but with structured args and parsed output instead of the shell
script's `awk`/text scraping. Pure/local generation commands (gen secrets, gen
config) always run; cluster-mutating commands honour --dry-run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..errors import ReconcileError
from ..output import action, dry_run, info, warn

BIN = "talosctl"


def _run(args: list[str], capture: bool = False, quiet_stderr: bool = False) -> str:
    proc = subprocess.run(
        [BIN, *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.DEVNULL if quiet_stderr else None,
    )
    return proc.stdout or "" if capture else ""


def _run_nocheck(args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    """Run without raising; return (returncode, stdout, stderr)."""
    proc = subprocess.run([BIN, *args], text=True, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ---- local generation (always runs; no side effects on the cluster) -------

def gen_secrets(talos_version: str) -> str:
    """Return a fresh secrets bundle (the content of `talosctl gen secrets`)."""
    return _run(
        ["gen", "secrets", "--talos-version", talos_version, "-o", "-"],
        capture=True, quiet_stderr=True,
    )


def gen_config(
    *,
    cluster: str,
    endpoint: str,
    secrets_path: Path,
    output_type: str,          # "controlplane" | "worker"
    install_image: str,
    install_disk: str,
    kubernetes_version: str,
    talos_version: str,
    patches: list[Path],
) -> str:
    """Generate a single node's machine config to stdout, with the patch files
    stacked in order (later patches win)."""
    args = [
        "gen", "config", cluster, endpoint,
        "--with-secrets", str(secrets_path),
        "--output-types", output_type,
        "--output", "-",
        "--install-image", install_image,
        "--install-disk", install_disk,
        "--kubernetes-version", kubernetes_version.lstrip("v"),
        "--talos-version", talos_version,
        "--with-docs=false",
        "--with-examples=false",
    ]
    for p in patches:
        args += ["--config-patch", f"@{p}"]
    return _run(args, capture=True, quiet_stderr=True)


def gen_talosconfig(cluster: str, endpoint: str, secrets_path: Path,
                    client_endpoint: str | None = None) -> str:
    """The client talosconfig (CA + context), the heir of terraform's
    talos_client_configuration.

    taloscluster always passes -e/-n explicitly, so only the CA/context matter to
    it. `client_endpoint` (controlplane-01's tailscale name) is baked in as the
    context endpoint so a `talosctl ...` typed by hand needs no -e.

    No default node is set: -n stays mandatory, so a destructive command like
    `talosctl reset` errors out for want of a target instead of silently
    picking controlplane-01.

    Exactly ONE endpoint, deliberately. talosctl v1.13 fails every resource-API
    call (`get`, and the dashboard's panes) with "name resolver error: produced
    zero addresses" as soon as the context lists two or more endpoints -- by
    name or by ip. Machine-API calls like `version` do fail over across a list,
    but that is not worth breaking `talosctl get` for.
    """
    out = _run(
        [
            "gen", "config", cluster, f"https://{endpoint}:6443",
            "--with-secrets", str(secrets_path),
            "--output-types", "talosconfig",
            "--output", "-",
        ],
        capture=True, quiet_stderr=True,
    )
    if not client_endpoint:
        return out
    doc = yaml.safe_load(out)
    ctx = doc.get("contexts", {}).get(doc.get("context", cluster))
    if ctx is None:  # unexpected shape; leave the generated file untouched
        warn(f"talosconfig has no context {cluster!r}; leaving endpoints unset")
        return out
    ctx["endpoints"] = [client_endpoint]
    ctx.pop("nodes", None)
    return yaml.safe_dump(doc, sort_keys=False)


# ---- cluster-mutating / query commands ------------------------------------

def _talos(talosconfig: Path, endpoint: str, node: str, *cmd: str) -> list[str]:
    return ["--talosconfig", str(talosconfig), "-e", endpoint, "-n", node, *cmd]


def reachable(talosconfig: Path, endpoint: str, node: str) -> bool:
    """True if the node's talos apid answers (used to wait for a fresh node to
    join the tailnet before bootstrap)."""
    rc, _, _ = _run_nocheck(_talos(talosconfig, endpoint, node, "version"))
    return rc == 0


@dataclass(frozen=True)
class Member:
    """One entry of talos cluster discovery (`get members`)."""

    address: str
    version: str      # talos version the member reports, e.g. "v1.13.9" ("" if odd)


def _member_version(operating_system: str) -> str:
    """"Talos (v1.13.9)" -> "v1.13.9" (the shape `get members` reports)."""
    if "(" in operating_system and operating_system.endswith(")"):
        return operating_system[operating_system.index("(") + 1:-1].strip()
    return ""


def members(
    talosconfig: Path, endpoint: str, exclude_vip: str | Iterable[str] = ""
) -> dict[str, Member]:
    """hostname -> Member, from talos cluster discovery (`get members`).

    Covers nodes that booted and joined the talos cluster but never became
    kubernetes nodes, which is why this beats asking kubectl. Each member
    reports several addresses; we prefer its tailscale (100.64/10) one because
    it is unique per node -- among the private ips, controlplane-01 also
    carries the shared kube-api VIP, which would target the wrong node. Without
    tailscale, pass the VIP (or every VIP the cluster may still carry, during
    an endpoint move) as ``exclude_vip`` so the owner's next (real) address is
    used instead of the floating one.

    Discovery also reports each member's talos version, so ONE call answers
    "which nodes exist, where, and on what version" -- no per-node
    `talosctl version` fan-out, which would need every node's apid to answer.

    Returns {} if discovery itself is unreachable (a cluster that has never
    bootstrapped), leaving the caller to fall back to OpenStack's private ips.
    """
    rc, out, _ = _run_nocheck(
        _talos(talosconfig, endpoint, endpoint, "get", "members", "-o", "json")
    )
    if rc != 0:
        return {}
    excluded = {exclude_vip} if isinstance(exclude_vip, str) else set(exclude_vip)
    found: dict[str, Member] = {}
    decoder = json.JSONDecoder()
    idx, n = 0, len(out)
    while idx < n:
        while idx < n and out[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(out, idx)  # `-o json` is a stream, not an array
        host = (obj.get("metadata") or {}).get("id") or ""
        spec = obj.get("spec") or {}
        addrs = spec.get("addresses") or []
        if not host or not addrs:
            continue
        tailscale = [a for a in addrs if a.startswith("100.64.")]
        stable = [a for a in addrs if a not in excluded]
        found[host] = Member(
            address=tailscale[0] if tailscale else (stable[0] if stable else addrs[0]),
            version=_member_version(str(spec.get("operatingSystem") or "")),
        )
    return found


def member_addresses(
    talosconfig: Path, endpoint: str, exclude_vip: str | Iterable[str] = ""
) -> dict[str, str]:
    """hostname -> address only, for callers that do not care about versions."""
    return {
        host: m.address
        for host, m in members(talosconfig, endpoint, exclude_vip=exclude_vip).items()
    }


def dashboard(talosconfig: Path, endpoint: str, nodes: list[str]) -> None:
    """Replace this process with `talosctl dashboard` (it owns the terminal)."""
    args = _talos(talosconfig, endpoint, ",".join(nodes), "dashboard")
    action(f"talosctl dashboard -n {','.join(nodes)}")
    if dry_run():
        return
    os.execvp(BIN, [BIN, *args])


def apply_config(talosconfig: Path, endpoint: str, node: str, config: str,
                 mode: str = "auto") -> None:
    """Push a machine config to an existing node.

    Without this, editing anything that lives in the machine config (extra
    manifests, kubelet args, network) only affected NEW nodes -- a running
    cluster never picked the change up, so cluster.yaml and reality drifted
    apart silently.

    mode=auto lets talos decide: config changes it can apply live are applied
    live, and only ones that genuinely need a restart reboot the node. Applying
    an unchanged config is a no-op, which keeps converge idempotent.

    Under `plan` this still talks to the node, with `--dry-run`: talos then
    reports how the change would be applied and prints the config diff without
    changing anything, so `plan` shows what `converge` would push.
    """
    action(f"talosctl apply-config {node} (mode={mode})")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(config)
        path = fh.name
    try:
        rc, out, err = _run_nocheck(
            _talos(talosconfig, endpoint, node, "apply-config",
                   "--mode", mode, "--file", path,
                   *(["--dry-run"] if dry_run() else []))
        )
    finally:
        os.unlink(path)
    if rc != 0:
        if dry_run():
            warn(f"could not diff machine config on {node}: {(err or out).strip()}")
            return
        raise RuntimeError(f"apply-config on {node} failed: {(err or out).strip()}")
    if dry_run():
        # talosctl writes the summary and diff to stderr
        for line in _dry_run_summary(out + "\n" + err):
            info(f"    {line}")


def _dry_run_summary(out: str) -> list[str]:
    """The lines worth showing from `talosctl apply-config --dry-run`.

    talos prints a "Dry run summary:" header, how the change would be applied,
    and either "No changes." or "Config diff:" followed by a unified diff.
    """
    lines = [line.rstrip() for line in out.splitlines()]
    lines = [line for line in lines if line and line != "Dry run summary:"]
    if any(line.startswith("No changes") for line in lines):
        return ["no changes"]
    return [_redact(line) for line in lines]


_SECRET_KEY = re.compile(r"^([-+ ]?\s*)([A-Za-z]*(?:key|secret|token)[A-Za-z]*):\s*\S.*$", re.I)
# `- TS_AUTHKEY=...` style environment entries (extension service configs).
_SECRET_ENV = re.compile(r"^([-+ ]?\s*-\s*)([A-Za-z_]*(?:key|secret|token)[A-Za-z_]*)=\S.*$", re.I)


def _redact(line: str) -> str:
    """Hide secret values in a machine-config diff line (keys, tokens, secrets)."""
    match = _SECRET_KEY.match(line)
    if match is not None:
        return f"{match.group(1)}{match.group(2)}: <redacted>"
    match = _SECRET_ENV.match(line)
    if match is not None:
        return f"{match.group(1)}{match.group(2)}=<redacted>"
    return line


def bootstrap(talosconfig: Path, endpoint: str, node: str,
              timeout_s: int = 300, interval_s: int = 10) -> None:
    action(f"talosctl bootstrap (node {node})")
    if dry_run():
        return
    deadline = time.monotonic() + timeout_s
    while True:
        rc, out, err = _run_nocheck(_talos(talosconfig, endpoint, node, "bootstrap"))
        if rc == 0:
            return
        # etcd already bootstrapped -> treat as success so re-runs are safe. Talos
        # phrases this a few ways across versions.
        msg = (err + out).lower()
        if any(s in msg for s in ("already", "not empty", "alreadyexists")):
            return
        # apid answers before etcd is ready to take the bootstrap call
        # (FailedPrecondition "bootstrap is not available yet"); keep trying.
        if "not available yet" in msg and time.monotonic() < deadline:
            info("bootstrap not available yet, retrying...")
            time.sleep(interval_s)
            continue
        raise RuntimeError(f"bootstrap failed: {(err or out).strip()}")


def kubeconfig(talosconfig: Path, endpoint: str, node: str, out: Path) -> None:
    action(f"talosctl kubeconfig -> {out}")
    if dry_run():
        return
    _run(_talos(talosconfig, endpoint, node, "kubeconfig", "--force", str(out)))


def health(talosconfig: Path, endpoint: str, node: str, timeout: str = "10m",
           k8s_endpoint: str | None = None) -> None:
    """`talosctl health`, with the kubernetes check pointed at `k8s_endpoint`.

    health runs server-side by default (--server), i.e. ON the control plane
    node. Left alone, the node reads its kube-api endpoint from the machine
    config -- the FLOATING ip -- and dialing its own floating ip means NAT
    hairpin through the openstack router, which does not work. The check then
    hangs on "waiting for all k8s nodes to report" until it times out, even
    though the cluster is healthy and the same ip answers fine from a laptop.
    Passing the internal VIP keeps that check inside the cluster network.
    """
    if dry_run():
        return
    args = ["health", "--wait-timeout", timeout]
    if k8s_endpoint:
        args += ["--k8s-endpoint", k8s_endpoint]
    _run(_talos(talosconfig, endpoint, node, *args))


def _server_tag(out: str) -> str:
    """The Server block's Tag from `talosctl version` output ("" if absent)."""
    server = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Server:"):
            server = True
        elif server and s.startswith("Tag:"):
            return s.split(None, 1)[1].strip()
    return ""


def server_version(talosconfig: Path, endpoint: str, node: str) -> str:
    """Parse the Server Tag from `talosctl version` (structured, not awk)."""
    return _server_tag(_run(_talos(talosconfig, endpoint, node, "version"), capture=True))


def running_schematic(talosconfig: Path, endpoint: str, node: str) -> str:
    """The schematic (extension set) the node is currently RUNNING, or "".

    The Image Factory bakes a virtual `schematic` extension into Every image it
    builds, and the extension's manifest version IS that image's schematic id.
    `talosctl get extensions` lists it, so this reads the node's *running*
    extension state directly.

    That is the right thing to compare for an extension change, unlike the
    installer reference in the node's machine config: converge applies the new
    config before the upgrade phase runs, so the config's install.image already
    points at the target schematic even while the node is still running the old
    extensions -- comparing it never triggers the reinstall.
    """
    out = _run(
        _talos(talosconfig, endpoint, node, "get", "extensions", "-o", "yaml"),
        capture=True,
    )
    for doc in _resource_docs(out):
        spec = doc.get("spec") or {}
        meta = spec.get("metadata") or {}
        ident = doc.get("metadata") or {}
        if meta.get("name") == "schematic" or ident.get("id") == "schematic":
            return str(meta.get("version") or "")
    return ""


def _resource_docs(out: str) -> list[dict]:
    """The resource documents in `talosctl get ... -o yaml` output.

    talosctl interleaves a plain `node: <address>` header line with the
    `---`-separated YAML resource documents, so a bare `yaml.safe_load_all`
    cannot read it; drop the header lines and split on document markers.
    """
    docs: list[dict] = []
    for chunk in out.split("---"):
        text = "\n".join(
            line for line in chunk.splitlines()
            if not line.strip().startswith("node:")
        )
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def upgrade(talosconfig: Path, endpoint: str, node: str, image: str) -> None:
    """Trigger the upgrade and return; the caller polls for the node to come back.

    The progress watch cannot be turned off on the legacy upgrade path (which is
    what an older server falls back to): `--wait=false` is passed and ignored,
    talosctl watches anyway. That watch holds a long-lived stream open across
    the node's reboot, and apid kills it whenever the client is newer than the
    server -- exactly the case during an upgrade:

        received prior goaway: ENHANCE_YOUR_CALM, debug data: "too_many_pings"

    talosctl then exits non-zero even though the upgrade was accepted. So a
    dropped watch is downgraded to a warning and the caller polls the node's
    reported version instead (converge._wait_version), which is skew-proof.
    Any other failure -- a bad image, a rejected request -- still raises.
    """
    action(f"talosctl upgrade {node} --image {image}")
    if dry_run():
        return
    rc, out, err = _run_nocheck(
        _talos(talosconfig, endpoint, node, "upgrade", "--image", image, "--wait=false")
    )
    if rc == 0:
        return
    msg = err + out
    if "upgrade completed" in msg and "post check passed" in msg:
        warn(f"upgrade completed for {node} despite talosctl exiting non-zero")
        return
    # the upgrade is under way; only the client's view of it died
    watch_died = (
        "too_many_pings", "ENHANCE_YOUR_CALM", "error reading from server: EOF",
        "transport is closing", "connection refused",
    )
    if any(s in msg for s in watch_died):
        warn(f"upgrade progress watch dropped for {node} "
             "(client/server version skew); polling for the new version instead")
        return
    raise RuntimeError(f"upgrade of {node} failed: {msg.strip()}")


def upgrade_k8s(talosconfig: Path, endpoint: str, node: str, version: str) -> None:
    action(f"talosctl upgrade-k8s --to {version}")
    if dry_run():
        return
    _run(_talos(talosconfig, endpoint, node, "upgrade-k8s", "--to", version.lstrip("v")))


def reset(talosconfig: Path, endpoint: str, node: str,
          control_plane: bool = False) -> None:
    """Gracefully reset a node so it cleanly leaves the cluster.

    `control_plane` marks an etcd member. A failed or timed-out reset leaves a
    dead etcd member behind: the next control-plane removal loses quorum. For a
    control plane we therefore refuse (raise) so the caller aborts the scale-down
    and keeps the VM. Workers are not etcd members, so a failed worker reset is
    only a warning and the VM can be deleted.
    """
    action(f"talosctl reset --graceful {node}")
    if dry_run():
        return
    try:
        rc, out, err = _run_nocheck(
            _talos(talosconfig, endpoint, node, "reset",
                   "--graceful", "--reboot=false", "--timeout", "10m"),
            timeout=660,
        )
    except subprocess.TimeoutExpired as e:
        if control_plane:
            raise ReconcileError(
                f"graceful reset of control plane {node} timed out after 10m; "
                "refusing to delete it -- a half-reset control plane is a dead "
                "etcd member that would cost quorum on the next removal"
            ) from e
        warn(f"reset of {node} timed out after 10m; continuing with deletion")
        return
    if rc != 0:
        if control_plane:
            raise ReconcileError(
                f"graceful reset of control plane {node} failed (rc={rc}): "
                f"{(err or out).strip()}; refusing to delete it -- a half-reset "
                "control plane is a dead etcd member"
            )
        warn(f"reset of {node} failed (rc={rc}): {(err or out).strip()}; continuing with deletion")

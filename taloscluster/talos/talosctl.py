"""Thin subprocess wrapper around talosctl.

talosctl has no stable python client, and it already IS a project dependency, so
we shell out -- but with structured args and parsed output instead of the shell
script's `awk`/text scraping. Pure/local generation commands (gen secrets, gen
config) always run; cluster-mutating commands honour --dry-run.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..output import action, dry_run, warn

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


def members(talosconfig: Path, endpoint: str, exclude_vip: str = "") -> dict[str, Member]:
    """hostname -> Member, from talos cluster discovery (`get members`).

    Covers nodes that booted and joined the talos cluster but never became
    kubernetes nodes, which is why this beats asking kubectl. Each member
    reports several addresses; we prefer its tailscale (100.64/10) one because
    it is unique per node -- among the private ips, controlplane-01 also
    carries the shared kube-api VIP, which would target the wrong node. Without
    tailscale, pass the VIP as ``exclude_vip`` so the owner's next (real)
    address is used instead of the floating one.

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
        stable = [a for a in addrs if a != exclude_vip]
        found[host] = Member(
            address=tailscale[0] if tailscale else (stable[0] if stable else addrs[0]),
            version=_member_version(str(spec.get("operatingSystem") or "")),
        )
    return found


def member_addresses(
    talosconfig: Path, endpoint: str, exclude_vip: str = ""
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
    """
    action(f"talosctl apply-config {node} (mode={mode})")
    if dry_run():
        return
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(config)
        path = fh.name
    try:
        rc, out, err = _run_nocheck(
            _talos(talosconfig, endpoint, node, "apply-config",
                   "--mode", mode, "--file", path)
        )
    finally:
        os.unlink(path)
    if rc != 0:
        raise RuntimeError(f"apply-config on {node} failed: {(err or out).strip()}")


def bootstrap(talosconfig: Path, endpoint: str, node: str) -> None:
    action(f"talosctl bootstrap (node {node})")
    if dry_run():
        return
    rc, out, err = _run_nocheck(_talos(talosconfig, endpoint, node, "bootstrap"))
    if rc == 0:
        return
    # etcd already bootstrapped -> treat as success so re-runs are safe. Talos
    # phrases this a few ways across versions.
    msg = (err + out).lower()
    if any(s in msg for s in ("already", "not empty", "alreadyexists")):
        return
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


def node_image(talosconfig: Path, endpoint: str, node: str) -> str:
    """The installer image currently applied to a node (to detect schematic
    changes, so editing the extension list triggers an upgrade)."""
    out = _run(
        _talos(talosconfig, endpoint, node, "get", "machineconfig", "-o", "yaml"),
        capture=True,
    )
    # cheap extraction; converge.py compares against the resolved installer ref
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("image:") and "installer" in s:
            return s.split(":", 1)[1].strip()
    return ""


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


def reset(talosconfig: Path, endpoint: str, node: str) -> None:
    action(f"talosctl reset --graceful {node}")
    if dry_run():
        return
    try:
        rc, out, err = _run_nocheck(
            _talos(talosconfig, endpoint, node, "reset",
                   "--graceful", "--reboot=false", "--timeout", "10m"),
            timeout=660,
        )
    except subprocess.TimeoutExpired:
        warn(f"reset of {node} timed out after 10m; continuing with deletion")
        return
    if rc != 0:
        warn(f"reset of {node} failed (rc={rc}): {(err or out).strip()}; continuing with deletion")

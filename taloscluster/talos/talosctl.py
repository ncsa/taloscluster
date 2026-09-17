"""Thin subprocess wrapper around talosctl.

talosctl has no stable python client, and it already IS a project dependency, so
we shell out -- but with structured args and parsed output instead of text
scraping. Pure/local generation commands (gen secrets, gen config) always run;
cluster-mutating commands honour --dry-run.
"""

from __future__ import annotations

import ipaddress
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

# Tailscale CGNAT addresses are the full 100.64.0.0/10 (100.64.0.0-100.127.255.255),
# not just the 100.64.0.0/16 a naive `startswith("100.64.")` would catch.
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_tailscale(addr: str) -> bool:
    host = addr.split("/", 1)[0]
    try:
        return ipaddress.ip_address(host) in TAILSCALE_NET
    except ValueError:
        return False


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
    """The client talosconfig (CA + context).

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
    used instead of the floating one. When every reported address is excluded
    (a member reporting only VIPs), its address is reported as "" -- unknown --
    rather than an excluded VIP, so callers fall back to the provider inventory
    instead of addressing whichever node owns the VIP.

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
        tailscale = [a for a in addrs if _is_tailscale(a)]
        stable = [a for a in addrs if a not in excluded]
        # When every reported address was excluded (all that remain are the VIP
        # and the tailscale/preferred ones are absent), fall back to "" rather
        # than `addrs[0]`: that first address would be an excluded VIP naming
        # whichever control plane owns it, not this member. An unknown address
        # lets callers fall through to the provider inventory / network result.
        found[host] = Member(
            address=tailscale[0] if tailscale else (stable[0] if stable else ""),
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


def etcd_members(talosconfig: Path, endpoint: str) -> dict[str, str]:
    """Authoritative live etcd membership (hostname -> member id), from
    `talosctl etcd members` against a surviving real control plane.

    This is the live etcd member list and is authoritative for "which nodes are
    still etcd members", unlike `get members` (Talos discovery service data),
    which can be stale and even drops entries that report no addresses. A node
    therefore absent from discovery may still be an etcd member; callers that
    must prove a control plane left etcd query this list instead.

    Fails closed: a failed query, an unparseable reply, an empty member list, or
    a member that cannot be identified by hostname all raise ``ReconcileError``
    -- there is no authoritative evidence to trust, and a surviving control plane
    always lists itself, so an empty list is missing/ambiguous evidence, not
    proof a node left.
    """
    rc, out, err = _run_nocheck(
        _talos(talosconfig, endpoint, endpoint, "etcd", "members")
    )
    if rc != 0:
        raise ReconcileError(
            f"could not read etcd membership from control plane {endpoint} "
            f"(rc={rc}): {(err or out).strip()}; refusing to delete an addressless "
            "control plane without authoritative proof it left etcd"
        )
    found = _parse_etcd_members(out, endpoint)
    if not found:
        raise ReconcileError(
            f"etcd membership query against control plane {endpoint} returned no "
            "members; a surviving control plane always lists itself, so an empty "
            "member list is missing/ambiguous evidence -- refusing to delete an "
            "addressless control plane without authoritative proof it left etcd"
        )
    return found


def _parse_etcd_members(out: str, endpoint: str) -> dict[str, str]:
    """Parse the `talosctl etcd members` tabwriter table into hostname -> id.

    The command has no `-o`/`--output` flag; it prints a header naming the
    columns and then one row per member, each cell space-padded by tabwriter to
    the column's width. The header's `ID` and `HOSTNAME` columns locate the two
    we need, so the parse survives versions that add a leading `NODE` column
    (v1.13+) or omit it (earlier). No cell contains whitespace, so splitting a
    line on runs of whitespace recovers the cells; a member whose row does not
    reach the hostname column cannot be positively identified and raises.
    """
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in out.splitlines():
        cells = line.split()
        if not cells:
            continue
        if "HOSTNAME" in cells:
            header = cells
            continue
        rows.append(cells)
    if header is None or "ID" not in header or "HOSTNAME" not in header:
        raise ReconcileError(
            f"could not parse etcd membership from control plane {endpoint}: "
            "unrecognised `etcd members` output; refusing to delete an addressless "
            "control plane without authoritative proof it left etcd"
        )
    id_idx = header.index("ID")
    host_idx = header.index("HOSTNAME")
    found: dict[str, str] = {}
    for cells in rows:
        if len(cells) <= host_idx or not cells[host_idx]:
            raise ReconcileError(
                f"etcd membership from control plane {endpoint} contains a member "
                "without a hostname; cannot positively confirm any addressless "
                "control plane has left etcd"
            )
        found[cells[host_idx]] = cells[id_idx]
    return found


def dashboard(talosconfig: Path, endpoint: str, nodes: list[str]) -> None:
    """Replace this process with `talosctl dashboard` (it owns the terminal)."""
    args = _talos(talosconfig, endpoint, ",".join(nodes), "dashboard")
    action(f"talosctl dashboard -n {','.join(nodes)}")
    if dry_run():
        return
    os.execvp(BIN, [BIN, *args])


def apply_config(talosconfig: Path, endpoint: str, node: str, config: str,
                 mode: str = "auto") -> bool:
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

    Returns True when the apply restarts the node (a reboot is pending), False
    for a silent live/no-op apply. This is how a settle path knows whether the
    node came down for a reason, so a no-op pass on a converged cluster is never
    mistaken for a reboot that never visibly happened.
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
            return False
        raise RuntimeError(f"apply-config on {node} failed: {(err or out).strip()}")
    if dry_run():
        # talosctl writes the summary and diff to stderr
        for line in _dry_run_summary(out + "\n" + err):
            info(f"    {line}")
        return False
    # talosctl prints the chosen mode (as apid ModeDetails) on stderr: "Applied
    # configuration with a reboot" vs "without a reboot". That is the signal a
    # settle path needs to tell a node taken down for a restart apart from a
    # silent live apply.
    return _apply_requires_reboot(out + "\n" + err)


def _apply_requires_reboot(out: str) -> bool:
    """Whether `talosctl apply-config` (mode=auto) restarted the node.

    apid reports the mode it settled on in its ModeDetails line on stderr:
    "Applied configuration with a reboot" when the change needs a restart, and
    "Applied configuration without a reboot" for a silent live/no-op apply.
    Anything unrecognised is treated as live -- auto mode only restarts a node
    for a change that genuinely needs it, and a no-op pass on a converged
    cluster is the overwhelmingly common case, so an unfamiliar message must not
    be mistaken for a reboot that never happened.
    """
    text = f"\n{out}\n".lower()
    if "with a reboot" in text:
        return True
    return False


def _dry_run_summary(out: str) -> list[str]:
    """The lines worth showing from `talosctl apply-config --dry-run`.

    talos prints a "Dry run summary:" header, how the change would be applied,
    and either "No changes." or "Config diff:" followed by a unified diff.
    """
    lines = [line.rstrip() for line in out.splitlines()]
    lines = [line for line in lines if line and line != "Dry run summary:"]
    if any(line.startswith("No changes") for line in lines):
        return ["no changes"]
    return _redact(lines)


_SECRET_KEY = re.compile(
    r"^([-+ ]?\s*)([A-Za-z]*(?:key|secret|token|passwd|pwd|pass|password)[A-Za-z]*)\s*:\s*(\S.*)$",
    re.I,
)
# `- TS_AUTHKEY=...` style environment entries (extension service configs).
_SECRET_ENV = re.compile(
    r"^([-+ ]?\s*-\s*)([A-Za-z_]*(?:key|secret|token|passwd|pwd|pass|password)[A-Za-z_]*)=\S.*$",
    re.I,
)
# A `NAME=value` environment line whose NAME mentions key/secret/token/password,
# with the dash made optional: a secret-named env fragment without a leading `- `
# (a body line of a `machine.files` env file or an export-laden script) is a
# secret no matter what its value looks like.
_SECRET_ENV_NAME = re.compile(
    r"^[A-Za-z_]*(?:key|secret|token|passwd|pwd|pass|password)[A-Za-z_]*=", re.I
)
# `content:`/`contents:` fields: machine.files file data and cluster.inlineManifests
# bodies, either of which may be a `|` literal spanning many lines.
_SECRET_CONTENT = re.compile(r"^([-+ ]?\s*(?:-\s*)?)(content|contents):\s*(\S.*)?$", re.I)
# A public `KEY=value` environment entry (extension service configs). Only a
# short bare identifier before `=` counts: a base64/PEM body blob such as
# ``LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo==`` is also an unbroken run of base64
# characters ending in `=`, but its "key" is far longer than any real env var.
_PUBLIC_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,23}=")


_BLOCK_INDICATOR = ("|", ">")


def _block_literal(value: str) -> bool:
    """Whether ``value`` starts a `|`/`>` block scalar whose body follows.

    The indicator may carry an explicit indentation hint (``|2``) or a trailing
    comment (``| # apiVersion v1``); either way the scalar's body starts on the
    following lines.
    """
    value = value.lstrip(" ")
    if value[:1] not in _BLOCK_INDICATOR:
        return False
    rest = value[1:]
    return (
        not rest.strip()
        or not rest.strip("-+0123456789")
        or rest.lstrip().startswith("#")
    )


def _is_secret_value(value: str) -> bool:
    """Whether a ``key: value``/``- value`` fragment's value looks like a body.

    A mapping or array entry that cannot be tied to a public part of the machine
    config is only safe to print while its value is recognisably public — a
    path, hostname, port or plain word. A base64/PEM blob or long high-entropy
    token is a secret body fragment (a certificate, key or token), no matter
    what its key is called. Separators (``/``, ``.``, ``-``, ``:``, ``=``) break
    a value into shorter runs, so a path or hostname is never flagged by the
    10+ char run rule; but a long value that still mixes upper and lower case is
    treated as a blob regardless of separators, since base64 can legitimately
    contain ``/`` — over-redacting a mixed-case hostname is the safe direction.
    An angle-bracket placeholder (``<base64>``) is treated as secret.
    """
    v = value.strip().strip("'\"")
    if not v:
        return False
    if v.startswith("<") and v.endswith(">"):
        return True
    if len(v) < 10:
        return False
    # base64-encoded PEM (`LS0t` is the base64 of `-----`), base64 padding and
    # the `+` base64 digit are strong blob signatures.
    if v.startswith("LS0t") or v.endswith("=") or "+" in v:
        return True
    # The longest unbroken run of letters/digits. A single 10+ char run is
    # high-entropy (a token such as `deadbeefcafe1234` or `3xMP1el3ak2Fo`),
    # whatever its case; separators only split paths (`/etc/hosts`), hostnames
    # (`Worker-01.example.edu`) and hyphenated words (`registry-user`) into
    # short public terms.
    runs = list(re.finditer(r"[A-Za-z0-9]+", v))
    if runs and max(m.end() - m.start() for m in runs) >= 10:
        return True
    return any(c.isupper() for c in v) and any(c.islower() for c in v)


# The deepest column at which a genuinely public mapping key (a bare `crt:`/
# cert field, a top-level field) sits before we start value-checking it. A
# `:`-bearing line indented deeper is treated as block body: file paths,
# hostnames and plain words still pass the value check, while a base64/PEM/token
# fragment — a manifest body no matter its key — is suppressed.
_PUBLIC_KEY_DEPTH = 9


def _redact(lines: Iterable[str]) -> list[str]:
    """Hide secret values in a machine-config diff.

    Redacts the value of any key whose name mentions key/secret/token/password
    (so a registry `password:`, a `machine.files`/`inlineManifests` field, ...),
    of any `VAR=...` environment entry whose name mentions key/secret/token/
    password, and — as a whole region — the body of any block literal such a key
    or a `content:`/`contents:` field opens (machine.files data,
    cluster.inlineManifests, a multiline `password: |` credential), redacting
    every deeper-indented line until the block closes, whether the scalar is a
    `|` literal or a `>` folded block.

    A change deep inside a long block body can arrive as a unified-diff hunk
    that does not carry the `content:`/`contents:` header line at all (it sits
    far above, outside the hunk's window). Such a line cannot be classified by
    position, so any indented line — whether a `+`/`-` changed line or a
    ` ` context line — that is not a recognisably public mapping/environment/
    array entry is suppressed rather than printed verbatim.
    """
    out: list[str] = []
    block: int | None = None  # key indentation of an open block scalar
    for line in lines:
        # Diff framing lines (file/hunk headers) must not disturb an open block:
        # a long block body split across two hunks keeps redacting past the second `@@`.
        if line.startswith(("@@", "--- ", "+++ ")):
            out.append(line)
            continue
        marker = line[0] if line[:1] in ("+", "-", " ") else ""
        content = line[1:] if marker else line
        stripped = content.lstrip(" ")
        indent = len(content) - len(stripped)
        # A marker-only line (bare `+`/`-`, i.e. a blank line inside the block body)
        # is part of the open block, never closes it; anything shallower than the
        # block's key column (sibling key, top-level field) closes the block.
        if content and block is not None and indent <= block:
            block = None
        if block is not None:
            out.append(f"{marker}    <redacted>")
            continue
        match = _SECRET_KEY.match(line)
        if match is not None:
            # A multiline credential is a block scalar: once we hide the header,
            # the deeper body lines must be hidden as a region too, exactly like
            # a `content:` field.
            if _block_literal(match.group(3)):
                block = indent + (2 if stripped.startswith("- ") else 0)
            out.append(f"{match.group(1)}{match.group(2)}: <redacted>")
            continue
        match = _SECRET_ENV.match(line)
        if match is not None:
            out.append(f"{match.group(1)}{match.group(2)}=<redacted>")
            continue
        match = _SECRET_CONTENT.match(line)
        if match is not None:
            # Compare against the `content`/`contents` key's own column, not the
            # dash column: sequence items (`- content: |`) have sibling keys
            # (`op:`, `path:`) at the key column, which must close the block.
            key_col = indent + (2 if stripped.startswith("- ") else 0)
            if _block_literal(match.group(3) or ""):
                block = key_col
            out.append(f"{match.group(1)}{match.group(2)}: <redacted>")
            continue
        # A changed or context line with no key/env/array marker that is indented
        # cannot reliably be tied to a public piece of the machine config — its
        # enclosing block (a `content:` or multiline credential) is out of the
        # hunk. Treat a ` ` context line exactly like a `+`/`-` changed line and
        # print nothing rather than expose a secret body fragment. A mapping
        # (`key: value`) or array (`- value`) entry is kept only while its value
        # looks public; a secret-shaped value (base64, PEM, long token) is
        # suppressed for array entries and for mapping entries deep enough to be
        # a manifest body (shallow `crt:`/`ca:` cert fields stay public).
        if marker in ("+", "-", " ") and indent > 0 and stripped:
            after_dash = stripped[2:].lstrip() if stripped.startswith("- ") else stripped
            # environment entries (`KEY=value`) are public config, but a base64
            # body fragment — whether a long PEM blob (`LS0t...==`) or the short
            # padded tail line of one (`xk2m9pqw4v=`) — is also `X=...`-shaped,
            # its "key" drawn from the base64 alphabet rather than a real env var.
            # Keep an env entry only while its value is recognisably public;
            # suppress it when the value is a token/blob or trailing `=` padding.
            if _PUBLIC_ENV.match(after_dash) is not None:
                name, _, env_value = after_dash.partition("=")
                # An env line whose NAME mentions key/secret/token/password is a
                # secret regardless of its value's shape — a `SECRET_KEY=`/`TOKEN=`
                # env file body fragment without the leading `- ` still names a
                # secret. Otherwise keep an env entry only while its value is
                # recognisably public; suppress it when the value is a
                # token/blob or trailing `=` padding, and when a single trailing
                # `=` padding leaves an empty value but the NAME is itself
                # blob-shaped (a genuinely public empty `NAME=` env entry stays).
                if (
                    _SECRET_ENV_NAME.match(after_dash) is not None
                    or _is_secret_value(env_value)
                    or (env_value and set(env_value) <= {"="})
                    or (not env_value and _is_secret_value(name))
                ):
                    out.append(f"{marker}    <redacted>")
                    continue
                out.append(line)  # public `KEY=value` (hostname, path, word)
                continue
            if stripped.startswith("- "):
                if _is_secret_value(stripped[2:].lstrip()):
                    out.append(f"{marker}    <redacted>")
                    continue
                out.append(line)  # public array/sequence value
                continue
            if ":" in stripped:
                # Shallow mapping keys are public config (a top-level `crt:`
                # cert field); once the line is deep enough to be a manifest
                # body, gate the value on whether it looks secret.
                if (
                    indent > _PUBLIC_KEY_DEPTH
                    and _is_secret_value(stripped.partition(":")[2])
                ):
                    out.append(f"{marker}    <redacted>")
                    continue
                out.append(line)  # public mapping entry (path, word, hostname)
                continue
            out.append(f"{marker}    <redacted>")
            continue
        out.append(line)
    return out


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

"""Thin subprocess wrapper around kubectl.

Only the handful of verbs the converger needs (get nodes / drain / delete node /
version). Output is parsed as JSON, not scraped, replacing the shell script's
jsonpath + jq. `drain` has no clean SDK equivalent, so kubectl stays the tool.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..output import action, dry_run

BIN = "kubectl"

# Wall-clock bound on every kubectl subprocess (seconds). A kube-api that
# accepts TCP but never answers (e.g. a VIP owned by a half-dead control plane)
# would otherwise block kubectl indefinitely; this raises subprocess.TimeoutExpired
# so a caller can tell a hung request apart from an abrupt negative answer.
RUN_TIMEOUT = 30.0
# kubectl --request-timeout for the probe; the only call that carries the flag.
PROBE_TIMEOUT = "10s"
# drain can legitimately take up to its own --timeout=5m; give it headroom.
DRAIN_TIMEOUT = 5 * 60 + 30.0
# a server-side diff, or an apply/delete of the full manifest set against a
# remote cluster, can legitimately exceed the probe bound; give these the same
# headroom as drain so a real operation is not cut off as a false timeout.
MANIFEST_TIMEOUT = 5 * 60 + 30.0


def _kc(kubeconfig: Path) -> list[str]:
    return [BIN, "--kubeconfig", str(kubeconfig)]


def display(args: list[str]) -> str:
    """"kubectl …" with the verbose flags/values trimmed, for a message."""
    if args and args[0] == BIN:
        args = args[1:]
    rest, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("--kubeconfig", "--context"):
            skip = True
            continue
        rest.append(a)
    return "kubectl " + " ".join(rest)


def _run(
    args: list[str],
    capture: bool = False,
    check: bool = True,
    timeout: float = RUN_TIMEOUT,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a kubectl subprocess, bounded by `timeout` so a half-dead kube-api that
    accepts TCP but never answers raises subprocess.TimeoutExpired instead of
    hanging converge. `capture` pipes stdout/stderr for parsing; `input` feeds a
    manifest over stdin (used for `apply/diff/delete -f -`)."""
    return subprocess.run(
        args,
        check=check,
        timeout=timeout,
        text=True,
        input=input,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def cluster_up(kubeconfig: Path) -> bool:
    """True iff kubeconfig is present and the api answers (heir of cluster_up())."""
    if not (kubeconfig.is_file() and kubeconfig.stat().st_size > 0):
        return False
    proc = _run(_kc(kubeconfig) + ["get", "nodes", f"--request-timeout={PROBE_TIMEOUT}"],
                capture=True, check=False)
    return proc.returncode == 0


def node_names(kubeconfig: Path) -> list[str]:
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "json"], capture=True)
    data = json.loads(proc.stdout or "{}")
    return [item["metadata"]["name"] for item in data.get("items", [])]


def node_exists(kubeconfig: Path, name: str) -> bool:
    proc = _run(_kc(kubeconfig) + ["get", "node", name], capture=True, check=False)
    return proc.returncode == 0


def node_ready(kubeconfig: Path, name: str) -> bool | None:
    """True if Ready, False if explicitly NotReady, None if unknown.

    None covers both kubectl API failure and node-not-found: scale-down must
    treat None as "cannot confirm safe to delete" and abort.
    """
    proc = _run(_kc(kubeconfig) + ["get", "node", name, "-o", "json"],
                capture=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    for condition in (data.get("status") or {}).get("conditions", []):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return None


def server_version(kubeconfig: Path) -> str:
    proc = _run(_kc(kubeconfig) + ["version", "-o", "json"], capture=True, check=False)
    if proc.returncode != 0:
        return ""
    data = json.loads(proc.stdout or "{}")
    return (data.get("serverVersion") or {}).get("gitVersion", "")


def drain(kubeconfig: Path, name: str) -> None:
    action(f"kubectl drain {name}")
    if dry_run():
        return
    _run(_kc(kubeconfig) + [
        "drain", name,
        "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=5m",
    ], timeout=DRAIN_TIMEOUT)


def delete_node(kubeconfig: Path, name: str) -> None:
    action(f"kubectl delete node {name}")
    if dry_run():
        return
    _run(_kc(kubeconfig) + ["delete", "node", name])


def unschedulable(kubeconfig: Path) -> list[str]:
    """Nodes currently cordoned (`spec.unschedulable`), i.e. SchedulingDisabled."""
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "json"], capture=True, check=False)
    if proc.returncode != 0:
        return []
    data = json.loads(proc.stdout or "{}")
    return [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if (item.get("spec") or {}).get("unschedulable")
    ]


def uncordon(kubeconfig: Path, name: str) -> bool:
    """Lift a cordon. True if it worked (or there was nothing to do).

    `talosctl upgrade` cordons the node for the duration of the upgrade and
    uncordons it when it finishes -- but if the upgrade's client-side watch dies
    (the version-skew case talosctl.upgrade() downgrades to a warning) or the
    run is interrupted, the cordon is left behind. The node then stays
    SchedulingDisabled forever, and every later `talosctl health` fails on
    "some nodes are not schedulable" even though the cluster is fine.
    """
    action(f"kubectl uncordon {name}")
    if dry_run():
        return True
    proc = _run(_kc(kubeconfig) + ["uncordon", name], capture=True, check=False)
    return proc.returncode == 0


def get_nodes_wide(kubeconfig: Path) -> str:
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "wide"], capture=True, check=False)
    return proc.stdout or ""


def node_summary(kubeconfig: Path) -> list[dict]:
    """`kubectl get nodes` reduced to the fields worth reporting in status:
    name, Ready state, roles, kubelet version and the internal ip."""
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "json"], capture=True, check=False)
    if proc.returncode != 0:
        return []
    data = json.loads(proc.stdout or "{}")
    nodes = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        st = item.get("status", {})
        ready = next(
            (c["status"] for c in st.get("conditions", []) if c.get("type") == "Ready"),
            "Unknown",
        )
        roles = sorted(
            label.split("/", 1)[1]
            for label in meta.get("labels", {})
            if label.startswith("node-role.kubernetes.io/")
        )
        internal = next(
            (a["address"] for a in st.get("addresses", [])
             if a.get("type") == "InternalIP"),
            "",
        )
        nodes.append({
            "name": meta.get("name", ""),
            "ready": ready == "True",
            "roles": roles,
            "version": (st.get("nodeInfo") or {}).get("kubeletVersion", ""),
            "internal_ip": internal,
        })
    return nodes


def node_addresses(kubeconfig: Path) -> dict[str, str]:
    """Every node's InternalIP as kubelet registered it, name -> address.

    The Node keeps its addresses whatever its Ready state, so this is the only
    address source left for a node the config no longer describes and Talos
    discovery no longer reports -- a metal machine in particular belongs to no
    provider inventory, so dropping it from the config drops its static ip too.
    Best-effort: an unreachable api returns nothing rather than raising.
    """
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "json"], capture=True, check=False)
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    addresses = {}
    for item in data.get("items", []):
        name = (item.get("metadata") or {}).get("name", "")
        internal = next(
            (a["address"] for a in (item.get("status") or {}).get("addresses", [])
             if a.get("type") == "InternalIP"),
            "",
        )
        if name and internal:
            addresses[name] = internal
    return addresses

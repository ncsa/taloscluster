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


def _kc(kubeconfig: Path) -> list[str]:
    return [BIN, "--kubeconfig", str(kubeconfig)]


def _run(args: list[str], capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def cluster_up(kubeconfig: Path) -> bool:
    """True iff kubeconfig is present and the api answers (heir of cluster_up())."""
    if not (kubeconfig.is_file() and kubeconfig.stat().st_size > 0):
        return False
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "--request-timeout=10s"],
                capture=True, check=False)
    return proc.returncode == 0


def node_names(kubeconfig: Path) -> list[str]:
    proc = _run(_kc(kubeconfig) + ["get", "nodes", "-o", "json"], capture=True)
    data = json.loads(proc.stdout or "{}")
    return [item["metadata"]["name"] for item in data.get("items", [])]


def node_exists(kubeconfig: Path, name: str) -> bool:
    proc = _run(_kc(kubeconfig) + ["get", "node", name], capture=True, check=False)
    return proc.returncode == 0


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
    ])


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

"""Read the Rancher cattle-cluster-agent state off a downstream cluster.

The cluster's Rancher registration is recorded on the cluster itself: the
cattle-cluster-agent pod swaps a `cattle-credentials-*` Secret into the
cattle-system namespace whose `namespace` key holds the Rancher cluster id
(`c-xxxxx`). Both the rancher plugin and the standalone argocd plugin resolve
the same identity from here, so they must not each re-implement the parsing.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from ..errors import ReconcileError
from . import kubectl


def _unreadable(args: list[str], detail: str) -> ReconcileError:
    """Error for a Rancher identity read that failed while cattle-system exists."""
    return ReconcileError(
        f"could not read the downstream cluster's Rancher identity: "
        f"\"{kubectl.display(args)}\" failed: {detail}; since the cattle-system "
        "namespace exists this is not 'no agent', so the cluster's registration "
        "must not be guessed -- investigate the cluster's kube-api and retry"
    )


def _unknown(args: list[str], detail: str) -> ReconcileError:
    """Error for a Rancher identity read that could not even tell whether
    cattle-system exists."""
    return ReconcileError(
        f"could not tell whether the downstream cluster has a Rancher agent: "
        f"\"{kubectl.display(args)}\" failed: {detail}; this is not 'no agent', "
        "so the cluster's registration must not be guessed -- investigate the "
        "cluster's kube-api and retry"
    )


def _hung(args: list[str]) -> ReconcileError:
    """Error for a Rancher identity read the kube-api never answered."""
    return ReconcileError(
        "reading the downstream cluster's Rancher identity timed out "
        f"(\"{kubectl.display(args)}\" did not answer after "
        f"{kubectl.RUN_TIMEOUT:.0f}s); investigate the cluster's kube-api and retry"
    )


def cluster_id(kubeconfig: Path) -> str | None:
    """The Rancher cluster id (c-xxxxx) the cluster is registered as.

    Read from the cattle-cluster-agent credentials Secret in cattle-system
    (`cattle-credentials-*`, `namespace` key). Returns None only when the
    cluster has no Rancher identity to act on: cattle-system is absent, or it
    exists but carries no readable credentials Secret yet (an agent still
    installing). Every other outcome -- a kubectl call that fails while
    cattle-system exists, a hung kube-api, or an unparseable answer -- raises
    ReconcileError instead, so a caller never mistakes a failed read for
    "no agent" and re-registers a cluster that is already registered (or
    re-writes the cluster Secret without its Rancher annotation).
    """
    if not kubeconfig.is_file():
        # No kubeconfig means the cluster has never been converged: there is no
        # downstream cluster to carry an agent, which is "no agent", not a
        # failed read.
        return None
    ns_args = [kubectl.BIN, "--kubeconfig", str(kubeconfig),
               "get", "ns", "cattle-system"]
    try:
        ns = kubectl._run(ns_args, capture=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise _hung(ns_args) from e
    if ns.returncode != 0:
        if "NotFound" in (ns.stderr or ""):
            return None
        raise _unknown(ns_args, (ns.stderr or "").strip())

    args = [kubectl.BIN, "--kubeconfig", str(kubeconfig),
            "get", "secret", "-n", "cattle-system", "-o", "json"]
    try:
        proc = kubectl._run(args, capture=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise _hung(args) from e
    if proc.returncode != 0:
        raise _unreadable(args, (proc.stderr or "").strip())
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        raise _unreadable(args, "kubectl returned unparseable output") from None
    for item in doc.get("items", []):
        if item.get("metadata", {}).get("name", "").startswith("cattle-credentials"):
            ns_value = item.get("data", {}).get("namespace")
            if ns_value:
                try:
                    return base64.b64decode(ns_value).decode()
                except Exception:
                    continue
    return None

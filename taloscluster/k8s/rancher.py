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
from pathlib import Path

from . import kubectl


def cluster_id(kubeconfig: Path) -> str | None:
    """The Rancher cluster id (c-xxxxx) the cluster is registered as.

    Read from the cattle-cluster-agent credentials Secret in cattle-system
    (`cattle-credentials-*`, `namespace` key). Returns None when Rancher is not
    installed (no agent): an absent cattle-system, a failed kubectl call, or an
    unparseable response all mean there is no Rancher identity to act on.
    """
    proc = kubectl._run(
        [kubectl.BIN, "--kubeconfig", str(kubeconfig),
         "get", "secret", "-n", "cattle-system", "-o", "json"],
        capture=True, check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return None
    for item in doc.get("items", []):
        if item.get("metadata", {}).get("name", "").startswith("cattle-credentials"):
            ns = item.get("data", {}).get("namespace")
            if ns:
                try:
                    return base64.b64decode(ns).decode()
                except Exception:
                    pass
    return None

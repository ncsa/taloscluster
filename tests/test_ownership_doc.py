"""Pin the provider ownership-marker strings to what the code writes and matches.

Every marker the docs quote must match what the code actually writes and
matches on: OpenStack's ``key=value`` tags, Proxmox's bare-string tags plus
pool membership plus the pool-comment rule, and metal's lack of any marker
(the config section plus the kube Node).
"""

from __future__ import annotations

from taloscluster.naming import sdn_alias, tag_cluster, tag_managed
from taloscluster.proxmox.backend import ProxmoxBackend
from taloscluster.proxmox.inventory import owned_tags

CLUSTER = "testcluster"
POOL_ID = f"taloscluster-{CLUSTER}"
POOL_COMMENT = f"managed-by=taloscluster cluster={CLUSTER}"


def test_quoted_marker_strings_match_the_code(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example.edu:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "nodes": ["pve001"],
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
        },
        remove=("openstack",),
    )
    backend = ProxmoxBackend(cfg, client=object())
    assert backend.pool_id == POOL_ID
    assert backend.pool_comment == POOL_COMMENT
    assert owned_tags(CLUSTER, "controlplane", "gpu") == frozenset(
        {"taloscluster", f"cluster_{CLUSTER}", "role_controlplane", "pool_gpu"}
    )
    assert tag_managed() == "managed-by=taloscluster"
    assert tag_cluster(CLUSTER) == f"cluster={CLUSTER}"
    assert sdn_alias(CLUSTER) == f"managed-by taloscluster cluster {CLUSTER}"

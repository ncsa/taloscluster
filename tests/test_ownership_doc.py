"""The three provider ownership-marker schemes stay documented in one place.

`naming.py`'s module docstring and the "Ownership markers" section of
``docs/concepts/machines.md`` must each cover all three schemes -- OpenStack's
``key=value`` tags (with the legacy ``clusterctl`` acceptance), Proxmox's
bare-string tags plus pool membership plus pool-comment rule, and metal's lack
of any marker (the config section plus the kube Node) -- and every marker
string the docs quote must match what the code actually writes and matches on.
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.naming import sdn_alias, tag_cluster, tag_managed
from taloscluster.proxmox.backend import ProxmoxBackend
from taloscluster.proxmox.inventory import owned_tags

ROOT = Path(__file__).resolve().parent.parent
NAMING = (ROOT / "taloscluster" / "naming.py").read_text()
MACHINES = (ROOT / "docs" / "concepts" / "machines.md").read_text()

CLUSTER = "testcluster"
POOL_ID = f"taloscluster-{CLUSTER}"
POOL_COMMENT = f"managed-by=taloscluster cluster={CLUSTER}"


def _markers_section() -> str:
    assert "## Ownership markers" in MACHINES
    return MACHINES.split("## Ownership markers", 1)[1].split("\n## ", 1)[0]


def test_naming_docstring_documents_all_three_schemes():
    # OpenStack: key=value tags, the managed/cluster pair and the legacy value.
    assert "key=value" in NAMING
    assert "managed-by=taloscluster" in NAMING
    assert "LEGACY_MANAGED_BY" in NAMING
    # Proxmox: bare-string tags, pool membership and the pool-comment rule.
    assert "`taloscluster`" in NAMING
    assert "cluster_<name>" in NAMING
    assert "managed-by=taloscluster cluster=<name>" in NAMING
    # Metal: no marker; the config section plus the kube Node.
    assert "kube Node" in NAMING


def test_machines_page_documents_all_three_schemes():
    section = _markers_section()
    for scheme in ("- **OpenStack**", "- **Proxmox**", "- **Metal**"):
        assert scheme in section
    # OpenStack markers, including the accepted legacy value.
    assert tag_managed() in section
    assert "managed-by=clusterctl" in section
    # Proxmox markers: the bare-string tags, the pool and its comment rule.
    assert "cluster_<name>" in section
    assert "role_<role>" in section
    assert "pool_<pool>" in section
    assert "taloscluster-<name>" in section
    assert "managed-by=taloscluster cluster=<name>" in section
    assert "managed-by taloscluster cluster <name>" in section
    # Metal has no marker; ownership is the config section plus the kube Node.
    assert "no marker" in section
    assert "kube Node" in section


def test_intro_links_the_ownership_markers_section():
    intro = MACHINES.split("\n## ", 1)[0]
    assert "[Ownership markers](#ownership-markers)" in intro


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

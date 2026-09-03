"""Tests for taloscluster.naming: tag builders and deterministic resource names."""

from __future__ import annotations

import ipaddress

import pytest

from taloscluster import naming
from taloscluster.errors import ConfigError

CLUSTER = "mycluster"

# ---------------------------------------------------------------------------
# tag builders
# ---------------------------------------------------------------------------

def test_tag_managed():
    assert naming.tag_managed() == "managed-by=taloscluster"


def test_managed_tags_includes_the_pre_rename_value():
    assert naming.managed_tags() == ["managed-by=taloscluster", "managed-by=clusterctl"]


def test_tag_cluster():
    assert naming.tag_cluster(CLUSTER) == "cluster=mycluster"


def test_tag_role():
    assert naming.tag_role("controlplane") == "role=controlplane"
    assert naming.tag_role("worker") == "role=worker"


def test_tag_pool():
    assert naming.tag_pool("controlplane") == "pool=controlplane"
    assert naming.tag_pool("gpu") == "pool=gpu"


def test_base_tags():
    tags = naming.base_tags(CLUSTER)
    assert tags == ["managed-by=taloscluster", "cluster=mycluster"]


def test_node_tags_is_base_plus_role_plus_pool():
    tags = naming.node_tags(CLUSTER, "worker", "gpu")
    assert tags == [
        "managed-by=taloscluster",
        "cluster=mycluster",
        "role=worker",
        "pool=gpu",
    ]
    # base tags are a prefix
    assert tags[: len(naming.base_tags(CLUSTER))] == naming.base_tags(CLUSTER)


def test_node_tags_controlplane():
    tags = naming.node_tags(CLUSTER, "controlplane", "controlplane")
    assert "role=controlplane" in tags
    assert "pool=controlplane" in tags


# ---------------------------------------------------------------------------
# resource names
# ---------------------------------------------------------------------------

def test_network_name():
    assert naming.network_name(CLUSTER) == "mycluster-net"


def test_subnet_name():
    assert naming.subnet_name(CLUSTER) == "mycluster-subnet"


def test_router_name():
    assert naming.router_name(CLUSTER) == "mycluster-router"


def test_kubeapi_name():
    assert naming.kubeapi_name(CLUSTER) == "mycluster-kubeapi"


def test_ingress_name():
    assert naming.ingress_name(CLUSTER) == "mycluster-ingress"


def test_secgroup_name_is_cluster_name():
    """The security group is named exactly after the cluster (no suffix)."""
    assert naming.secgroup_name(CLUSTER) == "mycluster"


def test_machine_name_is_hostname():
    assert naming.machine_name("mycluster-worker-01") == "mycluster-worker-01"


def test_image_name_includes_talos_version_and_tailscale():
    assert naming.image_name("v1.8.3") == "talos-v1.8.3-tailscale"


def test_names_are_deterministic():
    """Same cluster input always yields the same names."""
    assert naming.network_name(CLUSTER) == naming.network_name(CLUSTER)
    assert naming.image_name("v1.8.3") == naming.image_name("v1.8.3")


def test_sdn_id_format_matches_proxmox_rules():
    assert naming.SDN_ID_RE.fullmatch("bob")
    assert naming.SDN_ID_RE.fullmatch("Bob1")  # uppercase is valid in Proxmox ids
    # too long, hyphenated, or digit-first names do not fit the Proxmox id format
    for bad in ("testcluster", "my-clstr", "1cluster", "a"):
        assert not naming.SDN_ID_RE.fullmatch(bad)


def test_sdn_vni_default_is_even_and_in_range():
    vni = naming.sdn_vni("testcluster")
    assert vni == naming.sdn_vni("testcluster")
    assert vni % 2 == 0
    assert 10000 <= vni + 1 <= naming.SDN_VNI_MAX


def test_sdn_alias_carries_the_cluster_name_without_equals():
    alias = naming.sdn_alias("testcluster")
    assert "testcluster" in alias
    assert "=" not in alias  # the VNet alias character set forbids '='


def test_node_address_layout():
    pools = ("gpu", "web")
    assert str(naming.sdn_gateway("10.0.0.0/24")) == "10.0.0.1"
    cp = naming.node_address(
        "10.0.0.0/24", "c-controlplane-01", "controlplane", "controlplane", pools
    )
    assert str(cp) == "10.0.0.11/24"
    first_pool = naming.node_address("10.0.0.0/24", "c-gpu-01", "worker", "gpu", pools)
    assert str(first_pool) == "10.0.0.61/24"
    second_pool = naming.node_address("10.0.0.0/24", "c-web-02", "worker", "web", pools)
    assert str(second_pool) == "10.0.0.112/24"


def test_node_address_rejects_layout_overflow():
    with pytest.raises(ConfigError, match="does not fit"):
        naming.node_address("10.0.0.0/28", "c-gpu-01", "worker", "gpu", ("gpu",))
    with pytest.raises(ConfigError, match="too many control planes"):
        naming.node_address("10.0.0.0/16", "c-controlplane-50", "controlplane", "controlplane", ())
    with pytest.raises(ConfigError, match="exceeds the static SDN address block"):
        naming.node_address("10.0.0.0/16", "c-gpu-51", "worker", "gpu", ("gpu",))


def test_sdn_reserved_spans_gateway_cp_range_and_pool_blocks():
    def addr(host: int):
        return ipaddress.ip_address(f"10.0.0.{host}")

    reserved = naming.sdn_reserved("10.0.0.0/24", ("gpu", "web"))
    # gateway, the full controlplane range, and each pool's full 50-address block
    for host in (1, 11, 59, 61, 109, 111, 159):
        assert addr(host) in reserved, host
    # free hosts: below the layout, the unused block starts, and past the last block
    for host in (2, 9, 160, 200):
        assert addr(host) not in reserved, host
    # a pool added later reserves its own block, shifting nothing
    reserved_two = naming.sdn_reserved("10.0.0.0/24", ("gpu",))
    assert addr(111) not in reserved_two

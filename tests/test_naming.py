"""Tests for taloscluster.naming: tag builders and deterministic resource names."""

from __future__ import annotations

from taloscluster import naming

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

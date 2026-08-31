"""Rendering the ArgoCD manifests off the Context.

These cover the regression that motivated the move: the ingress VIP / floating ip
and the OpenStack project used to be read by shelling out to `clusterctl status`,
which stopped existing when the tool was renamed and failed silently -- blanking
metallb's address pool and both ingress IPs in an otherwise successful apply.
"""

from __future__ import annotations

import base64
import json

import pytest
import yaml
from taloscluster.context import Context

from taloscluster_argocd import manifests
from taloscluster_argocd.config import Config, Members, Openstack

CLUSTER_STATUS = {
    "openstack": {"url": "https://cloud", "region": "RegionOne", "project": "my project"},
    "kubernetes": {"floating_ip": "1.2.3.4", "vip": "10.0.0.1",
                   "endpoint": "https://1.2.3.4:6443"},
    "ingress": {"floating_ip": "1.2.3.5", "vip": "10.0.0.2"},
}


@pytest.fixture
def kubeconfig(tmp_path):
    """A minimal kubeconfig -- the cluster Secret is built from it."""
    ca = base64.b64encode(b"ca").decode()
    cert = base64.b64encode(b"cert").decode()
    key = base64.b64encode(b"key").decode()
    (tmp_path / "kubeconfig").write_text(yaml.safe_dump({
        "clusters": [{"name": "c", "cluster": {
            "server": "https://1.2.3.4:6443", "certificate-authority-data": ca}}],
        "users": [{"name": "u", "user": {
            "client-certificate-data": cert, "client-key-data": key}}],
    }))
    return tmp_path


@pytest.fixture
def cfg():
    return Config(
        name="testcluster",
        members=Members(admins=("a@example.com",), users=()),
        git_url="https://git.example.com/repo.git",
        openstack=Openstack(project="", url="https://cloud", region="RegionOne"),
        metallb={"enabled": True},
        ingress={"enabled": True},
        nfs={"enabled": True, "taiga": True},
    )


def ctx_for(root, results=None):
    return Context(root=root, cfg=None, status=dict(CLUSTER_STATUS), results=results or {})


def test_cluster_apps_carries_the_ingress_ips(kubeconfig, cfg):
    """The clusterctl regression: these three came out empty."""
    out = manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"]
    assert "- 10.0.0.2/32" in out          # metallb address pool
    assert 'publicIP: "1.2.3.5"' in out    # ingress floating ip
    assert 'privateIP: "10.0.0.2"' in out  # ingress vip


def test_cluster_apps_carries_the_openstack_project(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["openstack"]["project"] == "my project"


def test_cluster_apps_uses_nfs_csi(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["nfs"]["type"] == "csi"


def test_cluster_apps_uses_cluster_name_for_taiga_path(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["nfs"]["servers"]["taiga"]["path"] == (
        "/taiga/ncsa/radiant/testcluster"
    )


@pytest.mark.parametrize(
    ("monitoring", "expected"), [({}, False), ({"enabled": True}, True)]
)
def test_cluster_apps_configures_monitoring(kubeconfig, cfg, monitoring, expected):
    cfg.monitoring = monitoring
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["monitoring"]["enabled"] is expected


def test_config_loads_monitoring(tmp_path):
    (tmp_path / "cluster.yaml").write_text(
        "name: testcluster\nargocd:\n  monitoring:\n    enabled: true\n"
    )
    assert Config.load(tmp_path).monitoring == {"enabled": True}


def test_cluster_secret_is_annotated_with_the_rancher_id(kubeconfig, cfg):
    """What AFTER = ("rancher",) buys: the ArgoCD entry points back at Rancher."""
    ctx = ctx_for(kubeconfig, results={"rancher": {"cluster_id": "c-abc12"}})
    doc = yaml.safe_load(manifests.render(cfg, ctx)["secret"])
    assert doc["metadata"]["annotations"] == {"rancher.cattle.io/cluster-id": "c-abc12"}


def test_cluster_secret_has_no_annotation_without_rancher(kubeconfig, cfg):
    """rancher may not be installed, or not configured -- neither is an error."""
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["secret"])
    assert "annotations" not in doc["metadata"]
    # and the secret is still valid
    assert doc["stringData"]["server"] == "https://1.2.3.4:6443"
    assert json.loads(doc["stringData"]["config"])["tlsClientConfig"]["insecure"] is False


@pytest.mark.parametrize("results, expected", [
    ({"rancher": {"cluster_id": "c-abc12"}}, "c-abc12"),
    ({}, ""),
])
def test_cluster_apps_carries_the_rancher_id(kubeconfig, cfg, results, expected):
    ctx = ctx_for(kubeconfig, results=results)
    doc = yaml.safe_load(manifests.render(cfg, ctx)["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["cluster"]["rancher"]["id"] == expected


def test_render_without_git_url_emits_only_secret_and_project(kubeconfig, cfg):
    cfg.git_url = None
    assert sorted(manifests.render(cfg, ctx_for(kubeconfig))) == ["project", "secret"]

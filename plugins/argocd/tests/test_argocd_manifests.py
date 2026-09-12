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
from taloscluster.errors import ConfigError

from taloscluster_argocd import manifests
from taloscluster_argocd.config import Config, Members, Openstack

CLUSTER_STATUS = {
    "openstack": {"url": "https://cloud", "region": "RegionOne", "project": "my project"},
    "kubernetes": {"floating_ip": "1.2.3.4", "vip": "10.0.0.1",
                   "endpoint": "https://1.2.3.4:6443"},
    "ingress": {"floating_ip": "1.2.3.5", "vip": "10.0.0.2", "metallb": ["10.0.0.2"]},
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
        infra_url="https://git.example.com/infra.git",
        openstack=Openstack(project="", url="https://cloud", region="RegionOne"),
        metallb={"enabled": True},
        ingress={"enabled": True},
        nfs={
            "enabled": True,
            "servers": {"shared": {
                "server": "nfs.example.edu", "path": "/exports/testcluster", "defaultClass": True,
            }},
        },
    )


def ctx_for(root, results=None, status=None):
    return Context(root=root, cfg=None, status=dict(CLUSTER_STATUS if status is None else status),
                   results=results or {})


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


def test_metallb_single_vip_renders_as_slash32(kubeconfig, cfg):
    """OpenStack: the single ingress VIP becomes a /32 in the MetalLB pool."""
    out = manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"]
    doc = yaml.safe_load(out)
    addresses = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])["metallb"]["addresses"]
    assert addresses == ["10.0.0.2/32"]


def test_metallb_proxmox_range_renders_verbatim(kubeconfig, cfg):
    """Proxmox: the ingress_pool range is a valid MetalLB spec, not a /32."""
    status = dict(CLUSTER_STATUS)
    status["ingress"] = {
        "floating_ip": "",
        "vip": "",
        "metallb": ["203.0.113.20-203.0.113.40"],
    }
    ctx = ctx_for(kubeconfig, status=status)
    doc = yaml.safe_load(manifests.render(cfg, ctx)["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["metallb"]["addresses"] == ["203.0.113.20-203.0.113.40"]
    # no single VIP means the ingress controller has no private/public IP
    assert values["ingresscontroller"]["privateIP"] == ""
    assert values["ingresscontroller"]["publicIP"] == ""


def test_metallb_disabled_emits_no_addresses(kubeconfig, cfg):
    cfg.metallb = {}
    out = manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"]
    doc = yaml.safe_load(out)
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["metallb"]["enabled"] is False


def test_metallb_falls_back_to_vip_when_pool_absent(kubeconfig, cfg):
    """Contexts built before the pool was added still render the single VIP."""
    status = dict(CLUSTER_STATUS)
    status["ingress"] = {"floating_ip": "1.2.3.5", "vip": "10.0.0.2"}
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig, status=status))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["metallb"]["addresses"] == ["10.0.0.2/32"]


def test_cluster_apps_uses_nfs_csi(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["nfs"]["type"] == "csi"


def test_cluster_apps_passes_nfs_servers_through(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    values = yaml.safe_load(doc["spec"]["source"]["helm"]["values"])
    assert values["nfs"]["servers"] == cfg.nfs["servers"]


def test_cluster_apps_points_at_the_infra_repo(kubeconfig, cfg):
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["cluster-apps"])
    assert doc["spec"]["source"]["repoURL"] == "https://git.example.com/infra.git"


def test_render_without_infra_url_fails(kubeconfig, cfg):
    cfg.infra_url = None
    with pytest.raises(ConfigError, match="argocd.infra.url"):
        manifests.render(cfg, ctx_for(kubeconfig))


def test_config_loads_infra_url_and_nfs_servers(tmp_path):
    (tmp_path / "cluster.yaml").write_text(
        "name: testcluster\nargocd:\n  infra:\n    url: https://git.example.com/infra.git\n"
        "  nfs:\n    enabled: true\n    servers:\n      shared:\n        server: nfs.example.edu\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.infra_url == "https://git.example.com/infra.git"
    assert cfg.nfs["servers"] == {"shared": {"server": "nfs.example.edu"}}


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


# ---------------------------------------------------------------------------
# Sync semantics: `argocd.sync` controls only the chart's Helm `sync` value;
# `argocd.automated` is a separate switch for the two parent Applications'
# automated sync policy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("automated", [True, False])
def test_parent_applications_honour_automated(kubeconfig, cfg, automated):
    cfg.automated = automated
    out = manifests.render(cfg, ctx_for(kubeconfig))
    for name in ("apps", "cluster-apps"):
        doc = yaml.safe_load(out[name])
        sync_policy = doc["spec"]["syncPolicy"]
        automated_block = sync_policy.get("automated")
        if automated:
            assert automated_block == {
                "prune": True,
                "selfHeal": True,
                "allowEmpty": False,
            }
        else:
            assert automated_block is None
        assert sync_policy.get("syncOptions") == ["CreateNamespace=true"]


@pytest.mark.parametrize("sync", [True, False])
def test_chart_sync_value_is_independent_of_automated(kubeconfig, cfg, sync):
    """`argocd.sync` drives only the Helm `sync:` value, not parent auto-sync."""
    cfg.sync = sync
    cfg.automated = True
    out = manifests.render(cfg, ctx_for(kubeconfig))
    values = yaml.safe_load(yaml.safe_load(out["cluster-apps"])["spec"]["source"]["helm"]["values"])
    assert values["sync"] is sync
    assert yaml.safe_load(out["apps"])["spec"]["syncPolicy"]["automated"]["selfHeal"] is True


def test_automated_false_still_renders_sync_false_chart_value(kubeconfig, cfg):
    """The false case for both knobs: no parent auto-sync and sync:false in values."""
    cfg.sync = False
    cfg.automated = False
    out = manifests.render(cfg, ctx_for(kubeconfig))
    for name in ("apps", "cluster-apps"):
        assert "automated" not in yaml.safe_load(out[name])["spec"]["syncPolicy"]
    values = yaml.safe_load(yaml.safe_load(out["cluster-apps"])["spec"]["source"]["helm"]["values"])
    assert values["sync"] is False


def test_config_sync_and_automated_defaults(tmp_path):
    (tmp_path / "cluster.yaml").write_text("name: testcluster\n")
    d = Config.load(tmp_path)
    assert d.sync is False
    assert d.automated is True


def test_config_loads_automated_false(tmp_path):
    (tmp_path / "cluster.yaml").write_text(
        "name: testcluster\nargocd:\n  sync: false\n  automated: false\n"
    )
    d = Config.load(tmp_path)
    assert d.sync is False
    assert d.automated is False


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


def test_project_role_names_match_their_policy_subjects(kubeconfig, cfg):
    """Each AppProject role's policy subject must reference the role it belongs to."""
    cfg.members = Members(admins=("a@example.com",), users=("d@example.com",))
    doc = yaml.safe_load(manifests.render(cfg, ctx_for(kubeconfig))["project"])
    roles = {r["name"]: r for r in doc["spec"]["roles"]}
    assert set(roles) == {"admin", "user"}
    name = cfg.name
    for role_name, role in roles.items():
        for policy in role["policies"]:
            subject = policy.split(",")[1].strip().split(":")[2]
            assert subject == role_name, policy
    # the read-only user role still only grants get
    assert roles["user"]["policies"] == [
        f"p, proj:{name}:user, applications, get, {name}/*, allow"
    ]


# ---------------------------------------------------------------------------
# Cinder: the provider credential must never land in an ArgoCD Application.
# It is delivered to the downstream cluster as a Secret instead.
# ---------------------------------------------------------------------------


def _values(doc) -> dict:
    return yaml.safe_load(doc["spec"]["source"]["helm"]["values"])


def test_openstack_credentials_never_appear_in_parent_app(kubeconfig, cfg):
    """The credential is deliberately not read into the parent Application."""
    cfg.cinder = {"enabled": True}
    out = manifests.render(cfg, ctx_for(kubeconfig), ost=("cred-id", "cred-secret"))
    doc = yaml.safe_load(out["cluster-apps"])
    values = _values(doc)
    assert "credential_id" not in values["openstack"]
    assert "credential_secret" not in values["openstack"]
    # and the secret value itself must not appear anywhere in the rendered manifest
    assert "cred-secret" not in out["cluster-apps"]


def test_cinder_secret_renders_when_enabled_with_credentials(kubeconfig, cfg):
    cfg.cinder = {"enabled": True}
    out = manifests.render(cfg, ctx_for(kubeconfig), ost=("cred-id", "cred-secret"))
    secret = yaml.safe_load(out["cinder-secret"])
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "cinder-csi-cloud-config"
    assert secret["metadata"]["namespace"] == "cinder-csi"
    conf = secret["stringData"]["cloud.conf"]
    assert "application-credential-id=cred-id" in conf
    assert "application-credential-secret=cred-secret" in conf
    assert "auth-url=" in conf


def test_cinder_namespace_renders_separately():
    """The Namespace is its own manifest so check/destroy never touch it as drift."""
    ns = yaml.safe_load(manifests.cinder_namespace())
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["name"] == "cinder-csi"


def test_no_cinder_secret_when_disabled(kubeconfig, cfg):
    cfg.cinder = {"enabled": False}
    assert "cinder-secret" not in manifests.render(cfg, ctx_for(kubeconfig), ost=("id", "secret"))


def test_enabled_cinder_without_credentials_is_a_config_error(kubeconfig, cfg):
    cfg.cinder = {"enabled": True}
    with pytest.raises(ConfigError, match="application credential"):
        manifests.render(cfg, ctx_for(kubeconfig), ost=None)

"""Common values and derived resources for the known entries."""

from __future__ import annotations

from pathlib import Path

import yaml

from taloscluster_charts.charts import (
    ceph_storage_class_values,
    cert_manager_issuers,
    common_values,
    first_pool_address,
    metallb_pool_manifest,
    namespace_manifest,
    nfs_storage_class_values,
)
from taloscluster_charts.config import Config, Namespace


def _entry(root: Path, charts: dict, name: str):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cluster.yaml").write_text(yaml.safe_dump({"name": "t", "charts": charts}))
    return Config.load(root).entries[name]


def test_first_pool_address():
    assert first_pool_address(("203.0.113.190-203.0.113.199",)) == "203.0.113.190"
    assert first_pool_address(("192.0.2.7",)) == "192.0.2.7"
    assert first_pool_address(()) == ""


def test_metallb_pool_manifest():
    docs = yaml.safe_load_all(metallb_pool_manifest(("203.0.113.190-203.0.113.199",)))
    pool, l2 = list(docs)
    assert pool["kind"] == "IPAddressPool"
    assert pool["spec"]["addresses"] == ["203.0.113.190-203.0.113.199"]
    assert pool["spec"]["autoAssign"] is True
    assert pool["metadata"]["namespace"] == "metallb-system"
    assert l2["kind"] == "L2Advertisement"
    assert l2["spec"]["ipAddressPools"] == ["external"]


def test_namespace_manifest_labels():
    doc = yaml.safe_load(namespace_manifest(Namespace("traefik", "restricted", "restricted")))
    assert doc["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/audit": "restricted",
    }


def test_traefik_common_values_classic(tmp_path):
    entry = _entry(tmp_path, {"traefik": {}}, "traefik")
    values = common_values(entry, ingress_ip="203.0.113.190", gateway_enabled=False)
    assert values["deployment"]["replicas"] == 1
    assert values["service"]["loadBalancerIP"] == "203.0.113.190"
    assert values["ports"]["web"]["http"]["redirections"]["entryPoint"]["to"] == "websecure"
    # no acme resolver of its own: cert-manager owns issuance
    assert "certificatesResolvers" not in values
    assert "tls" not in values["ports"]["websecure"]
    assert "providers" not in values
    assert "gateway" not in values


def test_traefik_common_values_gateway(tmp_path):
    entry = _entry(tmp_path, {"traefik": {}}, "traefik")
    values = common_values(entry, ingress_ip="203.0.113.190", gateway_enabled=True)
    assert values["providers"]["kubernetesGateway"]["enabled"] is True
    assert values["gateway"]["listeners"]["web"]["port"] == 8000


def test_metallb_common_values(tmp_path):
    entry = _entry(tmp_path, {"metallb": {}}, "metallb")
    assert common_values(entry) == {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }


def test_unknown_chart_gets_no_common_values(tmp_path):
    entry = _entry(tmp_path, {"mystery": {"repo": "https://example.com/charts"}}, "mystery")
    assert common_values(entry) == {}


def test_nfs_storage_class_values(tmp_path):
    raw = {"nfs": {"storageClasses": [
        {"name": "nfs-data", "server": "nfs.example.edu", "share": "/exports/data",
         "defaultClass": True},
        {"name": "nfs-scratch", "server": "nfs2.example.edu", "share": "/scratch",
         "mountOptions": ["nfsvers=4.2"], "reclaimPolicy": "Delete"},
    ]}}
    entry = _entry(tmp_path, raw, "nfs")
    values = nfs_storage_class_values(entry, "testcluster")
    first, second = values["storageClasses"]
    assert first["annotations"] == {"storageclass.kubernetes.io/is-default-class": "true"}
    assert first["parameters"]["server"] == "nfs.example.edu"
    assert first["parameters"]["subDir"] == (
        "testcluster/${pvc.metadata.namespace}-${pvc.metadata.name}-${pv.metadata.name}"
    )
    assert first["parameters"]["onDelete"] == "retain"
    assert first["reclaimPolicy"] == "Retain"
    assert first["volumeBindingMode"] == "Immediate"
    assert first["mountOptions"] == ["nfsvers=4.1"]
    assert second["parameters"]["subDir"].startswith("testcluster/")
    assert second["mountOptions"] == ["nfsvers=4.2"]
    assert second["reclaimPolicy"] == "Delete"
    assert "annotations" not in second


def test_ceph_gets_no_common_values(tmp_path):
    entry = _entry(tmp_path, {"ceph": {"enabled": False}}, "ceph")
    assert common_values(entry) == {}


def test_ceph_storage_class_values_per_chart(tmp_path):
    entry = _entry(tmp_path, {"ceph": {
        "clusterID": "fsid",
        "monitors": ["m:6789"],
        "rbd": {
            "pool": "kubernetes", "defaultClass": True,
            "parameters": {"imageFeatures": "layering"},
        },
        "fs": {
            "fsName": "cephfs", "name": "cephfs-shared", "reclaimPolicy": "Delete",
            "mountOptions": ["debug"], "annotations": {"a": "b"},
        },
    }}, "ceph")
    rbd = ceph_storage_class_values(entry, "ceph-csi-rbd")["storageClass"]
    assert rbd == {
        "create": True,
        "name": "csi-rbd-sc",
        "clusterID": "fsid",
        "reclaimPolicy": "Retain",
        "pool": "kubernetes",
        "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
        "imageFeatures": "layering",
    }
    fs = ceph_storage_class_values(entry, "ceph-csi-cephfs")["storageClass"]
    assert fs == {
        "create": True,
        "name": "cephfs-shared",
        "clusterID": "fsid",
        "reclaimPolicy": "Delete",
        "fsName": "cephfs",
        "mountOptions": ["debug"],
        "annotations": {"a": "b"},
    }
    bare = _entry(
        tmp_path, {"ceph": {"clusterID": "fsid", "monitors": ["m:6789"], "rbd": True}}, "ceph"
    )
    assert ceph_storage_class_values(bare, "ceph-csi-rbd") == {}


def test_sealed_secrets_common_values(tmp_path):
    entry = _entry(tmp_path, {"sealed-secrets": {}}, "sealed-secrets")
    assert common_values(entry) == {"fullnameOverride": "sealed-secrets-controller"}


def test_cert_manager_common_values_without_issuers(tmp_path):
    entry = _entry(tmp_path, {"cert-manager": {"email": "a@b"}}, "cert-manager")
    assert common_values(entry) == {"crds": {"enabled": True}}


def test_cert_manager_ingress_shim_prefers_prod(tmp_path):
    entry = _entry(
        tmp_path, {"cert-manager": {"email": "a@b", "prod": True, "staging": True}}, "cert-manager"
    )
    shim = common_values(entry)["ingressShim"]
    assert shim["defaultIssuerName"] == "letsencrypt-prod"
    assert shim["defaultIssuerKind"] == "ClusterIssuer"
    assert shim["defaultIssuerGroup"] == "cert-manager.io"


def test_cert_manager_ingress_shim_falls_back_to_staging(tmp_path):
    entry = _entry(tmp_path, {"cert-manager": {"email": "a@b", "staging": True}}, "cert-manager")
    assert common_values(entry)["ingressShim"]["defaultIssuerName"] == "letsencrypt-staging"


def test_cert_manager_issuers_prod_only(tmp_path):
    entry = _entry(tmp_path, {"cert-manager": {"email": "a@b", "prod": True}}, "cert-manager")
    docs = list(yaml.safe_load_all(cert_manager_issuers(entry)))
    assert len(docs) == 1
    acme = docs[0]["spec"]["acme"]
    assert docs[0]["metadata"]["name"] == "letsencrypt-prod"
    assert acme["server"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert acme["email"] == "a@b"
    assert acme["privateKeySecretRef"]["name"] == "letsencrypt-prod-account-key"
    solver = acme["solvers"][0]["http01"]["ingress"]
    assert solver["ingressClassName"] == "traefik"
    annotations = solver["ingressTemplate"]["metadata"]["annotations"]
    assert annotations["traefik.ingress.kubernetes.io/router.priority"] == "99999"
    assert annotations["traefik.ingress.kubernetes.io/frontend-entry-points"] == "web"


def test_cert_manager_issuers_staging_and_prod(tmp_path):
    raw = {"cert-manager": {"email": "a@b", "staging": True, "prod": True}}
    entry = _entry(tmp_path, raw, "cert-manager")
    docs = list(yaml.safe_load_all(cert_manager_issuers(entry)))
    assert [d["metadata"]["name"] for d in docs] == ["letsencrypt-staging", "letsencrypt-prod"]
    assert "acme-staging" in docs[0]["spec"]["acme"]["server"]


def test_cert_manager_issuers_empty_without_flags(tmp_path):
    entry = _entry(tmp_path, {"cert-manager": {"email": "a@b"}}, "cert-manager")
    assert cert_manager_issuers(entry) == ""

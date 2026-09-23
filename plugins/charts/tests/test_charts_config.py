"""Schema and helpers for the `charts:` section of cluster.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from taloscluster.errors import ConfigError

from taloscluster_charts.config import (
    Config,
    charts_configured,
    is_newer,
    merge_values,
    same_version,
    validate_charts,
)


def _write_cluster(root: Path, charts: dict | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    doc = {"name": "test"}
    if charts is not None:
        doc["charts"] = charts
    (root / "cluster.yaml").write_text(yaml.safe_dump(doc))
    return root


def test_absent_section_is_not_configured(tmp_path):
    _write_cluster(tmp_path, None)
    assert charts_configured(tmp_path) is False


def test_present_section_is_configured(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"enabled": True}})
    assert charts_configured(tmp_path) is True


def test_all_disabled_section_is_not_configured(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"enabled": False}, "traefik": {"enabled": False}})
    assert charts_configured(tmp_path) is False


def test_section_with_a_default_enabled_entry_is_configured(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"enabled": False}, "traefik": {}})
    assert charts_configured(tmp_path) is True


def test_malformed_section_is_not_configured(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"bogus": 1}})
    assert charts_configured(tmp_path) is False
    _write_cluster(tmp_path, "not-a-mapping")
    assert charts_configured(tmp_path) is False


def test_known_chart_defaults(tmp_path):
    _write_cluster(tmp_path, {"metallb": {}})
    entry = Config.load(tmp_path).entries["metallb"]
    assert entry.enabled is True
    assert entry.is_latest
    assert entry.repo == "https://metallb.github.io/metallb"
    assert entry.namespace.name == "metallb-system"
    assert entry.namespace.enforce == "privileged"


def test_gateway_manifest_url_from_version(tmp_path):
    _write_cluster(tmp_path, {"gateway": {"version": "v1.3.1"}})
    entry = Config.load(tmp_path).entries["gateway"]
    assert entry.urls() == (
        "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.3.1"
        "/standard-install.yaml",
    )


def test_sealed_secrets_defaults(tmp_path):
    _write_cluster(tmp_path, {"sealed-secrets": {}})
    entry = Config.load(tmp_path).entries["sealed-secrets"]
    assert entry.is_chart
    assert entry.repo == "https://bitnami.github.io/sealed-secrets"
    assert entry.namespace.name == "sealed-secrets"
    assert entry.namespace.enforce == "restricted"


def test_cert_manager_defaults(tmp_path):
    _write_cluster(tmp_path, {"cert-manager": {"email": "a@example.edu", "prod": True}})
    entry = Config.load(tmp_path).entries["cert-manager"]
    assert entry.is_chart
    assert entry.repo == "https://charts.jetstack.io"
    assert entry.namespace.name == "cert-manager"
    assert entry.prod is True
    assert entry.staging is False


def test_cert_manager_email_required_with_issuers(tmp_path):
    for issuers in ({"staging": True}, {"prod": True}):
        _write_cluster(tmp_path, {"cert-manager": dict(issuers)})
        with pytest.raises(ConfigError, match="email is required"):
            validate_charts(tmp_path)
    _write_cluster(tmp_path, {"cert-manager": {}})
    validate_charts(tmp_path)  # no issuers, no email needed
    _write_cluster(tmp_path, {"cert-manager": {"enabled": False, "staging": True}})
    validate_charts(tmp_path)  # disabled: issuers never rendered, email optional


def test_staging_prod_rejected_outside_cert_manager(tmp_path):
    _write_cluster(tmp_path, {"traefik": {"email": "a@b", "staging": True}})
    with pytest.raises(ConfigError, match="only used by cert-manager"):
        validate_charts(tmp_path)


def test_staging_prod_must_be_bool(tmp_path):
    _write_cluster(tmp_path, {"cert-manager": {"email": "a@b", "prod": "yes"}})
    with pytest.raises(ConfigError, match="prod must be a boolean"):
        validate_charts(tmp_path)


def test_ceph_entry_schema(tmp_path):
    _write_cluster(tmp_path, {"ceph": {"enabled": False}})
    entry = Config.load(tmp_path).entries["ceph"]
    assert entry.repo == "https://ceph.github.io/csi-charts"
    assert entry.rbd is False
    _write_cluster(tmp_path, {"ceph": {}})
    with pytest.raises(ConfigError, match="clusterID is required"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"ceph": {"clusterID": "x", "monitors": ["m:6789"]}})
    with pytest.raises(ConfigError, match="at least one of rbd or fs"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"ceph": {"clusterID": "x", "monitors": [], "rbd": True}})
    with pytest.raises(ConfigError, match="monitors must be a non-empty list"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"ceph": {"clusterID": "x", "monitors": ["m:6789"], "rbd": "yes"}})
    with pytest.raises(ConfigError, match="rbd must be a boolean or a mapping"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"gateway": {"clusterID": "x", "rbd": True}})
    with pytest.raises(ConfigError, match="only used by ceph"):
        validate_charts(tmp_path)
    _write_cluster(
        tmp_path, {"ceph": {"clusterID": "x", "monitors": ["m:6789"], "rbd": True, "fs": True}}
    )
    entry = Config.load(tmp_path).entries["ceph"]
    assert entry.cluster_id == "x"
    assert entry.monitors == ("m:6789",)
    assert entry.rbd and entry.fs


def test_ceph_driver_mappings_create_storage_classes(tmp_path):
    base = {"clusterID": "x", "monitors": ["m:6789"]}
    _write_cluster(tmp_path, {"ceph": {
        **base, "rbd": {"pool": "kubernetes", "defaultClass": True}, "fs": {"fsName": "cephfs"},
    }})
    entry = Config.load(tmp_path).entries["ceph"]
    assert entry.rbd and entry.fs
    assert entry.rbd_class == {"pool": "kubernetes", "defaultClass": True}
    assert entry.fs_class == {"fsName": "cephfs"}
    # a bare boolean installs the driver alone
    _write_cluster(tmp_path, {"ceph": {**base, "rbd": True}})
    entry = Config.load(tmp_path).entries["ceph"]
    assert entry.rbd and entry.rbd_class is None and entry.fs_class is None
    for bad, msg in (
        ({"rbd": {}}, "rbd.pool must be a non-empty string"),
        ({"fs": {"pool": "p"}}, "fs.fsName must be a non-empty string"),
        ({"rbd": {"pool": "p", "fsName": "f"}}, "rbd: unsupported key"),
        ({"rbd": {"pool": "p", "name": ""}}, "rbd.name must be a non-empty string"),
        ({"rbd": {"pool": "p", "defaultClass": True}, "fs": {"fsName": "f", "defaultClass": True}},
         "at most one of rbd and fs may set defaultClass"),
        ({"rbd": {"pool": "p"}, "values": {"storageClass": {"create": True}}},
         "rbd/fs mappings or values.storageClass, not both"),
    ):
        _write_cluster(tmp_path, {"ceph": {**base, **bad}})
        with pytest.raises(ConfigError, match=msg):
            Config.load(tmp_path)


def test_ceph_secrets_merge_from_secrets_yaml(tmp_path):
    _write_cluster(tmp_path, {"ceph": {"clusterID": "x", "monitors": ["m:6789"], "rbd": True}})
    with (tmp_path / "cluster.yaml").open("a") as f:
        f.write("include: [secrets.yaml]\n")
    (tmp_path / "secrets.yaml").write_text(
        yaml.safe_dump({"charts": {"ceph": {"userID": "admin", "userKey": "AQC..."}}})
    )
    secrets = Config.load(tmp_path).entries["ceph"].ceph_secrets
    assert secrets is not None
    assert secrets.user_id == "admin"
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"openstack": {}}))
    assert Config.load(tmp_path).entries["ceph"].ceph_secrets is None
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"charts": {"ceph": {"userID": "a"}}}))
    with pytest.raises(ConfigError, match="both userID and userKey"):
        Config.load(tmp_path)


def test_ceph_credentials_only_for_ceph(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"userID": "a", "userKey": "k"}})
    with pytest.raises(ConfigError, match="only used by ceph"):
        Config.load(tmp_path)


def test_nfs_storage_classes_schema(tmp_path):
    _write_cluster(tmp_path, {"nfs": {}})
    with pytest.raises(ConfigError, match="storageClasses is required"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"metallb": {"storageClasses": [{"name": "x"}]}})
    with pytest.raises(ConfigError, match="only used by nfs"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"nfs": {"storageClasses": [
        {"name": "a", "server": "s", "share": "/x"},
        {"name": "a", "server": "s", "share": "/x"},
    ]}})
    with pytest.raises(ConfigError, match="duplicate name"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"nfs": {"storageClasses": [
        {"name": "a", "server": "s", "share": "/x", "defaultClass": True},
        {"name": "b", "server": "s", "share": "/x", "defaultClass": True},
    ]}})
    with pytest.raises(ConfigError, match="at most one storageClass"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"nfs": {"storageClasses": [
        {"name": "a", "server": "s", "share": "/x", "bogus": 1},
    ]}})
    with pytest.raises(ConfigError, match="unsupported key"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"nfs": {
        "storageClasses": [{"name": "a", "server": "s", "share": "/x"}],
        "values": {"storageClasses": []},
    }})
    with pytest.raises(ConfigError, match="not both"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"nfs": {"storageClasses": [
        {"name": "a", "server": "s", "share": "/x"},
    ]}})
    validate_charts(tmp_path)


def test_traefik_acme_resolver_rejected_alongside_cert_manager_issuers(tmp_path):
    charts = {
        "cert-manager": {"email": "a@b", "prod": True},
        "traefik": {"values": {"certificatesResolvers": {"le": {"acme": {"email": "a@b"}}}}},
    }
    _write_cluster(tmp_path, charts)
    with pytest.raises(ConfigError, match="two ACME clients"):
        validate_charts(tmp_path)


def test_traefik_acme_resolver_args_rejected_alongside_cert_manager_issuers(tmp_path):
    charts = {
        "cert-manager": {"email": "a@b", "staging": True},
        "traefik": {"values": {"additionalArguments": [
            "--certificatesresolvers.le.acme.email=a@b",
        ]}},
    }
    _write_cluster(tmp_path, charts)
    with pytest.raises(ConfigError, match="two ACME clients"):
        validate_charts(tmp_path)


def test_traefik_acme_resolver_alone_is_fine(tmp_path):
    # without cert-manager's issuers there is only one acme client
    _write_cluster(tmp_path, {"traefik": {"values": {"certificatesResolvers": {"le": {}}}}})
    validate_charts(tmp_path)
    _write_cluster(tmp_path, {
        "cert-manager": {"email": "a@b", "enabled": False, "prod": True},
        "traefik": {"values": {"certificatesResolvers": {"le": {}}}},
    })
    validate_charts(tmp_path)
    # issuers on, but traefik carries no resolver
    _write_cluster(tmp_path, {
        "cert-manager": {"email": "a@b", "prod": True},
        "traefik": {},
    })
    validate_charts(tmp_path)


def test_namespace_dict_form(tmp_path):
    _write_cluster(tmp_path, {"foo": {"repo": "https://example.com/charts",
                                      "namespace": {"name": "apps", "enforce": "restricted"}}})
    entry = Config.load(tmp_path).entries["foo"]
    assert entry.namespace.name == "apps"
    assert entry.namespace.enforce == "restricted"
    assert entry.namespace.audit is None


def test_unknown_entry_requires_source(tmp_path):
    _write_cluster(tmp_path, {"mystery": {}})
    with pytest.raises(ConfigError, match="set repo or manifest"):
        validate_charts(tmp_path)


def test_repo_and_manifest_conflict(tmp_path):
    _write_cluster(tmp_path, {"foo": {"repo": "https://a", "manifest": "https://b"}})
    with pytest.raises(ConfigError, match="either repo or manifest"):
        validate_charts(tmp_path)


def test_unsupported_key_rejected(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"bogus": 1}})
    with pytest.raises(ConfigError, match="unsupported key"):
        validate_charts(tmp_path)


def test_manifest_entry_refuses_values(tmp_path):
    _write_cluster(tmp_path, {"gateway": {"values": {}}})
    with pytest.raises(ConfigError, match="values is not used"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"crds": {"manifest": "https://example.com/x.yaml", "values": {}}})
    with pytest.raises(ConfigError, match="values is not used"):
        validate_charts(tmp_path)


def test_manifest_entry_refuses_namespace(tmp_path):
    _write_cluster(tmp_path, {"gateway": {"namespace": "gateway-system"}})
    with pytest.raises(ConfigError, match="namespace is not used"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"crds": {"manifest": "https://example.com/x.yaml", "namespace": "x"}})
    with pytest.raises(ConfigError, match="namespace is not used"):
        validate_charts(tmp_path)


def test_namespace_mapping_rejects_unknown_keys(tmp_path):
    _write_cluster(tmp_path, {"foo": {
        "repo": "https://example.com/charts",
        "namespace": {"name": "tr", "enforse": "privileged"},
    }})
    with pytest.raises(ConfigError, match="namespace: unsupported key"):
        validate_charts(tmp_path)


def test_ceph_refuses_namespace(tmp_path):
    _write_cluster(
        tmp_path, {"ceph": {"clusterID": "x", "monitors": ["m:6789"], "namespace": "ceph"}}
    )
    with pytest.raises(ConfigError, match="namespace is not used by ceph"):
        validate_charts(tmp_path)


def test_unknown_chart_entry_defaults_namespace_to_entry_name(tmp_path):
    _write_cluster(tmp_path, {"mystery": {"repo": "https://example.com/charts"}})
    entry = Config.load(tmp_path).entries["mystery"]
    assert entry.namespace.name == "mystery"
    assert entry.namespace.enforce is None
    # a configured namespace still wins
    _write_cluster(tmp_path, {"mystery": {"repo": "https://example.com/charts",
                                          "namespace": "other"}})
    assert Config.load(tmp_path).entries["mystery"].namespace.name == "other"


def test_email_rejected_outside_cert_manager(tmp_path):
    _write_cluster(tmp_path, {"metallb": {"email": "a@example.edu"}})
    with pytest.raises(ConfigError, match="only used by cert-manager"):
        validate_charts(tmp_path)
    _write_cluster(tmp_path, {"traefik": {"email": "a@example.edu"}})
    with pytest.raises(ConfigError, match="only used by cert-manager"):
        validate_charts(tmp_path)


def test_traefik_needs_no_email(tmp_path):
    # traefik runs no acme resolver of its own; cert-manager issues the certs
    _write_cluster(tmp_path, {"traefik": {"enabled": True}})
    validate_charts(tmp_path)
    _write_cluster(tmp_path, {"traefik": {"enabled": False}})
    validate_charts(tmp_path)


def test_gateway_requires_version_only_for_templates(tmp_path):
    # a `latest` gateway resolves the newest release at converge/check time
    _write_cluster(tmp_path, {"gateway": {"version": "latest"}})
    assert Config.load(tmp_path).entries["gateway"].is_latest
    _write_cluster(tmp_path, {"gateway": {"version": "v1.6.2"}})
    assert Config.load(tmp_path).entries["gateway"].version == "v1.6.2"


def test_gateway_latest_urls_require_resolution(tmp_path):
    _write_cluster(tmp_path, {"gateway": {"version": "latest"}})
    entry = Config.load(tmp_path).entries["gateway"]
    with pytest.raises(ConfigError, match="cannot resolve the latest release"):
        entry.urls()
    assert entry.urls(resolved="v1.7.0") == (
        "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.7.0"
        "/standard-install.yaml",
    )


def test_version_forbidden_on_manifest_entry(tmp_path):
    _write_cluster(tmp_path, {"crds": {"manifest": "https://example.com/x.yaml", "version": "1.0"}})
    with pytest.raises(ConfigError, match="version is not used"):
        validate_charts(tmp_path)


def test_repo_rejected_on_gateway(tmp_path):
    _write_cluster(tmp_path, {"gateway": {"version": "v1.3.1", "repo": "https://a"}})
    with pytest.raises(ConfigError, match="no repo"):
        validate_charts(tmp_path)


def test_version_key_and_is_newer():
    assert is_newer("0.15.0", "0.14.9")
    assert is_newer("v1.3.1", "1.3.0")
    assert not is_newer("0.14.9", "0.14.9")
    assert not is_newer("0.14", "0.14.9")


def test_same_version_ignores_a_leading_v():
    assert same_version("v1.21.2", "1.21.2")
    assert same_version("1.21.2", "v1.21.2")
    assert same_version("V1.21.2", "v1.21.2")
    assert not same_version("v1.21.2", "1.21.3")
    assert not same_version("v1.21.2", "")
    assert not same_version("version", "1.21.2")


def test_merge_values_deep_dicts_lists_replace():
    common = {"a": {"b": 1, "c": 2}, "l": [1, 2]}
    assert merge_values(common, {"a": {"b": 9}, "l": [3]}) == {"a": {"b": 9, "c": 2}, "l": [3]}

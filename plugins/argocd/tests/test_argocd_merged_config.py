"""Plugin credentials follow the include contract.

The `argocd:` section -- the kubectl apply target, the git credentials and the
OpenStack application credential the Cinder Secret needs -- is read from the
merged configuration (cluster.yaml, secrets.yaml or any included file), so
moving it out of secrets.yaml must keep the plugin configured, its validation
working and the values loadable.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.errors import ConfigError

from taloscluster_argocd.config import Config, argocd_configured, validate_argocd


def _write_cluster(root, cluster, include_files=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster))
    for name, data in (include_files or {}).items():
        (root / name).write_text(yaml.safe_dump(data))


def test_kubeconfig_in_cluster_yaml_activates_and_loads(tmp_path):
    """The item this pins: an apply target in cluster.yaml must not leave the
    plugin silently unconfigured."""
    _write_cluster(tmp_path, {
        "name": "testcluster",
        "argocd": {"kubeconfig": "../argocd-kubeconfig"},
    })
    assert argocd_configured(tmp_path) is True
    validate_argocd(tmp_path)
    target = Config.load_secrets(tmp_path)
    assert target.kubeconfig == "../argocd-kubeconfig"


def test_kubeconfig_in_an_included_file_activates(tmp_path):
    _write_cluster(tmp_path, {"name": "testcluster", "include": ["argocd.yaml"]},
                   include_files={"argocd.yaml": {"argocd": {"context": "argocd"}}})
    assert argocd_configured(tmp_path) is True


def test_openstack_credential_for_cinder_can_live_in_cluster_yaml(tmp_path):
    """The Cinder Secret's application credential is read from the merged
    configuration, not from secrets.yaml by name."""
    _write_cluster(tmp_path, {
        "name": "testcluster",
        "argocd": {"kubeconfig": "../argocd-kubeconfig"},
        "openstack": {"credential_id": "id", "credential_secret": "secret"},
    })
    target = Config.load_secrets(tmp_path)
    assert target.openstack_credential_id == "id"
    assert target.openstack_credential_secret == "secret"


def test_git_credentials_can_live_in_cluster_yaml(tmp_path):
    _write_cluster(tmp_path, {
        "name": "testcluster",
        "argocd": {"kubeconfig": "../argocd-kubeconfig",
                   "git": {"url": "https://git.example.com/cluster.git",
                           "username": "deploy", "token": "CHANGE-ME"},
                   "infra": {"url": "https://git.example.com/infra.git"}},
    })
    validate_argocd(tmp_path)
    target = Config.load_secrets(tmp_path)
    assert target.git_username == "deploy"
    assert target.git_token == "CHANGE-ME"


def test_url_token_in_cluster_yaml_is_still_an_unsupported_apply_target(tmp_path):
    _write_cluster(tmp_path, {
        "name": "testcluster",
        "argocd": {"url": "https://argocd.example.edu", "token": "CHANGE-ME"},
    })
    with pytest.raises(ConfigError, match="url/token is not a supported apply target"):
        validate_argocd(tmp_path)
    assert argocd_configured(tmp_path) is False


def test_validate_refuses_a_bad_apply_target_value_wherever_it_lives(tmp_path):
    _write_cluster(tmp_path, {
        "name": "testcluster", "argocd": {"kubeconfig": ["not", "a", "string"]},
    })
    with pytest.raises(ConfigError, match=r"argocd\.kubeconfig.*string"):
        validate_argocd(tmp_path)

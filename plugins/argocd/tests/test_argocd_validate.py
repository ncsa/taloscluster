"""Early plugin-configuration validation.

Core calls each configured plugin's `validate` hook in converge's validate phase,
before any cluster mutation. These cover what the argocd plugin rejects there:
paired repository URLs, git credentials without a Git URL, malformed settings,
and unsupported options.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.errors import ConfigError

from taloscluster_argocd.config import validate_argocd


def _write(root, argocd_cluster=None, argocd_secrets=None, name="testcluster"):
    root.mkdir(parents=True, exist_ok=True)
    cluster = {"name": name}
    if argocd_cluster is not None:
        cluster["argocd"] = argocd_cluster
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster))
    secrets = {}
    if argocd_secrets is not None:
        secrets["argocd"] = argocd_secrets
    (root / "secrets.yaml").write_text(yaml.safe_dump(secrets))


def _ok(root):
    validate_argocd(root)


def test_valid_full_config_passes(tmp_path):
    _write(
        tmp_path,
        argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                        "infra": {"url": "https://git.example.com/infra.git"}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig",
                        "git": {"username": "deploy", "token": "CHANGE-ME"}},
    )
    _ok(tmp_path)


def test_valid_registration_only_passes(tmp_path):
    """No git/infra URLs and no git credentials is fine -- secret + project only."""
    _write(tmp_path, argocd_cluster={}, argocd_secrets={"kubeconfig": "../argocd-kubeconfig"})
    _ok(tmp_path)


# ---- missing paired repository URLs ---------------------------------------


def test_git_url_requires_infra_url(tmp_path):
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"}})
    with pytest.raises(ConfigError, match=r"argocd\): infra\.url must be set together"):
        validate_argocd(tmp_path)


def test_infra_url_requires_git_url(tmp_path):
    _write(tmp_path, argocd_cluster={"infra": {"url": "https://git.example.com/infra.git"}})
    with pytest.raises(ConfigError, match=r"argocd\): git\.url must be set together"):
        validate_argocd(tmp_path)


# ---- credentials without a Git URL ----------------------------------------


def test_git_credentials_require_git_url(tmp_path):
    _write(
        tmp_path,
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig",
                        "git": {"username": "deploy", "token": "CHANGE-ME"}},
    )
    with pytest.raises(ConfigError, match=r"git credentials are set"):
        validate_argocd(tmp_path)


def test_git_token_alone_requires_git_url(tmp_path):
    _write(
        tmp_path,
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig", "git": {"token": "CHANGE-ME"}},
    )
    with pytest.raises(ConfigError, match=r"git credentials are set"):
        validate_argocd(tmp_path)


# ---- malformed settings ---------------------------------------------------


def test_non_mapping_argocd_rejected(tmp_path):
    _write(tmp_path, argocd_cluster="enabled")
    with pytest.raises(ConfigError, match="argocd must be a YAML mapping"):
        validate_argocd(tmp_path)


def test_non_mapping_argocd_in_secrets_rejected(tmp_path):
    _write(tmp_path, argocd_secrets="enabled")
    with pytest.raises(ConfigError, match="argocd must be a YAML mapping"):
        validate_argocd(tmp_path)


def test_non_mapping_git_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={"git": "https://git.example.com/cluster.git"})
    with pytest.raises(ConfigError, match="argocd.git.*YAML mapping"):
        validate_argocd(tmp_path)


def test_non_mapping_per_app_section_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={"metallb": "yes"})
    with pytest.raises(ConfigError, match="argocd.metallb.*YAML mapping"):
        validate_argocd(tmp_path)


# ---- unsupported options --------------------------------------------------


@pytest.mark.parametrize("key", ["admin", "sync_mode", "gitlab", "appofapps"])
def test_unsupported_top_level_option_rejected(tmp_path, key):
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                                     "infra": {"url": "https://git.example.com/infra.git"},
                                     key: "x"})
    with pytest.raises(ConfigError, match=f"unsupported option.*{key}"):
        validate_argocd(tmp_path)

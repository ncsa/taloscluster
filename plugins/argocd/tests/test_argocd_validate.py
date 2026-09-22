"""Early plugin-configuration validation.

Core calls each installed plugin's `validate` hook in converge's validate
phase, before any cluster mutation, whether or not the plugin is active -- a
supplied-but-malformed `argocd:` section is rejected even though activation
would silently discard it. These cover what the argocd plugin rejects on that
path: paired repository URLs, git credentials without a Git URL, malformed
settings, unsupported options, and secrets `url`/`token` apply targets without
a `kubeconfig`/`context`.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.errors import ConfigError

from taloscluster_argocd.config import validate_argocd


def _write(root, argocd_cluster=None, argocd_secrets=None, name="testcluster"):
    root.mkdir(parents=True, exist_ok=True)
    cluster = {"name": name, "include": ["secrets.yaml"]}
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


# ---- boolean and scalar type validation -----------------------------------


@pytest.mark.parametrize("bad", ["false", "true", 1, 0])
def test_sync_must_be_a_boolean(tmp_path, bad):
    _write(tmp_path, argocd_cluster={"sync": bad})
    with pytest.raises(ConfigError, match="argocd\\.sync.*boolean"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("bad", ["false", "true", 1, 0])
def test_automated_must_be_a_boolean(tmp_path, bad):
    _write(tmp_path, argocd_cluster={"automated": bad})
    with pytest.raises(ConfigError, match="argocd\\.automated.*boolean"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize(
    "app", ["metallb", "ingress", "sealedsecrets", "certmanager", "cinder", "nfs", "monitoring"]
)
def test_per_app_enabled_must_be_a_boolean(tmp_path, app):
    _write(tmp_path, argocd_cluster={app: {"enabled": "false"}})
    with pytest.raises(ConfigError, match=f"argocd\\.{app}\\.enabled.*boolean"):
        validate_argocd(tmp_path)


def test_quoted_booleans_reported_as_the_type_problem(tmp_path):
    """A quoted 'false' is refused for what it is: the wrong YAML type."""
    _write(tmp_path, argocd_cluster={"automated": "false", "sync": "false"})
    with pytest.raises(ConfigError, match=r"must be a boolean \(true or false\)"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("bad", ["alice@example.com", 3, [1, 2]])
def test_members_must_be_a_list_of_strings(tmp_path, bad):
    _write(tmp_path, argocd_cluster={"admins": bad})
    with pytest.raises(ConfigError, match="argocd\\.admins.*list of email addresses"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("repo", ["git", "infra"])
def test_repository_url_must_be_a_nonempty_string(tmp_path, repo):
    _write(tmp_path, argocd_cluster={repo: {"url": 42}})
    with pytest.raises(ConfigError, match=f"argocd\\.{repo}\\.url.*non-empty string"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("key", ["kubeconfig", "context", "url", "token"])
def test_apply_target_value_must_be_a_string(tmp_path, key):
    _write(tmp_path, argocd_cluster={}, argocd_secrets={key: ["not", "a", "string"]})
    with pytest.raises(ConfigError, match=f"argocd\\.{key}.*string"):
        validate_argocd(tmp_path)


# ---- unsupported apply-target mode -----------------------------------------


@pytest.mark.parametrize(
    "secrets",
    [
        {"url": "https://argocd.example.edu", "token": "CHANGE-ME"},
        {"url": "https://argocd.example.edu"},
        {"token": "CHANGE-ME"},
    ],
)
def test_url_token_alone_is_an_unsupported_apply_target(tmp_path, secrets):
    """A supplied url/token names the ArgoCD API, which the plugin cannot apply
    through; it must be reported instead of silently leaving the plugin
    inactive."""
    _write(tmp_path, argocd_cluster={}, argocd_secrets=secrets)
    with pytest.raises(ConfigError, match="url/token is not a supported apply target"):
        validate_argocd(tmp_path)


def test_url_token_with_a_kubeconfig_still_activates_mode(tmp_path):
    """A kubectl apply target alongside a url/token is a supported mode, so no
    unsupported-mode refusal."""
    _write(
        tmp_path,
        argocd_cluster={},
        argocd_secrets={
            "kubeconfig": "../argocd-kubeconfig",
            "url": "https://argocd.example.edu",
            "token": "x",
        },
    )
    _ok(tmp_path)


@pytest.mark.parametrize("key", ["username", "token"])
def test_secret_git_credential_type_must_be_a_string(tmp_path, key):
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                                     "infra": {"url": "https://git.example.com/infra.git"}},
            argocd_secrets={"git": {key: 123}})
    with pytest.raises(ConfigError, match=f"argocd\\.git\\.{key}.*string"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("key", ["credential_id", "credential_secret"])
def test_openstack_credential_must_be_a_string(tmp_path, key):
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"name": "testcluster", "include": ["secrets.yaml"]})
    )
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"openstack": {key: 123}}))
    with pytest.raises(ConfigError, match=f"openstack\\.{key}.*string"):
        validate_argocd(tmp_path)


def test_non_mapping_secret_git_section_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={"infra": {"url": "https://git.example.com/b"}},
            argocd_secrets={"git": "deploy"})
    with pytest.raises(ConfigError, match="argocd\\.git.*YAML mapping"):
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


# ---- unknown keys inside repository and credential blocks -------------------


@pytest.mark.parametrize("repo", ["git", "infra"])
def test_unknown_repository_key_rejected(tmp_path, repo):
    _write(tmp_path, argocd_cluster={repo: {"url": "https://git.example.com/cluster.git",
                                            "usl": "https://git.example.com/other.git"}})
    with pytest.raises(ConfigError, match=f"argocd\\.{repo}\\).*unsupported key.*usl"):
        validate_argocd(tmp_path)


def test_misspelled_repository_url_key_rejected(tmp_path):
    """`git.urre` is a typo for `git.url`; it must never be silently dropped."""
    _write(tmp_path, argocd_cluster={"git": {"urre": "https://git.example.com/cluster.git"},
                                     "infra": {"url": "https://git.example.com/infra.git"}})
    with pytest.raises(ConfigError, match="argocd\\.git\\).*unsupported key.*urre"):
        validate_argocd(tmp_path)


def test_repository_branch_key_is_rejected(tmp_path):
    """Only `url` is supported; a `branch` override is refused rather than ignored."""
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git",
                                             "branch": "main"},
                                     "infra": {"url": "https://git.example.com/infra.git"}})
    with pytest.raises(ConfigError, match="argocd\\.git\\).*unsupported key.*branch"):
        validate_argocd(tmp_path)


def test_unknown_secrets_git_credential_key_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                                     "infra": {"url": "https://git.example.com/infra.git"}},
            argocd_secrets={"git": {"username": "deploy", "tokn": "x"}})
    with pytest.raises(ConfigError, match=r"argocd\.git\): unsupported key\(s\): tokn"):
        validate_argocd(tmp_path)


def test_unknown_secrets_section_key_rejected(tmp_path):
    """A miscapped key in the `argocd:` secrets section is refused."""
    _write(tmp_path, argocd_secrets={"kubeconfig": "../argocd-kubeconfig",
                                     "kubeconfigg": "../other"})
    with pytest.raises(ConfigError, match=r"argocd\): unsupported option\(s\): kubeconfigg"):
        validate_argocd(tmp_path)


# ---- unsupported options --------------------------------------------------


@pytest.mark.parametrize("key", ["admin", "sync_mode", "gitlab", "appofapps"])
def test_unsupported_top_level_option_rejected(tmp_path, key):
    _write(tmp_path, argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                                     "infra": {"url": "https://git.example.com/infra.git"},
                                     key: "x"})
    with pytest.raises(ConfigError, match=f"unsupported option.*{key}"):
        validate_argocd(tmp_path)


# ---- per-app keys and version overrides ------------------------------------


@pytest.mark.parametrize(
    "section",
    [
        {"metallb": {"enabled": True, "verson": "1.2"}},
        {"certmanager": {"enabled": True, "emial": "admin@example.edu"}},
        {"monitoring": {"enabled": True, "scrapeInterval": "30s"}},
    ],
)
def test_misspelled_per_app_key_rejected(tmp_path, section):
    _write(tmp_path, argocd_cluster=section)
    with pytest.raises(ConfigError, match="unsupported key"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("app", ["ingress", "nfs", "monitoring"])
def test_version_on_an_app_that_does_not_forward_it_is_rejected(tmp_path, app):
    _write(tmp_path, argocd_cluster={app: {"enabled": True, "version": "9.9"}})
    with pytest.raises(ConfigError, match=f"argocd\\.{app}\\.version.*does not forward"):
        validate_argocd(tmp_path)


@pytest.mark.parametrize("app", ["metallb", "sealedsecrets", "certmanager", "cinder"])
def test_forwarded_version_is_accepted(tmp_path, app):
    _write(tmp_path, argocd_cluster={app: {"enabled": True, "version": "34.0.0"}})
    _ok(tmp_path)


@pytest.mark.parametrize("bad", [34.0, 34, "", "   "])
def test_forwarded_version_must_be_a_nonempty_string(tmp_path, bad):
    _write(tmp_path, argocd_cluster={"metallb": {"enabled": True, "version": bad}})
    with pytest.raises(ConfigError, match="argocd\\.metallb\\.version.*non-empty string"):
        validate_argocd(tmp_path)


def test_ingress_traefik_version_is_accepted(tmp_path):
    _write(tmp_path, argocd_cluster={
        "ingress": {"enabled": True, "class": "traefik", "traefik": {"version": "34.0.0"}}})
    _ok(tmp_path)


def test_ingress_traefik_misspelled_key_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={
        "ingress": {"enabled": True, "traefik": {"verson": "34.0.0"}}})
    with pytest.raises(ConfigError, match="argocd\\.ingress\\.traefik.*unsupported key"):
        validate_argocd(tmp_path)


def test_ingress_traefik_must_be_a_mapping(tmp_path):
    _write(tmp_path, argocd_cluster={"ingress": {"enabled": True, "traefik": "34.0.0"}})
    with pytest.raises(ConfigError, match="argocd\\.ingress\\.traefik.*YAML mapping"):
        validate_argocd(tmp_path)


def test_nfs_version_is_rejected(tmp_path):
    _write(tmp_path, argocd_cluster={"nfs": {"enabled": True, "servers": {}, "version": "1"}})
    with pytest.raises(ConfigError, match="argocd\\.nfs\\.version.*does not forward"):
        validate_argocd(tmp_path)


def test_nfs_servers_must_map_names_to_mappings(tmp_path):
    _write(tmp_path, argocd_cluster={"nfs": {"enabled": True, "servers": "nfs.example.edu"}})
    with pytest.raises(ConfigError, match="argocd\\.nfs\\.servers.*map server names to mappings"):
        validate_argocd(tmp_path)


def test_nfs_servers_verbatim_chart_keys_are_accepted(tmp_path):
    _write(tmp_path, argocd_cluster={"nfs": {"enabled": True, "servers": {
        "shared": {"server": "nfs.example.edu", "path": "/exports/x", "defaultClass": True}}}})
    _ok(tmp_path)


def test_valid_full_config_with_versions_passes(tmp_path):
    _write(
        tmp_path,
        argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"},
                        "infra": {"url": "https://git.example.com/infra.git"},
                        "metallb": {"enabled": True, "version": "34.0.0"},
                        "ingress": {"enabled": True, "traefik": {"version": "28.0.0"}},
                        "certmanager": {"email": "admin@example.edu", "version": "1.14.0"},
                        "nfs": {"enabled": True, "servers": {"shared": {"path": "/e"}}}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    _ok(tmp_path)


# ---- core reaches the hook for supplied-but-inactive config -----------------
# Activation can silently discard a supplied malformed section: a non-mapping
# `argocd:` in secrets.yaml, or a `url`/`token` apply target, never activates
# the plugin, so core must still run `validate_argocd`. These go through the
# real `taloscluster.plugins.validate` so the full core -> plugin chain is
# exercised, not just the hook in isolation.


def _plugins_validate(tmp_path):
    import taloscluster.plugins as core_plugins
    from taloscluster.context import Context

    core_plugins.validate(Context(root=tmp_path, cfg=None, status={}))


def test_plugins_validate_refuses_a_non_mapping_secrets_section(tmp_path):
    _write(tmp_path, argocd_secrets="enabled")
    with pytest.raises(ConfigError, match=r"argocd must be a YAML mapping"):
        _plugins_validate(tmp_path)


def test_plugins_validate_refuses_url_token_only_apply_target(tmp_path):
    _write(tmp_path, argocd_secrets={"url": "https://argocd.example.edu",
                                     "token": "CHANGE-ME"})
    with pytest.raises(ConfigError, match="url/token is not a supported apply target"):
        _plugins_validate(tmp_path)


def test_plugins_validate_noops_when_config_is_absent(tmp_path):
    """An entirely absent section has nothing to validate."""
    _write(tmp_path)
    _plugins_validate(tmp_path)  # no error

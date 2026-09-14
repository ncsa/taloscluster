"""Early plugin-configuration validation.

Core calls each installed plugin's `validate` hook in converge's validate
phase, before any cluster mutation, whether or not the plugin is active -- a
supplied-but-malformed `rancher:` section is rejected even though activation
would silently discard it. These cover what the rancher plugin rejects on that
path: admins/users that are not lists of usernames, a member under both tiers,
and secret credential values that are not non-empty strings, plus a non-mapping
`rancher:` section in either file.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.context import Context
from taloscluster.errors import ConfigError
from taloscluster.plugins import validate as preflight_validate

from taloscluster_rancher.config import validate_rancher


def _write(root, rancher_cluster=None, rancher_secrets=None, name="testcluster"):
    root.mkdir(parents=True, exist_ok=True)
    cluster = {"name": name}
    if rancher_cluster is not None:
        cluster["rancher"] = rancher_cluster
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster))
    secrets = {}
    if rancher_secrets is not None:
        secrets["rancher"] = rancher_secrets
    (root / "secrets.yaml").write_text(yaml.safe_dump(secrets))


def _ok(root):
    validate_rancher(root)


def test_valid_full_config_passes(tmp_path):
    _write(tmp_path, rancher_cluster={"admins": ["alice"], "users": ["carol"]},
            rancher_secrets={"url": "https://rancher.example.com", "token": "token-x:y"})
    _ok(tmp_path)


def test_empty_rancher_sections_pass(tmp_path):
    """Only the `rancher:` sections present, no members or credentials, is fine."""
    _write(tmp_path, rancher_cluster={}, rancher_secrets={})
    _ok(tmp_path)


def test_no_rancher_sections_pass(tmp_path):
    """A cluster with no `rancher:` section at all is not managed; nothing to reject."""
    _write(tmp_path)
    _ok(tmp_path)


# ---- section shape ---------------------------------------------------------


def test_non_mapping_rancher_in_cluster_yaml_rejected(tmp_path):
    _write(tmp_path, rancher_cluster="enabled")
    with pytest.raises(ConfigError, match="rancher must be a YAML mapping"):
        validate_rancher(tmp_path)


def test_non_mapping_rancher_in_secrets_yaml_rejected(tmp_path):
    _write(tmp_path, rancher_secrets="enabled")
    with pytest.raises(ConfigError, match="rancher must be a YAML mapping"):
        validate_rancher(tmp_path)


# ---- membership tiers -------------------------------------------------------


@pytest.mark.parametrize("bad", ["alice", 3, [1, 2], ["alice", 3]])
def test_admins_must_be_a_list_of_usernames(tmp_path, bad):
    _write(tmp_path, rancher_cluster={"admins": bad})
    with pytest.raises(ConfigError, match=r"rancher\.admins.*list of usernames"):
        validate_rancher(tmp_path)


@pytest.mark.parametrize("bad", ["carol", 3, [1, 2], ["carol", 3]])
def test_users_must_be_a_list_of_usernames(tmp_path, bad):
    _write(tmp_path, rancher_cluster={"users": bad})
    with pytest.raises(ConfigError, match=r"rancher\.users.*list of usernames"):
        validate_rancher(tmp_path)


def test_member_under_both_admins_and_users_rejected(tmp_path):
    _write(tmp_path, rancher_cluster={"admins": ["alice", "bob"], "users": ["carol", "bob"]})
    with pytest.raises(ConfigError, match=r"'admins' and 'users'.*bob"):
        validate_rancher(tmp_path)


# ---- unknown keys inside the rancher section ---------------------------------


@pytest.mark.parametrize(
    "rancher_cluster, field",
    [
        ({"admins": ["alice"], "memers": ["bob"]},
         r"unsupported option\(s\): memers"),
        ({"owners": ["alice"]}, r"unsupported option\(s\): owners"),
    ],
)
def test_unknown_cluster_key_rejected(tmp_path, rancher_cluster, field):
    _write(tmp_path, rancher_cluster=rancher_cluster)
    with pytest.raises(ConfigError, match=field):
        validate_rancher(tmp_path)


@pytest.mark.parametrize(
    "rancher_secrets, field",
    [
        ({"url": "https://r.example.com", "token": "t", "urll": "x"},
         r"unsupported option\(s\): urll"),
        ({"user": "x"}, r"unsupported option\(s\): user"),
    ],
)
def test_unknown_secrets_key_rejected(tmp_path, rancher_secrets, field):
    _write(tmp_path, rancher_secrets=rancher_secrets)
    with pytest.raises(ConfigError, match=field):
        validate_rancher(tmp_path)


def test_unknown_rancher_key_refused_through_the_core_preflight(tmp_path):
    """A miscapped `rancher:` key is refused by the core preflight, not silently
    ignored by the plugin."""
    _write(tmp_path, rancher_cluster={"admins": ["alice"], "memers": ["bob"]},
            rancher_secrets={"url": "https://rancher.example.com", "token": "token-x:y"})
    with pytest.raises(ConfigError, match=r"unsupported option\(s\): memers"):
        preflight_validate(Context(root=tmp_path, cfg=None))


# ---- credential types -------------------------------------------------------


@pytest.mark.parametrize("key", ["url", "token"])
@pytest.mark.parametrize("bad", [None, ["not", "a", "string"], 3, ""])
def test_credential_value_must_be_a_string(tmp_path, key, bad):
    _write(tmp_path, rancher_secrets={key: bad})
    with pytest.raises(ConfigError, match=f"rancher\\.{key}.*string"):
        validate_rancher(tmp_path)


# ---- integration through the core preflight ---------------------------------


def test_core_preflight_validates_the_rancher_plugin(tmp_path):
    """The real installed rancher plugin is discovered by core's `plugins.validate`
    and rejects a contradictory `rancher:` section before any core mutation.

    The dev extra installs taloscluster-rancher as an editable wheel, so its
    entry point is present here; a member under both tiers must abort the core
    preflight instead of only failing a late Rancher hook.
    """
    _write(tmp_path, rancher_cluster={"admins": ["alice", "bob"], "users": ["carol", "bob"]},
            rancher_secrets={"url": "https://rancher.example.com", "token": "token-x:y"})
    with pytest.raises(ConfigError, match="'admins' and 'users'"):
        preflight_validate(Context(root=tmp_path, cfg=None))


def test_core_preflight_refuses_null_credentials(tmp_path):
    """An active `rancher:` section with null url/token is refused by the core
    preflight, not left to crash the late HTTP client after core mutation.

    `rancher_configured` sees url/token present (even though null), so the
    plugin validates; the null must be caught here rather than surfacing as
    `NoneType` has no attribute `rstrip` in the late Rancher `Client`.
    """
    _write(tmp_path, rancher_cluster={"admins": ["alice"]},
            rancher_secrets={"url": None, "token": None})
    with pytest.raises(ConfigError, match=r"rancher\.(url|token)\) must be a non-empty string"):
        preflight_validate(Context(root=tmp_path, cfg=None))


def test_core_preflight_refuses_a_non_mapping_section(tmp_path):
    """A `rancher:` section that is not a mapping makes the plugin inactive, but
    core still runs the `validate` hook for a supplied section, so it is refused
    by the preflight instead of silently discarded by activation."""
    _write(tmp_path, rancher_secrets="enabled")
    with pytest.raises(ConfigError, match="rancher must be a YAML mapping"):
        preflight_validate(Context(root=tmp_path, cfg=None))


def test_core_preflight_noops_when_rancher_is_absent(tmp_path):
    """No `rancher:` section anywhere means nothing supplied to validate."""
    _write(tmp_path)
    preflight_validate(Context(root=tmp_path, cfg=None))  # no error

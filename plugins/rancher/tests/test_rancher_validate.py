"""Early plugin-configuration validation.

Core calls each *configured* plugin's `validate` hook in converge's validate
phase, before any cluster mutation; the plugin is only active when both
`rancher:` sections are mappings and secrets carries url + token, so only an
active config is validated there. These cover what the rancher plugin rejects
on that path: admins/users that are not lists of usernames, a member under both
tiers, and secret credential values that are not non-empty strings. A
`rancher:` section that is not a mapping never reaches the hook from core -- it
makes the plugin inactive -- but `validate_rancher` still refuses one on a
direct call, as defense-in-depth.
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


# ---- section shape (direct-call defense-in-depth) --------------------------
# Core's preflight only validates an *active* plugin, and a `rancher:` section
# that is not a mapping makes the plugin inactive, so these never fire on the
# integration path. `validate_rancher` still refuses them on a direct call.


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

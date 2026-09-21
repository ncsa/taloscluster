"""Plugin credentials follow the include contract.

The `rancher:` section is read from the merged configuration -- cluster.yaml,
secrets.yaml or any included file -- so moving url/token out of secrets.yaml
must keep the plugin configured, its validation working and its credentials
loadable, while a section that only secrets.yaml carries stays a credential,
not an activation.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.errors import ConfigError

from taloscluster_rancher.config import Config, rancher_configured, validate_rancher


def _write(root, cluster=None, secrets=None, include_files=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster or {}))
    (root / "secrets.yaml").write_text(yaml.safe_dump(secrets or {}))
    for name, data in (include_files or {}).items():
        (root / name).write_text(yaml.safe_dump(data))


def test_url_and_token_in_cluster_yaml_activate_and_load(tmp_path):
    """The item this pins: credentials in cluster.yaml must not yield a
    `secrets.yaml (rancher): missing 'url'` failure or a silent skip."""
    _write(tmp_path, cluster={
        "name": "testcluster",
        "rancher": {"admins": ["alice"], "url": "https://rancher.example.edu",
                    "token": "token-x:y"},
    })
    assert rancher_configured(tmp_path) is True
    validate_rancher(tmp_path)
    secrets = Config.load_secrets(tmp_path)
    assert secrets.rancher_url == "https://rancher.example.edu"
    assert secrets.rancher_token == "token-x:y"


def test_url_and_token_in_an_included_file_activate_and_load(tmp_path):
    _write(tmp_path, cluster={
        "name": "testcluster", "include": ["creds.yaml"], "rancher": {"admins": ["alice"]},
    }, include_files={
        "creds.yaml": {"rancher": {"url": "https://rancher.example.edu",
                                   "token": "token-x:y"}},
    })
    assert rancher_configured(tmp_path) is True
    secrets = Config.load_secrets(tmp_path)
    assert secrets.rancher_url == "https://rancher.example.edu"
    assert secrets.rancher_token == "token-x:y"


def test_the_scaffolded_split_still_activates(tmp_path):
    """The default layout keeps working: the section in cluster.yaml, the
    credentials in secrets.yaml."""
    _write(tmp_path, cluster={"name": "testcluster", "rancher": {"admins": ["alice"]}},
           secrets={"rancher": {"url": "https://rancher.example.edu", "token": "token-x:y"}})
    assert rancher_configured(tmp_path) is True
    secrets = Config.load_secrets(tmp_path)
    assert secrets.rancher_token == "token-x:y"


def test_a_section_only_in_secrets_yaml_supplies_credentials_without_activating(tmp_path):
    """secrets.yaml holds credentials, not the decision to manage the cluster:
    removing the `rancher:` section from cluster.yaml stops the plugin even
    when the credentials are still parked in secrets.yaml."""
    _write(tmp_path, cluster={"name": "testcluster"},
           secrets={"rancher": {"url": "https://rancher.example.edu", "token": "token-x:y"}})
    assert rancher_configured(tmp_path) is False


def test_null_credentials_in_secrets_never_activate(tmp_path):
    """An explicit null is no value at all in the loader, so a null url/token
    in secrets.yaml leaves the plugin inactive instead of failing late in the
    HTTP client."""
    _write(tmp_path, cluster={"name": "testcluster", "rancher": {"admins": ["alice"]}},
           secrets={"rancher": {"url": None, "token": None}})
    assert rancher_configured(tmp_path) is False


def test_validate_refuses_a_bad_credential_value_wherever_it_lives(tmp_path):
    _write(tmp_path, cluster={
        "name": "testcluster",
        "rancher": {"url": "https://rancher.example.edu", "token": 3},
    })
    with pytest.raises(ConfigError, match=r"rancher\.token.*non-empty string"):
        validate_rancher(tmp_path)


def test_validate_refuses_an_unknown_option_wherever_it_lives(tmp_path):
    _write(tmp_path, cluster={
        "name": "testcluster", "rancher": {"admins": ["alice"], "memers": ["bob"]},
    })
    with pytest.raises(ConfigError, match=r"unsupported option\(s\): memers"):
        validate_rancher(tmp_path)

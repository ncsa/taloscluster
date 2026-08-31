"""Tests for taloscluster.scaffold: `taloscluster init` file creation, permissions,
never-overwrite behaviour, and .gitignore append logic, using tmp_path.
"""

from __future__ import annotations

import os
import stat

import pytest
import yaml

from taloscluster import plugins
from taloscluster.scaffold import GITIGNORE_ENTRIES, init


@pytest.fixture(autouse=True)
def no_installed_plugins(monkeypatch):
    monkeypatch.setattr(plugins, "discover", lambda: [])


def test_init_creates_all_three_files(tmp_path):
    init(tmp_path, name="demo")
    assert (tmp_path / "cluster.yaml").is_file()
    assert (tmp_path / "secrets.yaml").is_file()
    assert (tmp_path / ".gitignore").is_file()


def test_init_creates_missing_directory(tmp_path):
    root = tmp_path / "new" / "cluster"
    init(root, name="demo")
    assert (root / "cluster.yaml").is_file()


def test_init_calls_installed_plugin_initializers(monkeypatch, tmp_path):
    seen = []

    def initialize(root):
        assert (root / "cluster.yaml").is_file()
        assert (root / "secrets.yaml").is_file()
        seen.append(root)

    monkeypatch.setattr(plugins, "initialize", initialize)
    init(tmp_path, name="demo")
    assert seen == [tmp_path]


def test_cluster_yaml_is_valid_and_uses_name(tmp_path):
    init(tmp_path, name="demo")
    d = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    assert d["name"] == "demo"
    # every key load_config requires must be present in the template
    assert d["talos"]["version"]
    assert d["kubernetes"]["version"]
    assert {"count", "flavor", "disk"} <= d["controlplane"].keys()
    assert d["openstack"].keys() >= {"url", "availability_zone", "external_net"}
    assert d["network"].keys() >= {"cidr", "dns", "ntp"}


def test_secrets_yaml_is_valid_and_mode_0600(tmp_path):
    init(tmp_path, name="demo")
    path = tmp_path / "secrets.yaml"
    d = yaml.safe_load(path.read_text())
    assert d["openstack"].keys() >= {"credential_id", "credential_secret"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_init_never_overwrites_existing_files(tmp_path):
    (tmp_path / "cluster.yaml").write_text("name: keepme\n")
    (tmp_path / "secrets.yaml").write_text("openstack: {}\n")
    init(tmp_path, name="demo")
    assert (tmp_path / "cluster.yaml").read_text() == "name: keepme\n"
    assert (tmp_path / "secrets.yaml").read_text() == "openstack: {}\n"


def test_gitignore_covers_secret_and_derived_files(tmp_path):
    init(tmp_path, name="demo")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    for entry in ("secrets.yaml", "talossecrets.yaml", "talosconfig",
                  "kubeconfig"):
        assert entry in lines


def test_gitignore_appends_only_missing_entries(tmp_path):
    (tmp_path / ".gitignore").write_text("secrets.yaml\n*.pyc\n")
    init(tmp_path, name="demo")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count("secrets.yaml") == 1
    assert "*.pyc" in lines
    for entry in GITIGNORE_ENTRIES:
        assert entry in lines


def test_gitignore_untouched_when_complete(tmp_path):
    content = "".join(f"{e}\n" for e in GITIGNORE_ENTRIES)
    (tmp_path / ".gitignore").write_text(content)
    init(tmp_path, name="demo")
    assert (tmp_path / ".gitignore").read_text() == content

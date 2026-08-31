"""Rancher contributes merge-only sections to `taloscluster init`."""

from __future__ import annotations

import yaml

from taloscluster_rancher import init


def test_init_appends_missing_rancher_sections(tmp_path):
    cluster = tmp_path / "cluster.yaml"
    secrets = tmp_path / "secrets.yaml"
    cluster.write_text("name: keepme\n")
    secrets.write_text("openstack: {}\n")

    init(tmp_path)

    assert cluster.read_text().startswith("name: keepme\n")
    assert secrets.read_text().startswith("openstack: {}\n")
    assert yaml.safe_load(cluster.read_text())["rancher"]["admins"] == []
    assert "rancher" in yaml.safe_load(secrets.read_text())


def test_init_does_not_replace_existing_rancher_sections(tmp_path):
    cluster = tmp_path / "cluster.yaml"
    secrets = tmp_path / "secrets.yaml"
    cluster_content = "name: keepme\nrancher:\n  admins: [alice]\n"
    secrets_content = "openstack: {}\nrancher:\n  url: https://existing.example.com\n"
    cluster.write_text(cluster_content)
    secrets.write_text(secrets_content)

    init(tmp_path)

    assert cluster.read_text() == cluster_content
    assert secrets.read_text() == secrets_content

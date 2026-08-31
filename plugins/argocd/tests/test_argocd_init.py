"""ArgoCD contributes merge-only sections to `taloscluster init`."""

from __future__ import annotations

import yaml

from taloscluster_argocd import init


def test_init_appends_missing_argocd_sections(tmp_path):
    cluster = tmp_path / "cluster.yaml"
    secrets = tmp_path / "secrets.yaml"
    cluster.write_text("name: keepme\n")
    secrets.write_text("openstack: {}\n")

    init(tmp_path)

    assert cluster.read_text().startswith("name: keepme\n")
    assert secrets.read_text().startswith("openstack: {}\n")
    assert yaml.safe_load(cluster.read_text())["argocd"]["admins"] == []
    assert "argocd" in yaml.safe_load(secrets.read_text())


def test_init_does_not_replace_existing_argocd_sections(tmp_path):
    cluster = tmp_path / "cluster.yaml"
    secrets = tmp_path / "secrets.yaml"
    cluster_content = "name: keepme\nargocd:\n  admins: [admin@example.com]\n"
    secrets_content = "openstack: {}\nargocd:\n  context: existing\n"
    cluster.write_text(cluster_content)
    secrets.write_text(secrets_content)

    init(tmp_path)

    assert cluster.read_text() == cluster_content
    assert secrets.read_text() == secrets_content

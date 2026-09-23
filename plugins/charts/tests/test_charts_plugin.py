"""Plugin protocol wiring: init scaffolding, configured, validate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from taloscluster.errors import ConfigError

import taloscluster_charts
from taloscluster_charts import reconcile
from taloscluster_charts.config import charts_configured


class _Ctx:
    """Duck-typed stand-in for taloscluster.context.Context."""

    def __init__(self, root: Path):
        self.root = root

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubeconfig"


def _write_cluster(root: Path, doc: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cluster.yaml").write_text(yaml.safe_dump(doc))
    return root


def test_init_scaffolds_all_charts_disabled(tmp_path):
    _write_cluster(tmp_path, {"name": "test", "include": ["secrets.yaml"]})
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"openstack": {}}))
    taloscluster_charts.init(tmp_path)
    charts = yaml.safe_load((tmp_path / "cluster.yaml").read_text())["charts"]
    assert charts["metallb"]["enabled"] is False
    assert charts["traefik"]["enabled"] is False
    assert charts["gateway"]["enabled"] is False
    assert charts["ceph"]["enabled"] is False
    assert charts_configured(tmp_path) is False
    assert taloscluster_charts.configured(_Ctx(tmp_path)) is False
    # the secrets scaffold is a commented example, so the section stays absent
    assert "charts" not in yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert "userKey" in (tmp_path / "secrets.yaml").read_text()


def test_disabled_section_stays_configured_while_something_is_installed(tmp_path, monkeypatch):
    _write_cluster(tmp_path, {"name": "test", "charts": {"metallb": {"enabled": False}}})
    (tmp_path / "kubeconfig").write_text("")
    ctx = _Ctx(tmp_path)
    assert taloscluster_charts.configured(ctx) is False
    monkeypatch.setattr(
        reconcile.helm,
        "release",
        lambda kubeconfig, name, namespace: {
            "name": name, "status": "deployed", "chart": f"{name}-1.0.0"},
    )
    assert taloscluster_charts.configured(ctx) is True


def test_init_leaves_existing_section_alone(tmp_path):
    _write_cluster(tmp_path, {"name": "test", "charts": {"metallb": {"enabled": True}}})
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"charts": {"ceph": {
        "userID": "admin", "userKey": "k"}}}))
    taloscluster_charts.init(tmp_path)
    charts = yaml.safe_load((tmp_path / "cluster.yaml").read_text())["charts"]
    assert charts == {"metallb": {"enabled": True}}
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert secrets["charts"]["ceph"] == {"userID": "admin", "userKey": "k"}


def test_init_does_not_repeat_the_commented_secrets_example(tmp_path):
    """The secrets scaffold is comments only, so no parsed `charts:` key ever
    appears; the commented header is what a re-run must recognise."""
    _write_cluster(tmp_path, {"name": "test", "include": ["secrets.yaml"]})
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"openstack": {}}))
    taloscluster_charts.init(tmp_path)
    taloscluster_charts.init(tmp_path)
    assert (tmp_path / "secrets.yaml").read_text().count("# charts:") == 1


def test_validate_rejects_bad_section(tmp_path):
    _write_cluster(tmp_path, {"name": "test", "charts": {"metallb": {"bogus": 1}}})
    with pytest.raises(ConfigError):
        taloscluster_charts.validate(tmp_path, None)


def test_validate_allows_absent_section(tmp_path):
    _write_cluster(tmp_path, {"name": "test"})
    taloscluster_charts.validate(tmp_path, None)


def test_protocol_is_complete():
    for hook in ("init", "validate", "configured", "converge", "destroy", "status", "check"):
        assert callable(getattr(taloscluster_charts, hook, None)), hook
    assert taloscluster_charts.CONFIG_SECTIONS == ("charts",)

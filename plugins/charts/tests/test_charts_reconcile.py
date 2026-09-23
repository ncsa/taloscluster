"""Converge/check decision logic, with helm and kubectl stubbed out."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from taloscluster import output
from taloscluster.errors import ConfigError, ReconcileError

from taloscluster_charts import reconcile
from taloscluster_charts.config import Config


@dataclass
class Ctx:
    """Duck-typed stand-in for taloscluster.context.Context."""

    root: Path
    ingress: dict = field(default_factory=dict)
    cfg: object = field(default_factory=lambda: SimpleNamespace(name="test"))

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubeconfig"


def _root(tmp_path, charts) -> Path:
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump({"name": "t", "charts": charts}))
    return tmp_path


def _pool_ctx(tmp_path, charts) -> Ctx:
    return Ctx(root=_root(tmp_path, charts), ingress={"metallb": ["192.0.2.190-192.0.2.199"]})


@dataclass
class FakeHelm:
    """Replaces taloscluster_charts.helm for converge decision tests."""

    installed: dict[str, str] = field(default_factory=dict)  # release -> chart version
    statuses: dict[str, str] = field(default_factory=dict)  # release -> helm status override
    latest: dict[str, str] = field(default_factory=dict)
    values: dict[str, dict] = field(default_factory=dict)  # release -> last applied values
    upgrades: list = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)  # (op, release) in call order
    lookups: list = field(default_factory=list)  # chart names asked for their latest version
    pulled: list = field(default_factory=list)  # (release, chart) pairs installed
    uninstalls: list = field(default_factory=list)

    def release(self, kubeconfig, name, namespace):
        if name not in self.installed:
            return None
        return {
            "name": name,
            "status": self.statuses.get(name, "deployed"),
            "chart": f"{name}-{self.installed[name]}",
        }

    def latest_version(self, chart, repo):
        self.lookups.append(chart)
        return self.latest.get(chart)

    def get_values(self, kubeconfig, name, namespace):
        return self.values.get(name, {})

    def upgrade_install(self, kubeconfig, name, chart, repo, namespace, version, values_yaml):
        self.upgrades.append((name, values_yaml))
        self.pulled.append((name, chart))
        self.events.append(("upgrade", name))
        self.values[name] = yaml.safe_load(values_yaml)

    def uninstall(self, kubeconfig, name, namespace):
        self.uninstalls.append(name)
        self.events.append(("uninstall", name))


@pytest.fixture
def fake_helm(monkeypatch):
    fake = FakeHelm()
    for attr in ("release", "latest_version", "upgrade_install", "uninstall", "get_values"):
        monkeypatch.setattr(reconcile.helm, attr, getattr(fake, attr))
    return fake


@pytest.fixture
def no_kube(monkeypatch):
    """Stub kubectl; by default everything matches (nothing to apply)."""
    calls = {"apply": [], "delete": [], "matches": True}
    monkeypatch.setattr(reconcile.kube, "matches", lambda *a, **k: calls["matches"])
    monkeypatch.setattr(
        reconcile.kube, "apply", lambda root, target, **k: calls["apply"].append(target)
    )
    monkeypatch.setattr(
        reconcile.kube, "delete", lambda root, target, **k: calls["delete"].append(target)
    )
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: False)
    monkeypatch.setattr(reconcile.kube, "wait_deployment_available", lambda *a, **k: True)
    monkeypatch.setattr(reconcile, "preflight_tools", lambda tools=None: None)
    return calls


def test_first_install(tmp_path, fake_helm, no_kube):
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "installed"
    assert fake_helm.upgrades and fake_helm.upgrades[0][0] == "metallb"


def test_pool_applied_when_drifted(tmp_path, fake_helm, no_kube):
    no_kube["matches"] = False
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    # namespace + pool resources both drift when matches is False
    assert no_kube["apply"] == ["-", "-"]


def test_latest_up_to_date(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {  # matches the merged common values
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "up_to_date"
    assert not fake_helm.upgrades


@pytest.fixture(autouse=True)
def _reset_dry_run():
    output.set_dry_run(False)
    yield
    output.set_dry_run(False)


@pytest.fixture
def dry():
    output.set_dry_run(True)


def test_plan_hides_values_when_up_to_date(tmp_path, fake_helm, no_kube, dry, capsys):
    (tmp_path / "kubeconfig").write_text("")
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    out = capsys.readouterr().out
    assert "metallb: chart 0.14.9 up to date" in out
    assert "metallb: values" not in out


def test_plan_shows_values_when_changing(tmp_path, fake_helm, no_kube, dry, capsys):
    (tmp_path / "kubeconfig").write_text("")
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {"stale": True}
    reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    out = capsys.readouterr().out
    assert "metallb: values" in out
    assert "frrk8s" in out


def test_ceph_pulls_the_csi_charts_not_the_entry_name(tmp_path, fake_helm, no_kube):
    charts = {"ceph": {
        "clusterID": "2f6a1c0e-0000-4000-8000-000000000000",
        "monitors": ["192.0.2.11:6789"],
        "rbd": True,
        "fs": True,
    }}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["ceph"]["charts"] == {
        "ceph-csi-rbd": "installed", "ceph-csi-cephfs": "installed",
    }
    assert fake_helm.lookups == ["ceph-csi-rbd", "ceph-csi-cephfs"]
    assert fake_helm.pulled == [
        ("ceph-csi-rbd", "ceph-csi-rbd"), ("ceph-csi-cephfs", "ceph-csi-cephfs"),
    ]


def test_ceph_charts_get_their_own_storage_class(tmp_path, fake_helm, no_kube):
    charts = {"ceph": {
        "clusterID": "fsid",
        "monitors": ["192.0.2.11:6789"],
        "rbd": {"pool": "kubernetes"},
        "fs": True,
        "values": {"logLevel": 3},
    }}
    reconcile.converge(_pool_ctx(tmp_path, charts))
    values = dict(fake_helm.upgrades)
    rbd, fs = yaml.safe_load(values["ceph-csi-rbd"]), yaml.safe_load(values["ceph-csi-cephfs"])
    assert rbd["storageClass"]["pool"] == "kubernetes" and rbd["storageClass"]["create"] is True
    assert "storageClass" not in fs
    assert rbd["logLevel"] == fs["logLevel"] == 3
    assert rbd["csiConfig"] == fs["csiConfig"]


def test_plan_never_prints_the_ceph_key(tmp_path, fake_helm, no_kube, dry, capsys):
    (tmp_path / "kubeconfig").write_text("")
    charts = {"ceph": {
        "clusterID": "2f6a1c0e-0000-4000-8000-000000000000",
        "monitors": ["192.0.2.11:6789"],
        "rbd": True,
        "userID": "kubernetes",
        "userKey": "AQC0secretkey==",
    }}
    reconcile.converge(_pool_ctx(tmp_path, charts))
    out = capsys.readouterr().out
    assert "kubectl apply ceph csi secrets" in out
    assert "name: csi-rbd-secret" in out
    assert "AQC0secretkey" not in out
    assert "userKey: REDACTED" in out


def test_values_change_triggers_upgrade(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {"stale": True}
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "upgraded"


def test_latest_upgrades_when_newer_exists(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.15.0"
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "upgraded"


def test_pinned_version_mismatch_upgrades(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"version": "0.13.0"}}))
    assert result["entries"]["metallb"]["action"] == "upgraded"


@pytest.mark.parametrize("pinned", ["1.21.2", "v1.21.2"])
def test_pinned_version_matches_installed_v_tag(tmp_path, fake_helm, no_kube, pinned):
    # cert-manager tags its charts v1.21.2 while `--version 1.21.2` resolves to
    # it: the pinned compare must not read that as drift and re-upgrade
    fake_helm.installed["cert-manager"] = "v1.21.2"
    fake_helm.values["cert-manager"] = {"crds": {"enabled": True}}
    result = reconcile.converge(_pool_ctx(tmp_path, {"cert-manager": {"version": pinned}}))
    assert result["entries"]["cert-manager"]["action"] == "up_to_date"
    assert not fake_helm.upgrades


def test_failed_release_upgrades_in_place(tmp_path, fake_helm, no_kube):
    # helm accepts an upgrade over a failed release; version and values match,
    # so the non-deployed status is the only trigger
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    fake_helm.statuses["metallb"] = "failed"
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "upgraded"
    assert fake_helm.upgrades[0][0] == "metallb"
    assert not fake_helm.uninstalls


@pytest.mark.parametrize(
    "status", ["pending-install", "pending-upgrade", "pending-rollback", "uninstalling"]
)
def test_stuck_release_is_cleared_then_installed_fresh(tmp_path, fake_helm, no_kube, status):
    # helm refuses to upgrade over a pending-*/uninstalling release ("another
    # operation is in progress"), so it is uninstalled first and the
    # upgrade --install falls through to a fresh install
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.statuses["metallb"] = status
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result["entries"]["metallb"]["action"] == "upgraded"
    assert fake_helm.events == [("uninstall", "metallb"), ("upgrade", "metallb")]


def test_disabled_entry_uninstalls(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"enabled": False}}))
    assert result["entries"]["metallb"]["action"] == "removed"
    assert fake_helm.uninstalls == ["metallb"]


def test_disabled_absent_entry_is_a_noop(tmp_path, fake_helm, no_kube):
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"enabled": False}}))
    assert result["entries"]["metallb"]["action"] == "absent"
    assert not fake_helm.uninstalls


def test_disabled_metallb_deletes_pool_before_uninstall(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("metallb",), exists=True)
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"enabled": False}}))
    assert result["entries"]["metallb"]["action"] == "removed"
    assert log == ["delete metallb pool resources", "uninstall metallb"]


def _stub(monkeypatch, log, releases=(), exists=True):
    """Wire helm/kubectl stubs that append human-readable steps to `log`."""

    def release(kubeconfig, name, namespace):
        if name in releases:
            return {"name": name, "status": "deployed", "chart": f"{name}-1.0.0"}
        return None

    monkeypatch.setattr(reconcile, "preflight_tools", lambda tools=None: None)
    monkeypatch.setattr(reconcile.helm, "release", release)
    monkeypatch.setattr(
        reconcile.helm, "uninstall", lambda kc, name, ns: log.append(f"uninstall {name}")
    )
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: exists)
    monkeypatch.setattr(
        reconcile.kube,
        "delete",
        lambda root, target, label="", input=None: log.append(f"delete {label}"),
    )


def log_stub(monkeypatch, exists=True, matches=True):
    """Stub the read-only kubectl probes `check` uses."""
    monkeypatch.setattr(reconcile, "preflight_tools", lambda tools=None: None)
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: exists)
    monkeypatch.setattr(reconcile.kube, "matches", lambda root, target, **k: matches)


def test_destroy_order(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("traefik", "metallb"), exists=True)
    charts = {
        "gateway": {"version": "v1.6.2"},
        "metallb": {},
        "traefik": {},
    }
    reconcile.destroy(_pool_ctx(tmp_path, charts))
    assert log == [
        "uninstall traefik",
        "delete namespace traefik",
        "delete metallb pool resources",  # before the chart: helm uninstall removes the CRDs
        "uninstall metallb",
        "delete namespace metallb-system",
        "delete https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.2"
        "/standard-install.yaml",
    ]


def test_destroy_skips_pool_when_chart_gone(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=(), exists=False)
    reconcile.destroy(_pool_ctx(tmp_path, {"metallb": {}}))
    assert log == ["delete namespace metallb-system"]


def test_destroy_skips_manifest_when_gone(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=(), exists=False)
    reconcile.destroy(_pool_ctx(tmp_path, {"gateway": {"version": "v1.6.2"}}))
    assert log == []


def test_destroy_resolves_latest_gateway(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=(), exists=True)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.7.0")
    reconcile.destroy(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert log == [
        "delete https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.7.0"
        "/standard-install.yaml"
    ]


def test_destroy_skips_unresolvable_latest_gateway(tmp_path, monkeypatch, capsys):
    # destroy must take the rest of the cluster down even when a `latest`
    # manifest cannot be named
    log = []
    _stub(monkeypatch, log, releases=("metallb",), exists=True)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    charts = {"gateway": {"enabled": False, "version": "latest"}, "metallb": {}}
    reconcile.destroy(_pool_ctx(tmp_path, charts))
    assert log == [
        "delete metallb pool resources",  # before the chart: helm uninstall removes the CRDs
        "uninstall metallb",
        "delete namespace metallb-system",
    ]
    assert "cannot resolve the latest release" in capsys.readouterr().err


def test_status_tolerates_unresolvable_latest_gateway(tmp_path, fake_helm, no_kube, monkeypatch):
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    report = reconcile.status(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert report["entries"]["gateway"] == {"kind": "manifest", "enabled": True, "applied": False}


def test_traefik_without_gateway_warns(tmp_path, fake_helm, no_kube, capsys):
    charts = {
        "gateway": {"enabled": False, "version": "v1.3.1"},
        "traefik": {},
    }
    reconcile.converge(_pool_ctx(tmp_path, charts))
    assert "Gateway API is already installed" in capsys.readouterr().err


def test_traefik_values_carry_pool_ip(tmp_path, fake_helm, no_kube):
    ctx = _pool_ctx(tmp_path, {"traefik": {}})
    reconcile.converge(ctx)
    values = yaml.safe_load(fake_helm.upgrades[0][-1])
    assert values["service"]["loadBalancerIP"] == "192.0.2.190"


def test_check_reports_upgrade_available(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.15.0"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is True
    assert report["entries"]["metallb"] == "ok"
    assert report["upgrade_available"] == {"metallb": "0.15.0"}


def test_check_fails_on_missing_release(tmp_path, fake_helm, no_kube):
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is False
    assert report["entries"]["metallb"] == "not_installed"


@pytest.mark.parametrize("status", ["failed", "pending-upgrade"])
def test_check_fails_on_unhealthy_release(tmp_path, fake_helm, no_kube, status):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.statuses["metallb"] = status
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is False
    assert report["entries"]["metallb"] == "drifted"


def test_check_ok_when_version_and_values_match(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is True
    assert report["entries"]["metallb"] == "ok"


def test_check_fails_on_pinned_version_drift(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {"version": "0.13.0"}}))
    assert report["ok"] is False
    assert report["entries"]["metallb"] == "drifted"


def test_check_ignores_a_leading_v_on_a_pinned_version(tmp_path, fake_helm, no_kube):
    fake_helm.installed["cert-manager"] = "v1.21.2"
    fake_helm.values["cert-manager"] = {"crds": {"enabled": True}}
    report = reconcile.check(_pool_ctx(tmp_path, {"cert-manager": {"version": "1.21.2"}}))
    assert report["ok"] is True
    assert report["entries"]["cert-manager"] == "ok"


def test_check_fails_on_values_drift(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.latest["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {"stale": True}
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is False
    assert report["entries"]["metallb"] == "drifted"


def test_check_fails_when_pool_drifted(tmp_path, fake_helm, no_kube):
    fake_helm.installed["metallb"] = "0.14.9"
    fake_helm.values["metallb"] = {
        "speaker": {"frr": {"enabled": False}},
        "frrk8s": {"enabled": False},
    }
    no_kube["matches"] = False
    report = reconcile.check(_pool_ctx(tmp_path, {"metallb": {}}))
    assert report["ok"] is False
    assert report["entries"]["metallb"] == "drifted"


def test_check_gateway_not_installed(tmp_path, monkeypatch, no_kube):
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.6.2")
    report = reconcile.check(_pool_ctx(tmp_path, {"gateway": {"version": "v1.6.2"}}))
    assert report["ok"] is False
    assert report["entries"]["gateway"] == "not_installed"
    assert report["upgrade_available"] == {}


def test_check_reports_newer_gateway_release(tmp_path, monkeypatch):
    log_stub(monkeypatch, exists=True, matches=True)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.7.0")
    report = reconcile.check(_pool_ctx(tmp_path, {"gateway": {"version": "v1.6.2"}}))
    assert report["ok"] is True
    assert report["entries"]["gateway"] == "ok"
    assert report["upgrade_available"] == {"gateway": "v1.7.0"}


def test_check_current_gateway_release_has_no_note(tmp_path, monkeypatch):
    log_stub(monkeypatch, exists=True, matches=True)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.6.2")
    report = reconcile.check(_pool_ctx(tmp_path, {"gateway": {"version": "v1.6.2"}}))
    assert report["upgrade_available"] == {}


def test_check_latest_gateway_tracks_upstream_without_note(tmp_path, monkeypatch):
    log_stub(monkeypatch, exists=True, matches=True)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.7.0")
    report = reconcile.check(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert report["ok"] is True
    assert report["entries"]["gateway"] == "ok"
    assert report["upgrade_available"] == {}  # a new release shows up as drift instead


def test_check_unresolvable_latest_gateway_reports_not_installed(tmp_path, monkeypatch, no_kube):
    # a lookup failure must not fail check itself (upstream.py's contract)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    report = reconcile.check(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert report["ok"] is False
    assert report["entries"]["gateway"] == "not_installed"


def test_check_unresolvable_latest_gateway_disabled_is_absent(tmp_path, monkeypatch, no_kube):
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    charts = {"gateway": {"enabled": False, "version": "latest"}}
    report = reconcile.check(_pool_ctx(tmp_path, charts))
    assert report["ok"] is True
    assert report["entries"]["gateway"] == "absent"


def test_converge_resolves_latest_gateway(tmp_path, fake_helm, no_kube, monkeypatch):
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.7.0")
    reconcile.converge(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert no_kube["apply"] == [
        "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.7.0"
        "/standard-install.yaml"
    ]


def test_converge_latest_gateway_unresolvable(tmp_path, fake_helm, no_kube, monkeypatch, capsys):
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    charts = {"gateway": {"version": "latest"}}
    with pytest.raises(ReconcileError, match="gateway failed to converge"):
        reconcile.converge(_pool_ctx(tmp_path, charts))
    assert "cannot resolve the latest release" in capsys.readouterr().err


def test_converge_continues_past_a_failed_entry(tmp_path, fake_helm, no_kube, monkeypatch, capsys):
    # one unresolvable entry must not stop the others; the run still fails
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    charts = {"gateway": {"version": "latest"}, "metallb": {}}
    with pytest.raises(ReconcileError, match="gateway failed to converge"):
        reconcile.converge(_pool_ctx(tmp_path, charts))
    assert fake_helm.upgrades and fake_helm.upgrades[0][0] == "metallb"
    assert "cannot resolve the latest release" in capsys.readouterr().err


def test_converge_continues_past_a_failed_chart(tmp_path, fake_helm, no_kube, monkeypatch):
    def boom(kubeconfig, name, chart, repo, namespace, version, values_yaml):
        if name == "traefik":
            raise ReconcileError("helm upgrade --install traefik failed: boom")
        fake_helm.upgrade_install(kubeconfig, name, chart, repo, namespace, version, values_yaml)

    monkeypatch.setattr(reconcile.helm, "upgrade_install", boom)
    with pytest.raises(ReconcileError, match="traefik failed to converge"):
        reconcile.converge(_pool_ctx(tmp_path, {"traefik": {}, "metallb": {}}))
    assert ("metallb", "metallb") in fake_helm.pulled


def test_converge_skips_unresolvable_manifest_when_disabled(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # the scaffolded default: a disabled `version: latest` gateway must not
    # need the GitHub releases API at all
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    charts = {"gateway": {"enabled": False, "version": "latest"}, "metallb": {}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["gateway"] == {"action": "absent", "kind": "manifest"}
    assert result["entries"]["metallb"]["action"] == "installed"


def test_converge_applies_custom_manifest_without_upstream(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # explicit urls have no {version} template and never touch upstream
    def no_lookup():
        raise AssertionError("an explicit-url manifest entry looked up upstream")

    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", no_lookup)
    charts = {"platform": {"manifest": "https://example.com/platform.yaml"}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["platform"]["action"] == "applied"
    assert no_kube["apply"] == ["https://example.com/platform.yaml"]


def test_config_load_error_surfaces(tmp_path, fake_helm, no_kube):
    _root(tmp_path, {"mystery": {}})
    with pytest.raises(ConfigError):
        reconcile.converge(Ctx(root=tmp_path))


def test_ordered_puts_gateway_first(tmp_path):
    charts = {
        "traefik": {},
        "gateway": {"version": "v1"},
        "metallb": {},
        "sealed-secrets": {},
        "cert-manager": {"email": "a@b"},
        "ceph": {"enabled": False},
        "nfs": {"enabled": False},
        "zcustom": {"repo": "https://x"},
    }
    cfg = Config.load(_root(tmp_path, charts))
    names = [entry.name for entry in reconcile._ordered(cfg.entries)]
    assert names == [
        "gateway", "metallb", "traefik", "sealed-secrets", "cert-manager",
        "ceph", "nfs", "zcustom",
    ]


def test_nfs_values_carry_storage_classes(tmp_path, fake_helm, no_kube):
    charts = {"nfs": {"storageClasses": [
        {"name": "nfs-data", "server": "nfs.example.edu", "share": "/exports/data"},
    ]}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["nfs"]["action"] == "installed"
    values = yaml.safe_load(fake_helm.upgrades[0][1])
    assert values["driver"]["mountPermissions"] == "0777"
    sc = values["storageClasses"][0]
    assert sc["name"] == "nfs-data"
    assert sc["parameters"]["subDir"].startswith("test/")
    assert sc["parameters"]["server"] == "nfs.example.edu"


def test_cert_manager_issuers_applied_after_chart(tmp_path, fake_helm, no_kube):
    charts = {"cert-manager": {"email": "a@b", "prod": True}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["cert-manager"]["action"] == "installed"
    assert "-" in no_kube["apply"]  # ClusterIssuers via stdin after the chart


def test_cert_manager_issuers_deleted_before_uninstall(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("cert-manager",), exists=True)
    charts = {"cert-manager": {"email": "a@b", "prod": True, "enabled": False}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["cert-manager"]["action"] == "removed"
    assert log == [
        "delete cert-manager ClusterIssuers",  # webhook must still be serving
        "uninstall cert-manager",
    ]


CERT_MANAGER_PROD_VALUES = {  # the plugin's common values for a prod-issuer entry
    "crds": {"enabled": True},
    "ingressShim": {
        "defaultIssuerKind": "ClusterIssuer",
        "defaultIssuerGroup": "cert-manager.io",
        "defaultIssuerName": "letsencrypt-prod",
    },
}


def test_check_fails_when_issuers_drifted(tmp_path, fake_helm, no_kube):
    fake_helm.installed["cert-manager"] = "1.21.2"
    fake_helm.values["cert-manager"] = CERT_MANAGER_PROD_VALUES
    no_kube["matches"] = False
    charts = {"cert-manager": {"email": "a@b", "prod": True}}
    report = reconcile.check(_pool_ctx(tmp_path, charts))
    assert report["entries"]["cert-manager"] == "drifted"


def test_check_ignores_issuers_when_none_enabled(tmp_path, fake_helm, no_kube):
    fake_helm.installed["cert-manager"] = "1.21.2"
    fake_helm.values["cert-manager"] = {"crds": {"enabled": True}}
    charts = {"cert-manager": {"email": "a@b"}}
    report = reconcile.check(_pool_ctx(tmp_path, charts))
    assert report["entries"]["cert-manager"] == "ok"


CEPH = {
    "clusterID": "2f6a1c0e-0000-4000-8000-000000000000",
    "monitors": ["192.0.2.11:6789"],
    "rbd": True,
}
CEPH_RBD_VALUES = {  # the values converge would hand the ceph-csi-rbd chart
    "csiConfig": [{"clusterID": CEPH["clusterID"], "monitors": CEPH["monitors"]}],
}


def test_check_ok_when_ceph_matches(tmp_path, fake_helm, no_kube):
    fake_helm.installed["ceph-csi-rbd"] = "3.0.0"
    fake_helm.values["ceph-csi-rbd"] = CEPH_RBD_VALUES
    report = reconcile.check(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert report["ok"] is True
    assert report["entries"]["ceph"] == "ok"


def test_check_fails_when_ceph_version_drifted(tmp_path, fake_helm, no_kube):
    fake_helm.installed["ceph-csi-rbd"] = "3.0.1"
    fake_helm.values["ceph-csi-rbd"] = CEPH_RBD_VALUES
    report = reconcile.check(_pool_ctx(tmp_path, {"ceph": {**CEPH, "version": "3.0.0"}}))
    assert report["ok"] is False
    assert report["entries"]["ceph"] == "drifted"


def test_check_fails_when_ceph_values_drifted(tmp_path, fake_helm, no_kube):
    fake_helm.installed["ceph-csi-rbd"] = "3.0.0"
    fake_helm.values["ceph-csi-rbd"] = {"stale": True}
    report = reconcile.check(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert report["ok"] is False
    assert report["entries"]["ceph"] == "drifted"


def test_check_reports_ceph_not_installed(tmp_path, fake_helm, no_kube):
    report = reconcile.check(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert report["ok"] is False
    assert report["entries"]["ceph"] == "not_installed"

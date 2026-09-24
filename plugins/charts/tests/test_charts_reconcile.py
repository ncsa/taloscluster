"""Converge/check decision logic, with helm and kubectl stubbed out."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from taloscluster import output
from taloscluster.errors import ConfigError, PreflightError, ReconcileError

from taloscluster_charts import reconcile
from taloscluster_charts.charts import MANAGED_BY_KEY, MANAGED_BY_VALUE
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
    """Stub kubectl; by default the cluster is empty and everything matches."""
    calls = {"apply": [], "delete": [], "matches": True}
    monkeypatch.setattr(reconcile.kube, "matches", lambda *a, **k: calls["matches"])
    monkeypatch.setattr(
        reconcile.kube, "apply", lambda root, target, **k: calls["apply"].append(target)
    )
    monkeypatch.setattr(
        reconcile.kube, "delete", lambda root, target, **k: calls["delete"].append(target)
    )
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: False)
    monkeypatch.setattr(reconcile.kube, "namespace_labels", lambda root, name: None)
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


def test_plan_defers_before_preflight(tmp_path, fake_helm, no_kube, dry, monkeypatch):
    """A plan before bootstrap reports the charts as deferred even without helm."""

    def fail_preflight(tools=None):
        raise PreflightError("required command(s) not found: helm kubectl")

    monkeypatch.setattr(reconcile, "preflight_tools", fail_preflight)
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {}}))
    assert result == {
        "deferred": True,
        "reason": "this cluster has no kubeconfig yet (it is written at bootstrap)",
    }


def test_still_installed_needs_a_kubeconfig(tmp_path):
    ctx = _pool_ctx(tmp_path, {"metallb": {"enabled": False}})
    assert reconcile.still_installed(ctx) is False


def test_still_installed_sees_a_disabled_release(tmp_path, fake_helm):
    (tmp_path / "kubeconfig").write_text("")
    ctx = _pool_ctx(tmp_path, {"metallb": {"enabled": False}})
    assert reconcile.still_installed(ctx) is False
    fake_helm.installed["metallb"] = "0.14.9"
    assert reconcile.still_installed(ctx) is True


def test_still_installed_sees_a_ceph_chart(tmp_path, fake_helm):
    (tmp_path / "kubeconfig").write_text("")
    ctx = _pool_ctx(tmp_path, {"ceph": {"enabled": False, "rbd": True, "fs": True}})
    fake_helm.installed["ceph-csi-rbd"] = "3.0.0"
    assert reconcile.still_installed(ctx) is True


def test_still_installed_sees_a_turned_off_ceph_chart(tmp_path, fake_helm):
    # the fs flag no longer names the installed cephfs release, but the
    # plugin must stay configured until converge has removed it
    (tmp_path / "kubeconfig").write_text("")
    ctx = _pool_ctx(tmp_path, {"ceph": dict(CEPH, enabled=False)})
    fake_helm.installed["ceph-csi-cephfs"] = "3.0.0"
    assert reconcile.still_installed(ctx) is True


def test_still_installed_sees_an_applied_manifest(tmp_path, monkeypatch):
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: True)
    # a `latest` gateway cannot be named without upstream, and reads as gone
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: None)
    ctx = _pool_ctx(tmp_path, {"gateway": {"enabled": False, "version": "latest"}})
    assert reconcile.still_installed(ctx) is False
    pinned = _pool_ctx(tmp_path, {"gateway": {"enabled": False, "version": "v1.6.2"}})
    assert reconcile.still_installed(pinned) is True


def test_still_installed_survives_a_hung_api(tmp_path, monkeypatch):
    """A kube-api that accepts TCP but never answers surfaces from the probe
    as the ReconcileError still_installed catches (reading as nothing left),
    not as the raw TimeoutExpired that used to escape it."""

    def hung(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 30))

    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.setattr(reconcile.kube.kubectl, "_run", hung)
    ctx = _pool_ctx(tmp_path, {"gateway": {"enabled": False, "version": "v1.6.2"}})
    assert reconcile.still_installed(ctx) is False


def test_still_installed_tolerates_a_missing_helm(tmp_path, monkeypatch):
    (tmp_path / "kubeconfig").write_text("")

    def no_helm(kubeconfig, name, namespace):
        raise FileNotFoundError("helm")

    monkeypatch.setattr(reconcile.helm, "release", no_helm)
    ctx = _pool_ctx(tmp_path, {"metallb": {"enabled": False}})
    assert reconcile.still_installed(ctx) is False


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


def test_converge_uninstalls_a_ceph_chart_whose_flag_was_turned_off(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # both charts were installed; turning fs off must uninstall the cephfs
    # release and remove its namespace, not leave them behind orphaned
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(k.get("label") or target),
    )
    fake_helm.installed["ceph-csi-rbd"] = "3.0.0"
    fake_helm.installed["ceph-csi-cephfs"] = "3.0.0"
    fake_helm.values["ceph-csi-rbd"] = CEPH_RBD_VALUES
    result = reconcile.converge(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert fake_helm.uninstalls == ["ceph-csi-cephfs"]
    assert not fake_helm.upgrades  # the wanted rbd chart is left up to date
    assert deleted == ["namespace ceph-csi-cephfs"]
    assert result["entries"]["ceph"]["charts"] == {
        "ceph-csi-cephfs": "removed", "ceph-csi-rbd": "up_to_date",
    }


def test_converge_takes_the_turned_off_chart_s_secret_with_it(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # the csi Secret of the chart whose flag was turned off goes too, while
    # the wanted chart's Secret is left in place
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: True)
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(yaml.safe_load(k["input"])["metadata"]["name"]),
    )
    fake_helm.installed["ceph-csi-cephfs"] = "3.0.0"
    reconcile.converge(_pool_ctx(tmp_path, {
        "ceph": dict(CEPH, userID="kubernetes", userKey="AQC0secretkey==")
    }))
    assert fake_helm.uninstalls == ["ceph-csi-cephfs"]
    assert deleted == ["csi-cephfs-secret", "ceph-csi-cephfs"]


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
    assert log == [
        "delete metallb pool resources",
        "uninstall metallb",
        "delete namespace metallb-system",
    ]


def _stub(monkeypatch, log, releases=(), exists=True, managed=True):
    """Wire helm/kubectl stubs that append human-readable steps to `log`.

    `managed` controls whether the live namespaces carry the plugin's
    managed-by label, the ownership marker deletes go through.
    """

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
    monkeypatch.setattr(
        reconcile.kube,
        "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE} if managed else {},
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
        "delete metallb pool resources",  # before the chart: helm uninstall removes the CRDs
        "uninstall metallb",
        "delete https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.2"
        "/standard-install.yaml",
        "delete namespace traefik",
        "delete namespace metallb-system",
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


def test_destroy_leaves_a_namespace_it_did_not_create(tmp_path, monkeypatch):
    # a namespace carrying no managed-by label -- one that pre-existed or was
    # created by something else -- is never deleted
    log = []
    _stub(monkeypatch, log, releases=("metallb",), exists=True, managed=False)
    reconcile.destroy(_pool_ctx(tmp_path, {"metallb": {}}))
    assert log == ["delete metallb pool resources", "uninstall metallb"]


def test_destroy_never_deletes_the_clusters_own_namespaces(tmp_path, monkeypatch, capsys):
    # the api server refuses a delete of default/kube-system/kube-public; the
    # refusal must not abort the entries after it either
    log = []
    _stub(monkeypatch, log, releases=("aaa", "zzz"))
    charts = {
        "aaa": {"repo": "https://charts.example.com", "namespace": "kube-system"},
        "zzz": {"repo": "https://charts.example.com", "namespace": "zzz-ns"},
    }
    reconcile.destroy(_pool_ctx(tmp_path, charts))
    assert log == [
        "uninstall zzz",
        "uninstall aaa",
        "delete namespace zzz-ns",
    ]
    assert "delete namespace kube-system" not in log
    assert "one of the cluster's own" in capsys.readouterr().err


def test_destroy_leaves_an_unlabelled_ceph_namespace(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("ceph-csi-rbd",), exists=True, managed=False)
    reconcile.destroy(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert log == ["uninstall ceph-csi-rbd"]


def test_destroy_removes_a_ceph_chart_whose_flag_was_turned_off(tmp_path, monkeypatch):
    # destroy probes both charts: the release of a flag turned off earlier
    # goes with its namespace, same as the wanted one
    log = []
    _stub(monkeypatch, log, releases=("ceph-csi-rbd", "ceph-csi-cephfs"), exists=True)
    reconcile.destroy(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert log == [
        "uninstall ceph-csi-cephfs",
        "uninstall ceph-csi-rbd",
        "delete namespace ceph-csi-rbd",
        "delete namespace ceph-csi-cephfs",
    ]


class LiveCluster:
    """A kube stand-in whose apply really merges labels onto live namespaces.

    `namespace_labels` reads the same live state `apply` writes, the way
    kubectl does, so a test can start from a pre-existing, unlabelled
    namespace and watch whether converge stamps the managed-by marker a
    later delete trusts -- the static stubs cannot see that.
    """

    def __init__(self):
        self.namespaces: dict[str, dict[str, str]] = {}
        self.applied: list[str] = []
        self.deleted: list[str] = []

    def exists(self, root, target, *, input=None):
        return yaml.safe_load(input)["metadata"]["name"] in self.namespaces

    def matches(self, root, target, *, input=None):
        doc = yaml.safe_load(input)
        live = self.namespaces.get(doc["metadata"]["name"])
        if live is None:
            return False
        wanted = doc["metadata"].get("labels") or {}
        return all(live.get(key) == value for key, value in wanted.items())

    def apply(self, root, target, *, label="", input=None):
        doc = yaml.safe_load(input)
        name = doc["metadata"]["name"]
        self.applied.append(name)
        self.namespaces.setdefault(name, {}).update(doc["metadata"].get("labels") or {})

    def delete(self, root, target, *, label="", input=None):
        name = yaml.safe_load(input)["metadata"]["name"]
        self.namespaces.pop(name, None)
        self.deleted.append(label or target)

    def namespace_labels(self, root, name):
        live = self.namespaces.get(name)
        return None if live is None else dict(live)


@pytest.fixture
def live_cluster(monkeypatch):
    live = LiveCluster()
    for attr in ("exists", "matches", "apply", "delete", "namespace_labels"):
        monkeypatch.setattr(reconcile.kube, attr, getattr(live, attr))
    monkeypatch.setattr(reconcile, "preflight_tools", lambda tools=None: None)
    return live


CUSTOM = {"custom": {"repo": "https://charts.example.com", "namespace": "mine"}}


def test_converge_leaves_a_preexisting_namespace_unlabelled(tmp_path, fake_helm, live_cluster):
    # a namespace that pre-existed unlabelled must not come out of converge
    # carrying the managed-by marker: the old code labelled it, and destroy
    # then removed a namespace the plugin never created
    live_cluster.namespaces["mine"] = {}
    fake_helm.installed["custom"] = "1.0.0"
    reconcile.converge(_pool_ctx(tmp_path, CUSTOM))
    assert live_cluster.applied == []  # its PSA-only target already matches
    assert MANAGED_BY_KEY not in live_cluster.namespaces["mine"]
    reconcile.destroy(_pool_ctx(tmp_path, CUSTOM))
    assert "namespace mine" not in live_cluster.deleted
    assert "mine" in live_cluster.namespaces


def test_converge_labels_a_created_namespace_and_destroy_removes_it(
    tmp_path, fake_helm, live_cluster
):
    reconcile.converge(_pool_ctx(tmp_path, CUSTOM))
    assert live_cluster.namespaces["mine"] == {MANAGED_BY_KEY: MANAGED_BY_VALUE}
    reconcile.destroy(_pool_ctx(tmp_path, CUSTOM))
    assert "namespace mine" in live_cluster.deleted


def test_converge_applies_psa_labels_to_a_preexisting_namespace_without_the_marker(
    tmp_path, fake_helm, live_cluster
):
    # the PSA labels still converge onto a namespace the plugin did not
    # create, and check does not read its missing marker as drift
    live_cluster.namespaces["mine"] = {}
    entry = {
        "custom": {
            "repo": "https://charts.example.com", "version": "1.0.0",
            "namespace": {"name": "mine", "enforce": "restricted"},
        }
    }
    fake_helm.installed["custom"] = "1.0.0"
    reconcile.converge(_pool_ctx(tmp_path, entry))
    assert live_cluster.namespaces["mine"] == {
        "pod-security.kubernetes.io/enforce": "restricted"
    }
    assert reconcile.check(_pool_ctx(tmp_path, entry))["entries"]["custom"] == "ok"


def test_disabled_entry_removes_a_leftover_namespace(tmp_path, monkeypatch):
    # the release is already gone but the plugin's namespace remains: disable
    # clears it, the way destroy does
    log = []
    _stub(monkeypatch, log, releases=(), exists=False)
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"enabled": False}}))
    assert result["entries"]["metallb"]["action"] == "absent"
    assert log == ["delete namespace metallb-system"]


def test_disabled_entry_keeps_a_namespace_it_did_not_create(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("metallb",), exists=True, managed=False)
    result = reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"enabled": False}}))
    assert result["entries"]["metallb"]["action"] == "removed"
    assert log == ["delete metallb pool resources", "uninstall metallb"]


def test_disabled_entry_keeps_a_namespace_another_entry_uses(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # disabling one of two entries sharing a namespace must not yank it from
    # the one still enabled
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(k.get("label") or target),
    )
    charts = {
        "traefik": {"namespace": "shared"},
        "custom": {
            "repo": "https://charts.example.com", "namespace": "shared", "enabled": False,
        },
    }
    fake_helm.installed["custom"] = "1.0.0"
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["custom"]["action"] == "removed"
    assert "namespace shared" not in deleted


def test_disabled_entry_removes_a_namespace_no_enabled_entry_needs(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # once no enabled entry converges into it anymore, a disable removes the
    # shared namespace; a disabled co-entry does not keep it alive
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(k.get("label") or target),
    )
    charts = {
        "traefik": {"namespace": "shared", "enabled": False},
        "custom": {"repo": "https://charts.example.com", "namespace": "shared", "enabled": False},
    }
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["traefik"]["action"] == "absent"
    assert "namespace shared" in deleted


def test_disabled_ceph_clears_leftover_namespaces(tmp_path, fake_helm, no_kube, monkeypatch):
    # the releases are already gone but the plugin's namespaces remain; both
    # charts are cleared, including one whose rbd:/fs: flag no longer names it
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(k.get("label") or target),
    )
    result = reconcile.converge(_pool_ctx(tmp_path, {"ceph": dict(CEPH, enabled=False)}))
    assert result["entries"]["ceph"]["action"] == "absent"
    assert deleted == ["namespace ceph-csi-rbd", "namespace ceph-csi-cephfs"]


def test_disabled_ceph_removes_a_chart_whose_flag_was_turned_off(
    tmp_path, fake_helm, no_kube, monkeypatch
):
    # fs was turned off (and the entry disabled) while the cephfs release was
    # still installed: the disable still probes both charts and takes it down
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda root, name: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    fake_helm.installed["ceph-csi-cephfs"] = "3.0.0"
    result = reconcile.converge(_pool_ctx(tmp_path, {"ceph": dict(CEPH, enabled=False)}))
    assert result["entries"]["ceph"]["action"] == "removed"
    assert fake_helm.uninstalls == ["ceph-csi-cephfs"]


def test_disabled_ceph_deletes_both_charts_secrets(tmp_path, fake_helm, no_kube, monkeypatch):
    # both Secrets are probed alone, so one missing never reads as both gone;
    # a flag no longer naming a chart does not keep its Secret either
    monkeypatch.setattr(reconcile.kube, "exists", lambda root, target, **k: True)
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "delete",
        lambda root, target, **k: deleted.append(yaml.safe_load(k["input"])["metadata"]["name"]),
    )
    result = reconcile.converge(_pool_ctx(tmp_path, {
        "ceph": dict(CEPH, enabled=False, userID="kubernetes", userKey="AQC0secretkey==")
    }))
    assert result["entries"]["ceph"]["action"] == "absent"
    assert deleted == ["csi-rbd-secret", "csi-cephfs-secret"]


def test_namespace_labels_reads_the_live_labels(tmp_path, monkeypatch):
    calls = []

    def run(root, args, **k):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"metadata": {"labels": {"a": "b"}}}')

    monkeypatch.setattr(reconcile.kube, "_run", run)
    assert reconcile.kube.namespace_labels(tmp_path, "ns") == {"a": "b"}
    assert calls == [["get", "namespace", "ns", "-o", "json"]]


def test_namespace_labels_reads_none_when_the_get_fails(tmp_path, monkeypatch):
    # an absent namespace or an unreachable api must never read as owned
    monkeypatch.setattr(
        reconcile.kube, "_run",
        lambda root, args, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert reconcile.kube.namespace_labels(tmp_path, "ns") is None


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


def test_converge_applies_manifests_server_side(tmp_path, fake_helm, no_kube, monkeypatch):
    """The gateway-api httproutes CRD nearly fills the 256 KiB last-applied
    annotation client-side apply records, so manifest entries apply with
    --server-side."""
    applied: dict = {}

    def apply(root, target, **kwargs):
        applied["target"] = target
        applied["server_side"] = kwargs.get("server_side")

    monkeypatch.setattr(reconcile.kube, "apply", apply)
    monkeypatch.setattr(reconcile.upstream, "gateway_latest_version", lambda: "v1.7.0")
    result = reconcile.converge(_pool_ctx(tmp_path, {"gateway": {"version": "latest"}}))
    assert result["entries"]["gateway"]["action"] == "applied"
    assert applied["server_side"] is True
    assert applied["target"].endswith("/standard-install.yaml")


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
        # zcustom precedes acustom in cluster.yaml and must converge in that
        # order, not the alphabetical one
        "zcustom": {"repo": "https://x"},
        "acustom": {"repo": "https://x"},
    }
    # safe_dump alphabetizes keys by default, which would erase the order
    # under test
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"name": "t", "charts": charts}, sort_keys=False)
    )
    cfg = Config.load(tmp_path)
    names = [entry.name for entry in reconcile._ordered(cfg.entries)]
    assert names == [
        "gateway", "metallb", "traefik", "sealed-secrets", "cert-manager",
        "ceph", "nfs", "zcustom", "acustom",
    ]


def test_pool_lands_in_the_configured_namespace(tmp_path, fake_helm, no_kube, monkeypatch):
    # a custom metallb namespace moves the pool CRs with it: the controller
    # watches the namespace its chart was installed into
    applied = []

    def apply(root, target, **k):
        if k.get("input"):
            applied.extend(yaml.safe_load_all(k["input"]))

    monkeypatch.setattr(reconcile.kube, "apply", apply)
    reconcile.converge(_pool_ctx(tmp_path, {"metallb": {"namespace": "lb"}}))
    pool = next(d for d in applied if d["kind"] == "IPAddressPool")
    l2 = next(d for d in applied if d["kind"] == "L2Advertisement")
    assert pool["metadata"]["namespace"] == "lb"
    assert l2["metadata"]["namespace"] == "lb"


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


def test_cert_manager_issuers_wait_for_the_webhook(tmp_path, fake_helm, no_kube, monkeypatch):
    # the ClusterIssuers are validated by the chart's webhook deployment,
    # which is still coming up seconds after a fresh install; the controller
    # reporting available says nothing about the webhook
    waited = []

    def wait(root, name, namespace, **k):
        waited.append(name)
        return True

    monkeypatch.setattr(reconcile.kube, "wait_deployment_available", wait)
    charts = {"cert-manager": {"email": "a@b", "prod": True}}
    reconcile.converge(_pool_ctx(tmp_path, charts))
    assert waited == ["cert-manager-webhook"]


def test_cert_manager_issuers_deleted_before_uninstall(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("cert-manager",), exists=True)
    charts = {"cert-manager": {"email": "a@b", "prod": True, "enabled": False}}
    result = reconcile.converge(_pool_ctx(tmp_path, charts))
    assert result["entries"]["cert-manager"]["action"] == "removed"
    assert log == [
        "delete cert-manager ClusterIssuers",  # webhook must still be serving
        "uninstall cert-manager",
        "delete namespace cert-manager",
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


def test_check_fails_while_a_turned_off_chart_is_still_installed(tmp_path, fake_helm, no_kube):
    # converge would uninstall the chart the fs flag no longer names, so its
    # presence is drift -- the same contract every other release answers to
    fake_helm.installed["ceph-csi-rbd"] = "3.0.0"
    fake_helm.installed["ceph-csi-cephfs"] = "3.0.0"
    fake_helm.values["ceph-csi-rbd"] = CEPH_RBD_VALUES
    report = reconcile.check(_pool_ctx(tmp_path, {"ceph": dict(CEPH)}))
    assert report["ok"] is False
    assert report["entries"]["ceph"] == "drifted"


@pytest.mark.parametrize("ceph", [False, True])
def test_destroy_removes_shared_namespaces_after_all_releases(tmp_path, monkeypatch, ceph):
    log = []
    namespace = "ceph-csi-rbd" if ceph else "shared"
    first = "ceph-csi-rbd" if ceph else "metallb"
    _stub(monkeypatch, log, releases=(first, "custom"), exists=True)
    entries = {
        "ceph": dict(CEPH),
        "custom": {"repo": "https://charts.example.com", "namespace": namespace},
    } if ceph else {
        "metallb": {"namespace": namespace},
        "custom": {"repo": "https://charts.example.com", "namespace": namespace},
    }
    reconcile.destroy(_pool_ctx(tmp_path, entries))
    deletion = f"delete namespace {namespace}"
    assert log.count(deletion) == 1
    assert log.index(deletion) > log.index(f"uninstall {first}")
    assert log.index(deletion) > log.index("uninstall custom")


def test_disabling_shared_entries_keeps_namespace_until_both_are_removed(tmp_path, monkeypatch):
    log = []
    _stub(monkeypatch, log, releases=("metallb", "custom"), exists=True)
    entries = {
        "metallb": {"namespace": "shared", "enabled": False},
        "custom": {
            "repo": "https://charts.example.com", "namespace": "shared", "enabled": False,
        },
    }
    reconcile.converge(_pool_ctx(tmp_path, entries))
    assert log.count("delete namespace shared") == 1
    assert log.index("delete namespace shared") > log.index("uninstall custom")


@pytest.mark.parametrize("disabled_ceph", [True, False])
def test_disable_respects_namespaces_shared_with_ceph(
    tmp_path, fake_helm, no_kube, monkeypatch, disabled_ceph
):
    deleted = []
    monkeypatch.setattr(
        reconcile.kube, "namespace_labels",
        lambda *a: {MANAGED_BY_KEY: MANAGED_BY_VALUE},
    )
    monkeypatch.setattr(
        reconcile.kube, "delete", lambda *a, **k: deleted.append(k.get("label"))
    )
    entries = {
        "ceph": dict(CEPH, enabled=not disabled_ceph),
        "custom": {
            "repo": "https://charts.example.com", "namespace": "ceph-csi-rbd",
            "enabled": disabled_ceph,
        },
    }
    reconcile.converge(_pool_ctx(tmp_path, entries))
    assert "namespace ceph-csi-rbd" not in deleted

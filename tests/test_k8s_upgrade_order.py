"""A kubernetes bump must go through `talosctl upgrade-k8s`, not a config push.

The machine config carries the kubelet/control-plane images for a version, so
generating a running cluster's configs with the target version upgrades it in
one jump when the configs are applied. converge therefore generates with the
running version and lets the upgrade phase step minors.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge, versions
from taloscluster.errors import ReconcileError
from taloscluster.infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
    TalosContribution,
)
from taloscluster.talos import machineconfig

CFG = SimpleNamespace(kubernetes_version="v1.36.4")


def test_running_cluster_keeps_its_current_version(monkeypatch):
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.4")
    assert converge._config_kubernetes_version(CFG, Path("kc"), up=True) == "v1.34.4"


def test_cluster_at_target_uses_target(monkeypatch):
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.36.4")
    assert converge._config_kubernetes_version(CFG, Path("kc"), up=True) == "v1.36.4"


def test_fresh_cluster_uses_target(monkeypatch):
    monkeypatch.setattr(converge.kubectl, "server_version",
                        lambda *_a: pytest.fail("must not ask a cluster that is down"))
    assert converge._config_kubernetes_version(CFG, Path("kc"), up=False) == "v1.36.4"


def test_unknown_running_version_aborts_instead_of_falling_back_to_target(monkeypatch):
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    with pytest.raises(ReconcileError, match="could not determine the running"):
        converge._config_kubernetes_version(CFG, Path("kc"), up=True)


def test_plan_with_a_missing_kubeconfig_keeps_the_target(monkeypatch, tmp_path):
    """`plan` (dry-run) on a recovered machine has no kubeconfig on disk, so the
    running version cannot be read; it must keep the target and complete rather
    than abort the whole plan (mirrors `_upgrade`'s dry-run guard)."""
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    # an empty read is exactly what kubectl returns against a missing kubeconfig
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    kubeconfig = tmp_path / "kubeconfig"  # absent, never written in dry-run
    assert converge._config_kubernetes_version(CFG, kubeconfig, up=True) == "v1.36.4"


def test_plan_escapes_only_with_no_nonempty_kubeconfig(monkeypatch, tmp_path):
    """The dry-run escape from the version-read refusal is granted solely by the
    missing or empty kubeconfig -- it is not tied to a recovered management
    machine. A `plan` run next to a reachable cluster whose kubeconfig was
    deleted keeps the target, but a dry run that DOES have a kubeconfig is not
    excused: it reads the running version like a real run (and still refuses to
    fall back to the target when that read comes up empty)."""
    # dry run with a present non-empty kubeconfig: the running version answers,
    # so the target is not blindly kept -- the running version is used.
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.4")
    present = tmp_path / "kubeconfig"
    present.write_text("clusters: []\n")
    assert converge._config_kubernetes_version(CFG, present, up=True) == "v1.34.4"

    # same dry run, but the read comes up empty: the escape does not apply when
    # a kubeconfig is on disk, so it still aborts rather than falling back to
    # the target.
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    with pytest.raises(ReconcileError, match="could not determine the running"):
        converge._config_kubernetes_version(CFG, present, up=True)


def test_version_read_is_retried_before_aborting(monkeypatch, capsys):
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    reads: list[str] = ["", "", "v1.34.4"]

    def fake_version(*_a):
        return reads.pop(0)

    monkeypatch.setattr(converge.kubectl, "server_version", fake_version)
    assert converge._config_kubernetes_version(CFG, Path("kc"), up=True) == "v1.34.4"
    out = capsys.readouterr().out
    assert "retrying" in out


def test_downgrade_is_refused(monkeypatch):
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.37.0")
    with pytest.raises(ReconcileError, match="downgrade"):
        converge._config_kubernetes_version(CFG, Path("kc"), up=True)


def test_build_configs_passes_the_override_to_talosctl(make_config, monkeypatch, tmp_path):
    from taloscluster.infrastructure import Endpoint, TalosContribution

    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
    })
    seen: list[str] = []

    def fake_gen_config(**kwargs):
        seen.append(kwargs["kubernetes_version"])
        return "machine: {}"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets = SimpleNamespace(tailscale_auth_key=None)
    contributions = {h: TalosContribution(install_disk="/dev/vda") for h in cfg.machines}
    endpoint = Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10")
    images = {m.extensions: "installer" for m in cfg.machines.values()}

    machineconfig.build_configs(cfg, secrets, cfg.machines, endpoint, tmp_path / "s",
                                images, contributions, kubernetes_version="v1.34.4")
    assert set(seen) == {"v1.34.4"}

    seen.clear()
    machineconfig.build_configs(cfg, secrets, cfg.machines, endpoint, tmp_path / "s",
                                images, contributions)
    assert set(seen) == {cfg.kubernetes_version}


# ---- scale-up during the same run as a kubernetes upgrade -----------------

def _new_node_fixtures(make_config):
    cfg = make_config({
        "controlplane": {"count": 2, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},
    })
    secrets = SimpleNamespace(tailscale_auth_key=None)
    contributions = {h: TalosContribution(install_disk="/dev/vda") for h in cfg.machines}
    endpoint = Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10")
    images = {m.extensions: "installer" for m in cfg.machines.values()}
    return cfg, secrets, contributions, endpoint, images


def test_scale_up_configs_carry_the_target_version_after_an_upgrade(
    make_config, monkeypatch, tmp_path
):
    """A node scaled up in the same run as a kubernetes upgrade must boot at the
    upgraded (target) version. The pre-upgrade configs -- the ones applied to the
    existing cluster so `talosctl upgrade-k8s` steps minors -- carry the running
    version; a new node has no prior minor to step, so its config must be rebuilt
    with the target the upgrade phase just established."""
    cfg, secrets, contributions, endpoint, images = _new_node_fixtures(make_config)
    running = "v1.34.4"  # a minor behind cluster.yaml (the upgrade target)
    assert cfg.kubernetes_version != running

    seen: list[str] = []

    def fake_gen_config(**kwargs):
        seen.append(kwargs["kubernetes_version"])
        return "machine: {}"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    # only one control plane exists; the other control plane and the worker are
    # new in this run
    existing = {"testcluster-controlplane-01"}
    inv = InfrastructureInventory(
        machines={h: InfrastructureMachine(h) for h in existing}
    )
    refs = NetworkResult(kubernetes=endpoint)

    fresh = converge._new_node_configs(
        cfg, secrets, cfg.machines, inv, refs, tmp_path / "secrets",
        images, contributions, default_tags=None,
    )

    assert set(fresh) == set(cfg.machines) - existing
    assert set(seen) == {cfg.kubernetes_version}  # the target, not the running version


def test_scale_up_configs_are_regenerated_only_for_missing_nodes(
    make_config, monkeypatch, tmp_path
):
    """No-scale-up runs must not regenerate anything -- `_apply_configs` already
    pushed the running-version configs to the existing nodes, so rebuilds with
    the target version would be both wasted work and a config-push upgrade."""
    cfg, secrets, contributions, endpoint, images = _new_node_fixtures(make_config)
    monkeypatch.setattr(
        machineconfig.talosctl, "gen_config",
        lambda **kwargs: pytest.fail("must not regenerate a fully-existing cluster"),
    )
    inv = InfrastructureInventory(machines={h: InfrastructureMachine(h) for h in cfg.machines})
    refs = NetworkResult(kubernetes=endpoint)

    fresh = converge._new_node_configs(
        cfg, secrets, cfg.machines, inv, refs, tmp_path / "secrets",
        images, contributions, default_tags=None,
    )
    assert fresh == {}


# ---- converge(): the compute phase must not crash when configs are empty ----

class _DryRunState:
    """A state where talossecrets.yaml does not exist (fresh cluster)."""

    def __init__(self, root):
        self.secrets_path = root / "talossecrets.yaml"

    def secrets_exist(self):
        return False

    def write_secrets(self, _contents):
        raise AssertionError("dry run must not write secrets")


class _DryRunNoFipBackend:
    """Reconciles to an empty network result (no fip/vip) and an empty inventory,
    so the config block never runs and the compute phase runs on empty configs."""

    name = "openstack"
    installer_platform = "openstack"
    computed = False

    def load_inventory(self):
        return InfrastructureInventory()

    def ensure_boot_artifact(self):
        return "image"

    def validate_machines(self, _machines, _inventory):
        return None

    def reconcile_network(self, _machines, _inventory):
        return NetworkResult()

    def default_node_tags(self):
        return {}

    def reconcile_machines(self, _machines, _inventory, _boot_image, configs):
        self.computed = True
        return set()

    def provider_status(self):
        return {}


def test_converge_plan_dry_run_reaches_compute_without_secrets(monkeypatch, tmp_path):
    """The `plan` command drives converge with dry_run=True; on a fresh cluster
    no secrets exist and no fip is allocated, so `configs` stays empty and the
    config block (which binds `contributions`) is skipped. The compute phase must
    still run without crashing -- this regressed into an UnboundLocalError on
    `contributions` at the scale-up rebuild call site."""
    backend = _DryRunNoFipBackend()
    cfg = SimpleNamespace(
        name="phoenix", talos_version="v1.13.0",
        extension_sets=lambda: [()], machines={},
        kubernetes_version="v1.31.0", tailscale_enabled=True,
    )
    secrets = SimpleNamespace(tailscale_auth_key=None)
    state = _DryRunState(tmp_path)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: secrets)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)

    assert backend.computed is True


class _FreshBootstrapBackend:
    """Fresh-cluster backend: secrets exist and a fip is allocated, but no
    machines are up yet, so the configs are built once at the target version."""

    name = "openstack"
    installer_platform = "openstack"

    def __init__(self, inventory):
        self.inventory = inventory

    def load_inventory(self):
        return self.inventory

    def ensure_boot_artifact(self):
        return "image"

    def validate_machines(self, _machines, _inventory):
        return None

    def reconcile_network(self, _machines, _inventory):
        return NetworkResult(
            kubernetes=Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10")
        )

    def default_node_tags(self):
        return {}

    def reconcile_machines(self, _machines, _inventory, _boot_image, _configs):
        return set()

    def talos_contribution(self, _machine, _refs):
        return TalosContribution(install_disk="/dev/vda")

    def provider_status(self):
        return {}


class _ExistingSecretsState(_DryRunState):
    def secrets_exist(self):
        return True

    def write_secrets(self, _contents):
        raise AssertionError("secrets already exist")


def test_converge_fresh_bootstrap_regenerates_configs_once(make_config, monkeypatch, tmp_path):
    """A fresh-cluster bootstrap builds configs at the target version (up=False,
    `_config_kubernetes_version` returns cfg.kubernetes_version), so the compute
    phase must not regenerate the same configs a second time. This was a waste
    that ran `talosctl gen_config` once per node twice for identical output."""
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
    })
    backend = _FreshBootstrapBackend(InfrastructureInventory())
    state = _ExistingSecretsState(tmp_path)
    calls = {"n": 0}

    def counting_gen_config(**kwargs):
        calls["n"] += 1
        return "machine: {}"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", counting_gen_config)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)

    expected_nodes = len(cfg.machines)
    assert calls["n"] == expected_nodes  # once per node for the bootstrap, never a second time


class _ScaleUpAfterUpgradeBackend:
    """An up cluster scaled up in the same run as a kubernetes upgrade: one
    control plane already exists, the other control plane and the worker are new
    and must boot at the upgraded (target) version. `reconcile_machines` records
    the configs it is handed so the test can assert the fresh node got the target
    version while the existing node kept its running-version config."""

    name = "openstack"
    installer_platform = "openstack"

    def __init__(self, inventory):
        self.inventory = inventory
        self.applied: dict[str, str] = {}
        self.probed = False

    def load_inventory(self):
        return self.inventory

    def ensure_boot_artifact(self):
        return "image"

    def validate_machines(self, _machines, _inventory):
        return None

    def reconcile_network(self, _machines, _inventory):
        return NetworkResult(
            kubernetes=Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10")
        )

    def default_node_tags(self):
        return {}

    def reconcile_machines(self, _machines, _inventory, _boot_image, configs):
        self.applied = dict(configs)
        return set()

    def talos_contribution(self, _machine, _refs):
        return TalosContribution(install_disk="/dev/vda")

    def provider_status(self):
        return {}


def test_converge_scales_up_nodes_at_the_upgraded_version(
    make_config, monkeypatch, tmp_path
):
    """End-to-end wiring of the scale-up fix: with the cluster up and running a
    minor behind cluster.yaml (the upgrade target), a node that does not exist
    yet must be handed to `reconcile_machines` with a config baked at the target
    version -- not the running version baked into the existing nodes' configs.
    This is the converging behaviour the unit tests above only assert in
    isolation."""
    cfg = make_config({
        "controlplane": {"count": 2, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},
    })
    running = "v1.34.4"  # a minor behind cfg.kubernetes_version (the upgrade target)
    assert versions.is_older(running, cfg.kubernetes_version)  # genuinely an upgrade

    # one control plane exists; the other control plane and the worker are new
    missing_h = {h for h in cfg.machines if h != "testcluster-controlplane-01"}
    backend = _ScaleUpAfterUpgradeBackend(
        InfrastructureInventory(
            machines={
                "testcluster-controlplane-01": InfrastructureMachine(
                    "testcluster-controlplane-01"
                )
            }
        )
    )
    state = _ExistingSecretsState(tmp_path)
    # an up cluster already bootstrapped and wrote its kubeconfig; without it
    # `_kube_up` would short-circuit on the missing file and read the cluster as
    # never-bootstrapped (down), skipping the scale-up wiring under test
    (tmp_path / "kubeconfig").write_text("clusters: []\n")

    calls: list[str] = []

    def fake_build_configs(
        _cfg, _secrets, machines, _endpoint, _secrets_path, _images,
        _contributions, default_tags=None, kubernetes_version=None,
    ):
        calls.append(kubernetes_version)
        return {h: f"config/{h}" for h in machines}

    monkeypatch.setattr(machineconfig, "build_configs", fake_build_configs)
    # the real `_config_kubernetes_version` runs so the downgrade guard is
    # exercised: running (v1.34.4) is below the target (v1.36.4), a genuine upgrade
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: running)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)
    monkeypatch.setattr(converge, "_scale_down", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_apply_configs", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)

    # configs baked twice: the existing cluster at the running version, then the
    # missing nodes regenerated at the target version
    assert calls == [running, cfg.kubernetes_version]
    # the fresh nodes' target-version configs reach reconcile_machines
    for h in missing_h:
        assert backend.applied.get(h) == f"config/{h}"
    # the existing node keeps its running-version config, not a rebuild
    existing = "testcluster-controlplane-01"
    assert backend.applied.get(existing) == f"config/{existing}"


def test_converge_aborts_when_reachable_cluster_version_cannot_be_read(
    make_config, monkeypatch, tmp_path
):
    """A reachable cluster whose kubernetes version read fails must abort before
    any config mutation -- never fall back to the target version.

    The machine config carries kubelet/control-plane images; baking the target
    into it and pushing it through `_apply_existing_configs` would skip every
    minor in between. With the cluster answering `cluster_up` (reachable) but
    `server_version` returning empty on every retry, `_config_kubernetes_version`
    raises before `build_configs`/`_apply_existing_configs` run, so no target
    version reaches the backend."""
    cfg = make_config({
        "controlplane": {"count": 2, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},  # target that skips minors from v1.34
    })
    backend = _ScaleUpAfterUpgradeBackend(
        InfrastructureInventory(
            machines={
                "testcluster-controlplane-01": InfrastructureMachine(
                    "testcluster-controlplane-01"
                )
            }
        )
    )
    state = _ExistingSecretsState(tmp_path)
    (tmp_path / "kubeconfig").write_text("clusters: []\n")

    monkeypatch.setattr(machineconfig, "build_configs",
                        lambda *a, **k: pytest.fail("must not build configs"))
    monkeypatch.setattr(converge, "_apply_configs",
                        lambda *a, **k: pytest.fail("must not apply configs"))
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)
    # reachable cluster that never answers the version read
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge, "_scale_down", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    with pytest.raises(ReconcileError, match="could not determine the running"):
        converge.converge(tmp_path)

    assert not backend.applied  # no config reached the provider


def test_converge_recovers_a_missing_kubeconfig_and_keeps_upgrade_before_scale_up(
    make_config, monkeypatch, tmp_path
):
    """End-to-end of the recovery fix: a lost management machine restored the
    identity but not the derived kubeconfig. When converge recovers the kubeconfig
    from the restored identity and finds the cluster UP (running a minor behind
    cluster.yaml), a missing node must still be scaled up at the upgraded (target)
    version -- NOT treated as a fresh cluster, which would skip the upgrade and
    input newer kubelets among an old cluster."""
    cfg = make_config({
        "controlplane": {"count": 2, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},
    })
    running = "v1.34.4"  # a minor behind cfg.kubernetes_version (the upgrade target)
    assert versions.is_older(running, cfg.kubernetes_version)

    missing_h = {h for h in cfg.machines if h != "testcluster-controlplane-01"}
    backend = _ScaleUpAfterUpgradeBackend(
        InfrastructureInventory(
            machines={
                "testcluster-controlplane-01": InfrastructureMachine(
                    "testcluster-controlplane-01"
                )
            }
        )
    )
    state = _ExistingSecretsState(tmp_path)
    kubeconfig = tmp_path / "kubeconfig"  # deliberately absent (recovered machine)

    def recover(*_a, **_k):
        kubeconfig.write_text("clusters: []\n")
        return True

    calls: list[str] = []

    def fake_build_configs(
        _cfg, _secrets, machines, _endpoint, _secrets_path, _images,
        _contributions, default_tags=None, kubernetes_version=None,
    ):
        calls.append(kubernetes_version)
        return {h: f"config/{h}" for h in machines}

    monkeypatch.setattr(machineconfig, "build_configs", fake_build_configs)
    # recovery succeeds: the kubeconfig now exists and the api answers; the real
    # `_config_kubernetes_version` (and its downgrade guard) runs against `running`
    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", recover)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: running)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)
    monkeypatch.setattr(converge, "_scale_down", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_apply_configs", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)

    assert kubeconfig.is_file()  # the recovery wrote it back
    # configs baked twice: the recovered cluster at the running version, then the
    # missing nodes regenerated at the target version -- the ordering a fresh
    # mis-read (target-version bootstrap) would have skipped
    assert calls == [running, cfg.kubernetes_version]
    for h in missing_h:
        assert backend.applied.get(h) == f"config/{h}"
    existing = "testcluster-controlplane-01"
    assert backend.applied.get(existing) == f"config/{existing}"


class _NoTailscaleRecoverBackend(_ScaleUpAfterUpgradeBackend):
    """An up recovered cluster without tailscale: cp-01 reports a real managed
    address so `_talos_endpoint` resolves to it, and the recovery must dial that
    real address -- there is no MagicDNS name to dial as the bare hostname."""

    def reconcile_network(self, _machines, _inventory):
        return NetworkResult(
            kubernetes=Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10"),
            machine_attachments={
                "testcluster-controlplane-01": (
                    NetworkAttachment(name="cluster", address="192.168.100.11"),
                )
            },
        )


def test_converge_recovers_a_missing_kubeconfig_via_the_real_address_without_tailscale(
    make_config, monkeypatch, tmp_path
):
    """End-to-end of the recovery fix on the second supported access path (no
    tailscale). talosctl uses `-n` as the apid dial target, so on a recovered
    cluster without tailscale kubeconfig recovery must target cp-01's REAL
    address -- the bare `{name}-controlplane-01` hostname has no MagicDNS name
    to dial. Before this fix that left the 900s tolerant wait to expire, recovery
    returned False and the healthy cluster was read as never-bootstrapped."""
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},
    })
    assert not cfg.tailscale_enabled
    backend = _NoTailscaleRecoverBackend(
        InfrastructureInventory(
            machines={
                "testcluster-controlplane-01": InfrastructureMachine(
                    "testcluster-controlplane-01"
                )
            }
        )
    )
    state = _ExistingSecretsState(tmp_path)
    kubeconfig = tmp_path / "kubeconfig"  # deliberately absent (recovered machine)
    dial: list[str] = []

    def recover(_talosconfig, endpoint, node, _kubeconfig, **_k):
        dial.append((endpoint, node))
        kubeconfig.write_text("clusters: []\n")
        return True

    def fake_build_configs(
        _cfg, _secrets, machines, _endpoint, _secrets_path, _images,
        _contributions, default_tags=None, kubernetes_version=None,
    ):
        return {h: f"config/{h}" for h in machines}

    monkeypatch.setattr(machineconfig, "build_configs", fake_build_configs)
    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", recover)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.36.4")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)
    monkeypatch.setattr(converge, "_scale_down", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_apply_configs", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)

    # the recovery targeted cp-01's real managed address, not the bare hostname
    assert dial == [("192.168.100.11", "192.168.100.11")]
    assert kubeconfig.is_file()


def test_converge_plan_recovers_without_stubbing_phase_functions(
    make_config, monkeypatch, tmp_path
):
    """`plan` (dry-run) on a recovered machine must complete with the REAL phase
    functions -- no kubeconfig is on disk, because recovery prognoses the cluster
    UP without writing a client file. Before the dry-run guards, `_scale_down`
    hit `kubectl.get nodes` (check=True) against the missing file and aborted,
    and `_upgrade` slept 30s then failed its stabilization loop over an absent
    kube-config. Regress only the phase functions is what the other recovery
    tests do by stubbing them; this test exercises them for real."""
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
        "kubernetes": {"version": "v1.36.4"},
        "tailscale": {},
    })
    backend = _ScaleUpAfterUpgradeBackend(
        InfrastructureInventory(
            machines={
                "testcluster-controlplane-01": InfrastructureMachine(
                    "testcluster-controlplane-01"
                )
            }
        )
    )
    state = _ExistingSecretsState(tmp_path)
    kubeconfig = tmp_path / "kubeconfig"  # absent: plan recovers but writes nothing

    def recover_prognosis(*_a, **_k):
        return True  # dry-run recovery writes no kubeconfig, yet reports the cluster UP

    def fake_build_configs(
        _cfg, _secrets, _machines, _endpoint, _secrets_path, _images,
        _contributions, default_tags=None, kubernetes_version=None,
    ):
        return {}

    monkeypatch.setattr(machineconfig, "build_configs", fake_build_configs)
    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", recover_prognosis)
    # no talosctl/kubectl binaries on CI: the dry-run guards under test are the
    # _scale_down/_upgrade reclaim paths, not membership discovery or node state,
    # so keep those probes from shelling out
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_k: {})
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: False)
    # dry-run recovery prognoses the cluster UP with no kubeconfig on disk, so
    # the running version read is empty -- exactly what kubectl returns against
    # a missing file. The plan must complete rather than abort.
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        converge, "load_secrets", lambda _root: SimpleNamespace(tailscale_auth_key=None)
    )
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_k: 0)
    # pure unit test: don't POST to the talos image factory for a schematic id
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")

    converge.converge(tmp_path)  # must not raise CalledProcessError/ReconcileError

    assert not kubeconfig.exists()  # dry-run wrote no client file
    assert not backend.applied  # nothing reached reconcile_machines (no configs)


# ---------------------------------------------------------------------------
# _k8s_upgrade_path: step one minor at a time, stone-patch hops
# ---------------------------------------------------------------------------

def test_upgrade_path_steps_through_intermediate_minors(monkeypatch):
    monkeypatch.setattr(
        converge.versions, "latest_kubernetes_patch", lambda minor: f"v{minor}.9"
    )
    assert converge._k8s_upgrade_path("v1.34.1", "v1.36.2") == ["v1.35.9", "v1.36.2"]


def test_upgrade_path_is_direct_for_adjacent_minors():
    assert converge._k8s_upgrade_path("v1.34.5", "v1.35.0") == ["v1.35.0"]


def test_upgrade_path_at_target_returns_only_the_target():
    assert converge._k8s_upgrade_path("v1.36.2", "v1.36.2") == ["v1.36.2"]


def test_upgrade_path_falls_back_to_minor_point_zero_when_lookup_fails(monkeypatch, capsys):
    def boom(_minor):
        raise OSError("dl.k8s.io unreachable")

    monkeypatch.setattr(converge.versions, "latest_kubernetes_patch", boom)
    assert converge._k8s_upgrade_path("v1.34.1", "v1.36.2") == ["v1.35.0", "v1.36.2"]
    assert "using 1.35.0" in capsys.readouterr().err


def test_upgrade_path_rejects_an_unknown_current():
    # `_upgrade` raises before calling when the running version is empty, so
    # the direct-upgrade path was removed; an empty `cur` is a programming error
    with pytest.raises(ValueError, match="invalid literal"):
        converge._k8s_upgrade_path("", "v1.36.2")

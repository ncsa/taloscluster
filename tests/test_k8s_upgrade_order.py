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

from taloscluster import converge
from taloscluster.errors import ReconcileError
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


def test_unknown_running_version_falls_back_to_target(monkeypatch):
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "")
    assert converge._config_kubernetes_version(CFG, Path("kc"), up=True) == "v1.36.4"


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

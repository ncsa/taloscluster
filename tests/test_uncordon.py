"""Tests for the stale-cordon cleanup in the upgrade path.

`talosctl upgrade` cordons the node it upgrades and uncordons it on completion,
but skips the uncordon when its client-side watch dies or the run is
interrupted -- leaving a node that nothing schedules onto and that fails every
later `talosctl health` on "some nodes are not schedulable".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.infrastructure import (
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkResult,
)
from taloscluster.k8s import kubectl


@pytest.fixture
def cluster(monkeypatch):
    """Fake cluster state: which nodes are cordoned + which uncordons ran."""
    state: dict = {"cordoned": [], "uncordoned": [], "fail": False}
    monkeypatch.setattr(kubectl, "unschedulable", lambda kc: list(state["cordoned"]))

    def fake_uncordon(kubeconfig, name):
        if state["fail"]:
            return False
        state["uncordoned"].append(name)
        state["cordoned"].remove(name)
        return True

    monkeypatch.setattr(kubectl, "uncordon", fake_uncordon)
    return state


def test_uncordons_a_cordoned_node(cluster):
    cluster["cordoned"] = ["cp-01"]
    converge._uncordon_stale(Path("kubeconfig"), "cp-01")
    assert cluster["uncordoned"] == ["cp-01"]


def test_no_op_when_the_node_is_schedulable(cluster):
    """Idempotent: the normal case is that talos already uncordoned it."""
    cluster["cordoned"] = ["other-01"]
    converge._uncordon_stale(Path("kubeconfig"), "cp-01")
    assert cluster["uncordoned"] == []


def test_a_failed_uncordon_warns_but_does_not_raise(cluster, capsys):
    """A rollout must not abort because the cleanup could not run."""
    cluster["cordoned"] = ["cp-01"]
    cluster["fail"] = True
    converge._uncordon_stale(Path("kubeconfig"), "cp-01")
    assert "kubectl uncordon cp-01" in capsys.readouterr().err


def test_upgrade_resume_uncordons_a_node_already_at_the_target(monkeypatch):
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    uncordoned: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "node_image", lambda *_a: "installer:v1.13.9")
    monkeypatch.setattr(converge, "_uncordon_stale", lambda _kc, host: uncordoned.append(host))
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.35.8")

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    assert uncordoned == ["cp-01"]


def test_unschedulable_reads_spec(monkeypatch):
    payload = """{"items": [
        {"metadata": {"name": "cp-01"}, "spec": {"unschedulable": true}},
        {"metadata": {"name": "cp-02"}, "spec": {}},
        {"metadata": {"name": "w-01"}}
    ]}"""

    class Proc:
        returncode = 0
        stdout = payload

    monkeypatch.setattr(kubectl, "_run", lambda *a, **k: Proc())
    assert kubectl.unschedulable(Path("kubeconfig")) == ["cp-01"]


def test_unschedulable_empty_when_api_is_down(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(kubectl, "_run", lambda *a, **k: Proc())
    assert kubectl.unschedulable(Path("kubeconfig")) == []

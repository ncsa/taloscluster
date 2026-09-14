"""Tests for the stale-cordon cleanup in the upgrade path.

`talosctl upgrade` cordons the node it upgrades and uncordons it on completion,
but skips the uncordon when its client-side watch dies or the run is
interrupted -- leaving a node that nothing schedules onto and that fails every
later `talosctl health` on "some nodes are not schedulable".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.errors import ReconcileError
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
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-123")
    monkeypatch.setattr(converge, "_uncordon_stale", lambda _kc, host: uncordoned.append(host))
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.35.8")

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )

    assert uncordoned == ["cp-01"]


def test_extension_only_change_triggers_an_upgrade(monkeypatch):
    """Same talos version but a different RUNNING schematic (an added or removed
    extension) must reinstall, even though converge already applied the new
    machine config -- the config's installer reference alone can't be trusted."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-old")
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: upgrade_calls.append("upgrade")
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.35.8")

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )

    assert upgrade_calls == ["upgrade"]


def test_extension_only_change_uses_the_schematic_as_the_wait_barrier(monkeypatch):
    """An extension-only upgrade must tell the wait the target schematic, since
    the talos version cannot distinguish the reboot."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    waits: list[tuple] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-old")
    monkeypatch.setattr(converge.talosctl, "upgrade", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_wait_version",
                        lambda *args, **kw: waits.append(args) or None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.35.8")

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )

    # _upgrade passes the target schematic to _wait_version (5th positional arg)
    assert waits and waits[0][4] == "sch-123"


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


# ---------------------------------------------------------------------------
# _reconcile_talos: OpenStack first-boot and scaled-up nodes
# ---------------------------------------------------------------------------

def test_reconcile_talos_upgrades_a_node_created_on_the_base_schematic(monkeypatch):
    """A node created this run (OpenStack first boot, or a scale-up) joins on the
    shared base image but cluster.yaml wants pool extensions: the post-join
    reconcile must reinstall it."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {"w-01": SimpleNamespace(role="worker", extensions=("base", "nvidia"))}
    inventory = InfrastructureInventory(machines={"w-01": InfrastructureMachine("w-01")})
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"w-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "base-sch")
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: upgrade_calls.append("upgrade")
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base", "nvidia"): "installer:v1.13.9"},
        {("base", "nvidia"): "want-sch"}, Path("talosconfig"), Path("kubeconfig"),
    )

    assert upgrade_calls == ["upgrade"]


def test_reconcile_talos_is_a_noop_for_a_node_on_the_target_schematic(monkeypatch):
    """A node already on the target schematic (converted by an earlier phase, or
    a converged cluster) is not reinstalled."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {"w-01": SimpleNamespace(role="worker", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"w-01": InfrastructureMachine("w-01")})
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"w-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-123")
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: upgrade_calls.append("upgrade")
    )

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base",): "installer:v1.13.9"}, {("base",): "sch-123"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    assert upgrade_calls == []


def test_reconcile_talos_health_checks_a_resumed_control_plane_at_target(monkeypatch):
    """A node already on the target may be the leftover of an interrupted run: its
    apid answers while etcd never recovered. Resuming must re-establish the health
    barrier before touching the next control plane."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {
        "cp-01": SimpleNamespace(role="controlplane", extensions=("base",)),
        "cp-02": SimpleNamespace(role="controlplane", extensions=("base",)),
    }
    inventory = InfrastructureInventory(machines={h: InfrastructureMachine(h) for h in machines})
    health: list[str] = []
    quick: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_kw: {h: f"192.0.2.{i}" for i, h in enumerate(machines, 1)},
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-123")
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)

    def fake_health(*_a, **_kw):
        if _kw.get("fallback") is False and not health:
            health.append("barrier")
            return False  # etcd unhealthy under a resurrected cp-01
        quick.append("advanced")
        return True

    monkeypatch.setattr(converge, "_health_or_kube_fallback", fake_health)
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: quick.append("upgrade")
    )

    with pytest.raises(ReconcileError, match="unhealthy before touching cp-01"):
        converge._reconcile_talos(
            cfg, machines, inventory, NetworkResult(),
            {("base",): "installer:v1.13.9"}, {("base",): "sch-123"},
            Path("talosconfig"), Path("kubeconfig"),
        )

    # the barrier ran with fallback=False for the control plane and no node was
    # upgraded past it -- cp-02 is never touched
    assert health == ["barrier"]
    assert quick == []


def test_reconcile_talos_advances_past_a_healthy_resumed_control_plane(monkeypatch):
    """A resumed control plane at the target that passes the health barrier does
    not block the rollout -- the next node is still upgraded and health-checked."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {
        "cp-01": SimpleNamespace(role="controlplane", extensions=("base",)),
        "w-01": SimpleNamespace(role="worker", extensions=("base",)),
    }
    inventory = InfrastructureInventory(machines={h: InfrastructureMachine(h) for h in machines})
    calls: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_kw: {h: f"192.0.2.{i}" for i, h in enumerate(machines, 1)},
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    # cp-01 sits on the target schematic; w-01 joins on the shared base image
    monkeypatch.setattr(
        converge.talosctl, "running_schematic",
        lambda *_a, **_kw: "sch-123" if _a[2] == "192.0.2.1" else "base-sch",
    )
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)

    def fake_health(*_a, **_kw):
        calls.append("health")
        return True

    monkeypatch.setattr(converge, "_health_or_kube_fallback", fake_health)
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: calls.append("upgrade")
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base",): "installer:v1.13.9"}, {("base",): "sch-123"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    # cp-01 at target passed its resumed barrier; w-01 was upgraded from base-sch
    assert calls.count("health") == 2  # resumed barrier + worker post-upgrade
    assert "upgrade" in calls


def test_reconcile_joined_reloads_inventory_to_see_created_nodes(monkeypatch):
    """The inventory converge holds from the network phase does not include the
    machines the compute phase just created, so _reconcile_joined must load the
    inventory again -- otherwise the membership guard skips every freshly
    created node and their extensions never land until another converge."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {"w-01": SimpleNamespace(role="worker", extensions=("base", "nvidia"))}
    created = InfrastructureInventory(machines={"w-01": InfrastructureMachine("w-01")})
    backend_calls: list[int] = []

    class ScriptedBackend:
        def load_inventory(self):
            backend_calls.append(len(backend_calls))
            return created  # a fresh OpenStack/Proxmox query reports the node

    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"w-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "base-sch")
    monkeypatch.setattr(
        converge.talosctl, "upgrade", lambda *_a, **_kw: upgrade_calls.append("upgrade")
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)

    refreshed = converge._reconcile_joined(
        cfg, machines, ScriptedBackend(), NetworkResult(),
        {("base", "nvidia"): "installer:v1.13.9"}, {("base", "nvidia"): "want-sch"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    # an inventory was loaded fresh on this call, not a pre-compute one handed in
    assert backend_calls == [0]
    # and its returned inventory carries the created node for finalize_machines
    assert refreshed.machines == created.machines
    # the created node was reconciled onto its configured extensions
    assert upgrade_calls == ["upgrade"]


# ---------------------------------------------------------------------------
# _wait_version: the schematic is the barrier for an extension-only upgrade
# ---------------------------------------------------------------------------

def test_wait_version_waits_for_the_schematic_through_a_reboot(monkeypatch):
    """An extension-only upgrade keeps the talos version constant, so the wait
    follows the running schematic: old -> unreachable mid-reboot -> target."""
    states = iter([("sch-old", None), None, ("sch-123", None)])
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")

    def fake_schematic(*_args):
        state = next(states)
        if state is None:
            raise subprocess.CalledProcessError(1, "talosctl")
        return state[0]

    monkeypatch.setattr(converge.talosctl, "running_schematic", fake_schematic)
    monkeypatch.setattr(converge.time, "sleep", lambda s: None)
    clock = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(clock))

    converge._wait_version(Path("talosconfig"), "ep", "cp-01", "v1.13.9",
                           want_schematic="sch-123", timeout_s=60)
    # reached the target without error


def test_wait_version_times_out_when_the_schematic_never_matches(monkeypatch):
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-old")
    monkeypatch.setattr(converge.time, "sleep", lambda s: None)
    clock = iter([0.0, 1.0, 62.0])  # deadline is 0 + 60
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="v1.13.9/sch-123"):
        converge._wait_version(Path("talosconfig"), "ep", "cp-01", "v1.13.9",
                               want_schematic="sch-123", timeout_s=60)

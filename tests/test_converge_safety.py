"""Destructive-action confirmation and final health-check safety."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.errors import ReconcileError, StateError
from taloscluster.infrastructure import (
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
)


class FakeBackend:
    name = "openstack"

    def __init__(self, mutations=None):
        self.mutations = mutations if mutations is not None else []

    def load_inventory(self):
        return InfrastructureInventory()

    def delete_machine(self, *_args):
        self.mutations.append("compute")

    def destroy_summary(self, _inventory):
        return "0 servers, 0 ports, 0 floating ips, network + router + security group"

    def destroy_resources(self, _inventory):
        self.mutations.append("destroy")


def test_scale_down_decline_happens_before_mutation(monkeypatch):
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit, match="aborted"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"),
            Path("kubeconfig"), assume_yes=False,
        )

    assert mutations == []


def test_scale_down_yes_skips_prompt_and_deletes(monkeypatch):
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), Path("talosconfig"),
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["drain", "reset", "delete", "compute"]


def test_scale_down_continues_when_drain_fails_on_notready_node(monkeypatch):
    """A previous run may have drained and reset the node but not deleted it.
    Re-running converge must skip the failed drain and still delete the node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(
        converge.kubectl,
        "drain",
        lambda *_a: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "drain")),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), Path("talosconfig"),
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["reset", "delete", "compute"]


def test_scale_down_aborts_when_drain_fails_on_ready_node(monkeypatch):
    """A PDB or eviction failure on a live node must abort, not delete the node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: True)
    monkeypatch.setattr(
        converge.kubectl,
        "drain",
        lambda *_a: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "drain")),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    with pytest.raises(ReconcileError, match="drain of old-worker failed"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, inventory, NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_deletes_notready_node_with_no_address(monkeypatch):
    """A node already shut down by a previous reset has no resolvable address
    and is NotReady in k8s. Scale-down must still delete it."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
        Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["delete", "compute"]


def test_scale_down_aborts_when_no_address_but_node_ready(monkeypatch):
    """No address + Ready node means a discovery failure, not an already-reset node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: True)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="no address for old-worker but node is Ready"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_when_drain_fails_and_status_unknown(monkeypatch):
    """kubectl API failure during drain must not be treated as NotReady."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: None)
    monkeypatch.setattr(
        converge.kubectl,
        "drain",
        lambda *_a: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "drain")),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    with pytest.raises(ReconcileError, match="drain of old-worker failed"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, inventory, NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_when_no_address_and_status_unknown(monkeypatch):
    """No address + unknown node status must not be treated as confirmed down."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: None)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="no address for old-worker but node is unknown"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def _cp_inventory(*names):
    return InfrastructureInventory(
        machines={
            n: InfrastructureMachine(
                n, attachments=(NetworkAttachment("cluster", f"192.0.2.{i % 250 + 1}"),)
            )
            for i, n in enumerate(names)
        }
    )


def test_scale_down_aborts_without_deleting_when_control_plane_reset_fails(monkeypatch):
    """A failed graceful reset must not delete the control-plane VM."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))

    def fail_reset(*_a, **_k):
        raise ReconcileError(
            "graceful reset of control plane testcluster-controlplane-03 failed"
        )
    monkeypatch.setattr(converge.talosctl, "reset", fail_reset)
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="control plane"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, _cp_inventory("testcluster-controlplane-03"),
            NetworkResult(), Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    # drained but the VM is NOT deleted once the reset failed
    assert mutations == ["drain"]


def test_scale_down_health_checks_between_successive_control_plane_removals(monkeypatch):
    """Removing one control plane at a time must health-check before the next."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names",
        lambda _kc: ["testcluster-controlplane-02", "testcluster-controlplane-03"],
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    checks: list[bool] = []
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *a, **k: checks.append(True) or True,
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, {},
        _cp_inventory("testcluster-controlplane-02", "testcluster-controlplane-03"),
        NetworkResult(), Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == [
        "drain", "reset", "delete", "compute",
        "drain", "reset", "delete", "compute",
    ]
    assert len(checks) == 1  # only between the two control-plane removals


def test_scale_down_stops_before_second_control_plane_when_unhealthy(monkeypatch):
    """An unhealthy cluster between control-plane removals must abort the rollout."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names",
        lambda _kc: ["testcluster-controlplane-02", "testcluster-controlplane-03"],
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *a, **k: False)

    with pytest.raises(ReconcileError, match="unhealthy after removing control plane"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {},
            _cp_inventory("testcluster-controlplane-02", "testcluster-controlplane-03"),
            NetworkResult(), Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    # first control plane removed, the second one left untouched
    assert mutations == ["drain", "reset", "delete", "compute"]


def test_destroy_decline_happens_before_plugin_teardown(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="testcluster")
    plugin_calls: list[str] = []
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: object())
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(
        converge, "_run_plugins", lambda *_a, **_kw: plugin_calls.append("destroy") or 0
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit, match="aborted"):
        converge.destroy(tmp_path)

    assert plugin_calls == []


def test_destroy_yes_skips_prompt_and_runs_plugin_teardown(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="testcluster")
    plugin_calls: list[str] = []
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: object())
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(
        converge, "_run_plugins", lambda *_a, **_kw: plugin_calls.append("destroy") or 0
    )
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    assert converge.destroy(tmp_path, assume_yes=True) == 0
    assert plugin_calls == ["destroy"]
    assert backend.mutations == ["destroy"]


def test_final_health_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: False)

    with pytest.raises(ReconcileError, match="unhealthy"):
        converge._require_final_health(
            Path("talosconfig"), "testcluster-controlplane-01", "192.0.2.5",
            Path("kubeconfig"),
        )


def test_resolve_cp1_address_prefers_network_result_then_inventory():
    cfg = SimpleNamespace(name="phoenix")
    host = "phoenix-controlplane-01"
    static = NetworkResult(
        machine_attachments={host: (NetworkAttachment(name="cluster", address="192.168.100.11"),)}
    )
    backend = SimpleNamespace(load_inventory=lambda: (_ for _ in ()).throw(AssertionError))
    assert converge._resolve_cp1_address(backend, cfg, static) == "192.168.100.11"

    # bridge mode: no static address; the guest-agent inventory answers
    inventory = InfrastructureInventory(
        machines={
            host: InfrastructureMachine(
                name=host,
                attachments=(NetworkAttachment(name="cluster", address="10.0.0.236"),),
            )
        }
    )
    backend = SimpleNamespace(load_inventory=lambda: inventory)
    assert converge._resolve_cp1_address(backend, cfg, NetworkResult()) == "10.0.0.236"


def test_resolve_cp1_address_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(converge.time, "sleep", lambda s: None)
    cfg = SimpleNamespace(name="phoenix")
    backend = SimpleNamespace(load_inventory=lambda: InfrastructureInventory())
    assert converge._resolve_cp1_address(backend, cfg, NetworkResult(), timeout_s=0) == ""


def _kubeconfig(tmp_path, server):
    path = tmp_path / "kubeconfig"
    path.write_text(
        "clusters:\n"
        "- name: other\n  cluster:\n    server: https://10.0.0.1:6443\n"
        f"- name: phoenix\n  cluster:\n    server: {server}\n"
    )
    return path


def test_recorded_endpoint_reads_the_cluster_entry_of_the_kubeconfig(tmp_path):
    path = _kubeconfig(tmp_path, "https://203.0.113.79:6443")
    assert converge._recorded_endpoint(path, "phoenix") == "203.0.113.79"
    assert converge._recorded_endpoint(path, "unknown") == ""
    assert converge._recorded_endpoint(tmp_path / "missing", "phoenix") == ""


def test_kubeapi_endpoint_move_is_reported_with_the_old_address(tmp_path, capsys):
    path = _kubeconfig(tmp_path, "https://203.0.113.79:6443")
    assert converge._endpoint_move(path, "phoenix", "203.0.113.77") == "203.0.113.79"
    assert "move kube-api endpoint 203.0.113.79 -> 203.0.113.77" in capsys.readouterr().out


def test_unchanged_or_unknown_kubeapi_endpoint_is_not_a_move(tmp_path):
    path = _kubeconfig(tmp_path, "https://203.0.113.79:6443")
    assert converge._endpoint_move(path, "phoenix", "203.0.113.79") == ""
    assert converge._endpoint_move(path, "phoenix", "") == ""  # endpoint still pending
    assert converge._endpoint_move(tmp_path / "missing", "phoenix", "203.0.113.77") == ""


# ---- talosctl endpoint: a real control plane, never the VIP -----------------

def _no_tailscale_cfg():
    return SimpleNamespace(name="phoenix", tailscale_enabled=False)


def test_talos_endpoint_prefers_static_then_inventory_never_the_vip(tmp_path):
    host = "phoenix-controlplane-01"
    refs = NetworkResult(
        machine_attachments={host: (NetworkAttachment(name="cluster", address="192.168.100.11"),)}
    )
    inv = InfrastructureInventory(
        machines={
            host: InfrastructureMachine(
                name=host, attachments=(NetworkAttachment(name="cluster", address="10.0.0.248"),)
            )
        }
    )
    assert converge._talos_endpoint(_no_tailscale_cfg(), refs, inv) == "192.168.100.11"
    assert converge._talos_endpoint(_no_tailscale_cfg(), NetworkResult(), inv) == "10.0.0.248"


def test_talos_endpoint_falls_back_to_the_recorded_talosconfig(tmp_path):
    path = tmp_path / "talosconfig"
    path.write_text(
        "context: phoenix\ncontexts:\n  phoenix:\n    endpoints:\n    - 10.0.0.248\n"
    )
    assert converge._talos_endpoint(_no_tailscale_cfg(), talosconfig=path) == "10.0.0.248"


def test_talos_endpoint_without_any_address_is_an_error(tmp_path):
    with pytest.raises(ReconcileError, match="no address known for phoenix-controlplane-01"):
        converge._talos_endpoint(_no_tailscale_cfg(), NetworkResult(), InfrastructureInventory())
    assert (
        converge._talos_endpoint(_no_tailscale_cfg(), talosconfig=tmp_path / "none", required=False)
        == "phoenix-controlplane-01"
    )


def test_talos_endpoint_with_tailscale_is_the_magicdns_name():
    cfg = SimpleNamespace(name="phoenix", tailscale_enabled=True)
    assert converge._talos_endpoint(cfg) == "phoenix-controlplane-01"


# ---- --reboot: one node at a time, control planes first ------------------

def test_reboot_nodes_is_serial_controlplanes_first_and_health_checked(monkeypatch, tmp_path):
    events = []
    cfg = SimpleNamespace(name="phoenix", tailscale_enabled=False)
    machines = {
        "phoenix-worker-01": SimpleNamespace(role="worker"),
        "phoenix-controlplane-01": SimpleNamespace(role="controlplane"),
        "phoenix-worker-02": SimpleNamespace(role="worker"),
    }
    addresses = {
        "phoenix-controlplane-01": "10.0.0.1",
        "phoenix-worker-01": "10.0.0.11",
        "phoenix-worker-02": "10.0.0.12",
    }
    inv = InfrastructureInventory(
        machines={
            h: InfrastructureMachine(name=h, attachments=(NetworkAttachment("cluster", a),))
            for h, a in addresses.items()
        }
    )
    backend = SimpleNamespace(
        restart_machine=lambda name, _inv: events.append(("restart", name))
    )
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *a, **k: {})
    monkeypatch.setattr(converge, "_wait_reachable", lambda tc, e, n: events.append(("up", n)))
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *a, **k: events.append(("health",)) or True,
    )

    converge._reboot_nodes(
        backend, cfg, machines, inv, NetworkResult(),
        {"phoenix-worker-02", "phoenix-controlplane-01"},  # worker-01 unchanged
        tmp_path / "talosconfig", tmp_path / "kubeconfig",
    )

    assert events == [
        ("restart", "phoenix-controlplane-01"), ("up", "10.0.0.1"), ("health",),
        ("restart", "phoenix-worker-02"), ("up", "10.0.0.12"), ("health",),
    ]


def test_reboot_rollout_stops_when_the_cluster_is_unhealthy(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="phoenix", tailscale_enabled=False)
    machines = {
        "phoenix-controlplane-01": SimpleNamespace(role="controlplane"),
        "phoenix-controlplane-02": SimpleNamespace(role="controlplane"),
    }
    addresses = {"phoenix-controlplane-01": "10.0.0.1", "phoenix-controlplane-02": "10.0.0.2"}
    inv = InfrastructureInventory(
        machines={
            h: InfrastructureMachine(name=h, attachments=(NetworkAttachment("cluster", a),))
            for h, a in addresses.items()
        }
    )
    restarted = []
    backend = SimpleNamespace(restart_machine=lambda name, _inv: restarted.append(name))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *a, **k: {})
    monkeypatch.setattr(converge, "_wait_reachable", lambda tc, e, n: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *a, **k: False)

    with pytest.raises(ReconcileError, match="unhealthy after rebooting phoenix-controlplane-01"):
        converge._reboot_nodes(
            backend, cfg, machines, inv, NetworkResult(), set(machines),
            tmp_path / "talosconfig", tmp_path / "kubeconfig",
        )
    assert restarted == ["phoenix-controlplane-01"]


def test_reboot_rollout_aborts_on_control_plane_when_health_fails_and_vip_responds(
    monkeypatch, tmp_path
):
    """A control-plane reboot with twice-failed `talosctl health` must abort the
    rollout even when the kube-api VIP answers -- the surviving control planes
    answer the VIP whether or not the rebooted node rejoined etcd, and the call
    site passes `fallback=role != "controlplane"` so the fallback is refused."""
    cfg = SimpleNamespace(name="phoenix", tailscale_enabled=False)
    machines = {
        "phoenix-controlplane-01": SimpleNamespace(role="controlplane"),
        "phoenix-controlplane-02": SimpleNamespace(role="controlplane"),
    }
    addresses = {"phoenix-controlplane-01": "10.0.0.1", "phoenix-controlplane-02": "10.0.0.2"}
    inv = InfrastructureInventory(
        machines={
            h: InfrastructureMachine(name=h, attachments=(NetworkAttachment("cluster", a),))
            for h, a in addresses.items()
        }
    )
    restarted = []
    backend = SimpleNamespace(restart_machine=lambda name, _inv: restarted.append(name))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *a, **k: {})
    monkeypatch.setattr(converge, "_wait_reachable", lambda tc, e, n: None)
    # talosctl health fails twice, but the kube-api VIP keeps answering -- this
    # is exactly the false healthy signal: the rollout must not advance to cp-02.
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        converge.talosctl, "health",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "health")),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    with pytest.raises(
        ReconcileError, match="unhealthy after rebooting phoenix-controlplane-01"
    ):
        converge._reboot_nodes(
            backend, cfg, machines, inv, NetworkResult(), set(machines),
            tmp_path / "talosconfig", tmp_path / "kubeconfig",
        )
    assert restarted == ["phoenix-controlplane-01"]

def test_health_or_kube_fallback_allows_kube_api_for_a_worker(monkeypatch):
    """A worker upgrade still falls back to kube-api readiness when talosctl
    health fails twice -- the worker is not an etcd member, so a responding
    kube-api on the surviving control planes is a fine signal."""
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        converge.talosctl, "health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "health")
        ),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    assert converge._health_or_kube_fallback(
        Path("talosconfig"), "cp-01", "192.0.2.10", Path("kubeconfig")
    )


def test_health_or_kube_fallback_refuses_fallback_for_a_control_plane(monkeypatch):
    """After a control-plane upgrade or reboot, a twice-failed `talosctl health`
    must NOT be papered over by a responding kube-api VIP: the surviving
    control planes answer the VIP even if the upgraded node never rejoined
    etcd, so accepting it would advance the rollout past a missing member."""
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        converge.talosctl, "health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "health")
        ),
    )
    # the VIP responds -- this is exactly the false healthy signal from the bug
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    assert not converge._health_or_kube_fallback(
        Path("talosconfig"), "cp-01", "192.0.2.10", Path("kubeconfig"),
        fallback=False,
    )


def test_controlplane_upgrade_rollout_aborts_when_health_fails_and_vip_responds(
    monkeypatch, tmp_path
):
    """The health phase must not advance the rollout to the next control plane
    when the previous one never rejoined etcd, even if the kube-api VIP answers.
    Callers pass `fallback=role != "controlplane"`, so a control-plane upgrade
    aborts while a worker upgrade would still fall back to kube-api readiness."""
    cfg = SimpleNamespace(name="phoenix", talos_version="v1.13.9")
    machines = {
        "phoenix-controlplane-01": SimpleNamespace(role="controlplane", extensions=("base",)),
        "phoenix-controlplane-02": SimpleNamespace(role="controlplane", extensions=("base",)),
    }
    inventory = InfrastructureInventory(
        machines={h: InfrastructureMachine(h) for h in machines}
    )
    upgraded = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_kw: {h: f"192.0.2.{i}" for i, h in enumerate(machines, 1)},
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.6")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "old-sch")
    monkeypatch.setattr(
        converge.talosctl, "upgrade",
        lambda *_a, **_kw: upgraded.append("upgrade"),
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)

    # talosctl health fails twice for the first control plane, and the kube-api
    # VIP answers; the call site passes fallback=role != "controlplane", so the
    # real _health_or_kube_fallback refuses the fallback and the rollout aborts.
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        converge.talosctl, "health",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "health")),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    with pytest.raises(
        ReconcileError, match="unhealthy after upgrading phoenix-controlplane-01"
    ):
        converge._reconcile_talos(
            cfg, machines, inventory, NetworkResult(),
            {("base",): "installer:v1.13.9"}, {("base",): "want-sch"},
            tmp_path / "talosconfig", tmp_path / "kubeconfig",
        )

    assert upgraded == ["upgrade"]


# ---- secrets: refused on an existing cluster, generated on first run -------

class _FakeState:
    """Minimal stand-in for taloscluster.state.State used by converge()."""

    def __init__(self, secrets_exist, secrets_path):
        self._exist = secrets_exist
        self.secrets_path = secrets_path
        self.generated = False

    def secrets_exist(self):
        return self._exist

    def write_secrets(self, _contents):
        self.generated = True


# raised by reconcile_network below to stop a first-run converge just past the
# state phase, proving the refusal did not trip and secrets were written
class _StatePhaseDone(Exception):
    pass


class _SecretsBackend:
    name = "openstack"
    installer_platform = "openstack"

    def __init__(self, inventory, stop_after_state=False):
        self.inventory = inventory
        self.stop_after_state = stop_after_state

    def load_inventory(self):
        return self.inventory

    def ensure_boot_artifact(self):
        return "image"

    def validate_machines(self, _machines, _inventory):
        return None

    def reconcile_network(self, _machines, _inventory):
        if self.stop_after_state:
            raise _StatePhaseDone
        return NetworkResult()


def _stub_converge(monkeypatch, tmp_path, state, backend):
    """Wire converge() so the state phase runs against fakes."""
    cfg = SimpleNamespace(
        name="phoenix", talos_version="v1.13.0",
        extension_sets=lambda: [()], machines={},
        kubernetes_version="v1.31.0", tailscale_enabled=True,
    )
    secrets = SimpleNamespace(tailscale_auth_key=None)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: secrets)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    # image-factory and talosctl side effects are out of scope for these tests
    # and talosctl is not installed in CI
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(converge.talosctl, "gen_secrets", lambda _v: "dummy secrets")
    return converge.converge(tmp_path)


def test_converge_refuses_to_generate_secrets_when_machines_exist(monkeypatch, tmp_path):
    inventory = InfrastructureInventory(
        machines={
            "phoenix-controlplane-01": InfrastructureMachine(
                "phoenix-controlplane-01",
                attachments=(NetworkAttachment("cluster", "10.0.0.1"),),
            )
        }
    )
    state = _FakeState(False, tmp_path / "talossecrets.yaml")
    monkeypatch.setattr(converge, "dry_run", lambda: False)

    with pytest.raises(StateError, match="irreplaceable identity"):
        _stub_converge(monkeypatch, tmp_path, state, _SecretsBackend(inventory))

    assert state.generated is False


def test_converge_generates_secrets_on_first_run_when_no_machines(monkeypatch, tmp_path):
    state = _FakeState(False, tmp_path / "talossecrets.yaml")
    monkeypatch.setattr(converge, "dry_run", lambda: False)

    # empty inventory -> no refusal; secrets written, then the network phase runs
    with pytest.raises(_StatePhaseDone):
        _stub_converge(
            monkeypatch, tmp_path, state,
            _SecretsBackend(InfrastructureInventory(), stop_after_state=True),
        )

    assert state.generated is True


# ---- unsupported machine-change preflight runs before any mutation -----------

class _RecordingBackend(_SecretsBackend):
    """Records every cluster mutation converge would perform."""

    def __init__(self, inventory=None, *, fail_validate=False, **kw):
        super().__init__(inventory if inventory is not None else InfrastructureInventory(), **kw)
        self.mutations: list[str] = []
        self.fail_validate = fail_validate

    def validate_machines(self, _machines, _inventory):
        if self.fail_validate:
            raise ReconcileError(
                "refusing to shrink the disk of testcluster-controlplane-01 "
                "from 40GB to 20GB"
            )

    def ensure_boot_artifact(self):
        self.mutations.append("image")
        return "image"

    def reconcile_network(self, _machines, _inventory):
        self.mutations.append("network")
        raise _StatePhaseDone  # stop converge after the mutable phases


def test_converge_disk_shrink_rejection_leaves_resources_unchanged(monkeypatch, tmp_path):
    """A Proxmox disk-shrink refusal in the validate phase fires before the
    image or network phases mutate anything -- not after converge already
    changed infrastructure or Talos configuration."""
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    backend = _RecordingBackend(fail_validate=True)

    with pytest.raises(ReconcileError, match="refusing to shrink the disk"):
        _stub_converge(monkeypatch, tmp_path, state, backend)

    assert backend.mutations == []


def test_converge_valid_changes_pass_the_preflight_and_mutate(monkeypatch, tmp_path):
    """When validate_machines accepts the changes, the image and network phases
    still run -- the preflight only refuses unsupported changes, never valid ones."""
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    backend = _RecordingBackend(fail_validate=False)

    with pytest.raises(_StatePhaseDone):
        _stub_converge(monkeypatch, tmp_path, state, backend)

    assert backend.mutations == ["image", "network"]

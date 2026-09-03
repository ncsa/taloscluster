"""Destructive-action confirmation and final health-check safety."""

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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
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
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="no address for old-worker but node is unknown"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


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
                attachments=(NetworkAttachment(name="cluster", address="172.29.21.236"),),
            )
        }
    )
    backend = SimpleNamespace(load_inventory=lambda: inventory)
    assert converge._resolve_cp1_address(backend, cfg, NetworkResult()) == "172.29.21.236"


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
    path = _kubeconfig(tmp_path, "https://141.142.36.79:6443")
    assert converge._recorded_endpoint(path, "phoenix") == "141.142.36.79"
    assert converge._recorded_endpoint(path, "unknown") == ""
    assert converge._recorded_endpoint(tmp_path / "missing", "phoenix") == ""


def test_kubeapi_endpoint_move_is_reported_with_the_old_address(tmp_path, capsys):
    path = _kubeconfig(tmp_path, "https://141.142.36.79:6443")
    assert converge._endpoint_move(path, "phoenix", "141.142.36.77") == "141.142.36.79"
    assert "move kube-api endpoint 141.142.36.79 -> 141.142.36.77" in capsys.readouterr().out


def test_unchanged_or_unknown_kubeapi_endpoint_is_not_a_move(tmp_path):
    path = _kubeconfig(tmp_path, "https://141.142.36.79:6443")
    assert converge._endpoint_move(path, "phoenix", "141.142.36.79") == ""
    assert converge._endpoint_move(path, "phoenix", "") == ""  # endpoint still pending
    assert converge._endpoint_move(tmp_path / "missing", "phoenix", "141.142.36.77") == ""


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
                name=host, attachments=(NetworkAttachment(name="cluster", address="172.29.21.248"),)
            )
        }
    )
    assert converge._talos_endpoint(_no_tailscale_cfg(), refs, inv) == "192.168.100.11"
    assert converge._talos_endpoint(_no_tailscale_cfg(), NetworkResult(), inv) == "172.29.21.248"


def test_talos_endpoint_falls_back_to_the_recorded_talosconfig(tmp_path):
    path = tmp_path / "talosconfig"
    path.write_text(
        "context: phoenix\ncontexts:\n  phoenix:\n    endpoints:\n    - 172.29.21.248\n"
    )
    assert converge._talos_endpoint(_no_tailscale_cfg(), talosconfig=path) == "172.29.21.248"


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

"""Destructive-action confirmation and final health-check safety."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.errors import ConfigError, ReconcileError, StateError
from taloscluster.infrastructure import (
    Endpoint,
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


def test_scale_down_refuses_even_controlplane_count(monkeypatch):
    """Removing a control plane when the desired count is even would break etcd
    quorum, so scale-down must refuse before prompting or mutating."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 2})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(
        ReconcileError,
        match="refusing to remove controlplane testcluster-controlplane-03",
    ):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
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


def test_scale_down_aborts_addressless_control_plane_when_still_a_member(monkeypatch, tmp_path):
    """An addressless NotReady control plane that is still registered as a talos
    etcd member must abort, not delete the VM: NotReady does not prove it left
    etcd, so deleting would bypass the reset-failure protection."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    # the failed-reset node is still an (unreachable) etcd member
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-03": ""},
    )

    with pytest.raises(ReconcileError, match="not established"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            talosconfig, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_addressless_control_plane_when_discovery_empty(monkeypatch):
    """Without a member list we cannot positively establish that an addressless
    control plane left etcd, so relying on NotReady alone must abort."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="not established"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_deletes_addressless_control_plane_when_removal_established(
    monkeypatch, tmp_path
):
    """An addressless NotReady control plane whose node is confirmed absent from
    the member list has positively left etcd and can be deleted."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    # discovery works and the removed node is not among the current members
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-02": "192.0.2.2"},
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
        talosconfig, Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["delete", "compute"]


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


def test_scale_down_real_health_refuses_fallback_between_control_plane_removals(
    monkeypatch,
):
    """Between control-plane removals the real _health_or_kube_fallback must
    refuse the kube-api fallback: talosctl health fails twice and the VIP
    answers (the surviving control planes serve it even when the removed member
    never left etcd), so the rollout must abort before removing the next control
    plane instead of trusting kube-api readiness."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names",
        lambda _kc: ["testcluster-controlplane-02", "testcluster-controlplane-03"],
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {},
    )
    # talosctl health fails twice and the kube-api VIP answers: the health
    # barrier between control-plane removals is exercised for real, not stubbed.
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        converge.talosctl, "health",
        lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "health")
        ),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    with pytest.raises(ReconcileError, match="unhealthy after removing control plane"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {},
            _cp_inventory("testcluster-controlplane-02", "testcluster-controlplane-03"),
            NetworkResult(), Path("talosconfig"), Path("kubeconfig"), assume_yes=True,
        )

    # only the first control plane was removed; the second is untouched because
    # the real helper refused the kube-api fallback for a control plane
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


def test_destroy_continues_teardown_after_plugin_destroy_failure(monkeypatch, tmp_path):
    """A plugin whose destroy hook fails must not keep the cluster alive: core
    still tears the infrastructure down, and the command exits nonzero so the
    stale external registration is noticed."""
    cfg = SimpleNamespace(name="testcluster")
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: object())
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_kw: 1)
    monkeypatch.setattr("builtins.input", lambda _prompt: cfg.name)

    assert converge.destroy(tmp_path, assume_yes=True) == 1
    assert backend.mutations == ["destroy"]


def test_final_health_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: False)

    with pytest.raises(ReconcileError, match="unhealthy"):
        converge._require_final_health(
            Path("talosconfig"), "testcluster-controlplane-01", "192.0.2.5",
            Path("kubeconfig"),
        )


# ---- kube-api probe: never read a single failure as a fresh cluster ---------

def test_kube_up_retries_on_a_transient_failure_then_succeeds(monkeypatch, tmp_path):
    """A single failed probe must not be read as a fresh cluster: _kube_up
    retries, and when a later attempt answers the cluster is treated as UP."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    calls = {"n": 0}
    monkeypatch.setattr(
        converge.kubectl, "cluster_up", lambda _kc: (calls.__setitem__("n", calls["n"] + 1)
                                                     or calls["n"] >= 2)
    )
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(kubeconfig, _cp_inventory("cp-01")) is True
    assert calls["n"] == 2  # first probe failed, the retry answered
    assert warns == []  # no loud warning for a cluster that came back


def test_kube_up_warns_and_reports_down_when_infra_exists_but_api_never_answers(
    monkeypatch, tmp_path
):
    """When machines already exist (with a kubeconfig written earlier) yet the
    API does not answer after every retry, _kube_up reports down and warns
    loudly that this is NOT a fresh cluster -- so no nodes are recreated or
    bootstrap attempted."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(kubeconfig, _cp_inventory("phoenix-controlplane-01")) is False

    joined = " ".join(warns)
    assert "machine(s) already exist" in joined
    assert "NOT a fresh cluster" in joined


def test_kube_up_skips_retry_when_there_is_no_prior_kubeconfig(monkeypatch, tmp_path):
    """An interrupted first run has machines but NO kubeconfig, so the cluster
    was never bootstrapped. There is nothing to probe against (kubectl
    short-circuits on the missing file), so _kube_up returns straight down
    without wasting retry sleeps or warning that the cluster is not fresh -- the
    caller will bootstrap it."""
    kubeconfig = tmp_path / "kubeconfig"  # never created
    monkeypatch.setattr(
        converge.kubectl, "cluster_up",
        lambda _kc: pytest.fail("a probe cannot succeed without a kubeconfig"),
    )
    monkeypatch.setattr(converge.time, "sleep", lambda _s: pytest.fail("must not sleep"))
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(kubeconfig, _cp_inventory("phoenix-controlplane-01")) is False
    assert warns == []


def test_kube_up_does_not_warn_for_a_genuinely_fresh_cluster(monkeypatch, tmp_path):
    """No machines means no existing infrastructure, so silence is fine -- this
    is the legitimate bootstrap case."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(kubeconfig, InfrastructureInventory()) is False
    assert warns == []


def test_kube_up_recovers_a_missing_kubeconfig_from_the_restored_identity(
    monkeypatch, tmp_path
):
    """A lost management machine restores the identity but not the derived
    kubeconfig. With machines + identity present, recovery regenerates the file
    from the restored talos identity and _kube_up probes it -- so a recovered
    cluster reads UP instead of a never-bootstrapped fresh one (which would skip
    scale-down, apply and upgrade and re-bootstrap the existing infra)."""
    kubeconfig = tmp_path / "kubeconfig"  # missing, like a freshly recovered machine

    def recover(*_a, **_k):
        kubeconfig.write_text("clusters: []\n")
        return True

    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", recover)
    calls: list[bool] = []
    monkeypatch.setattr(converge.kubectl, "cluster_up",
                        lambda _kc: calls.append(True) or True)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(
        kubeconfig, _cp_inventory("phoenix-controlplane-01"),
        recover=True, talosconfig=Path("talosconfig"),
        endpoint="phoenix-controlplane-01", node="phoenix-controlplane-01",
    ) is True
    assert kubeconfig.is_file()
    assert warns == []


def test_kube_up_plan_reports_a_recovered_cluster_up_in_dry_run(monkeypatch, tmp_path):
    """The `plan` command (dry_run) cannot probe the real cluster, but a recovery
    prognosis must read UP so plan reports reconcile-as-existing instead of the
    misleading "will bootstrap if needed" the fresh path prints (which contradicts
    the real converge, that will recover the kubeconfig and reconcile the cluster
    as existing)."""
    kubeconfig = tmp_path / "kubeconfig"  # missing, like a freshly recovered machine
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", lambda *_a, **_k: True)
    # plan must not probe or reach for the node: no kubeconfig was written
    monkeypatch.setattr(
        converge.kubectl, "cluster_up",
        lambda _kc: pytest.fail("dry run must not probe the kube-api"),
    )
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(
        kubeconfig, _cp_inventory("phoenix-controlplane-01"),
        recover=True, talosconfig=Path("talosconfig"),
        endpoint="phoenix-controlplane-01", node="phoenix-controlplane-01",
    ) is True
    assert not kubeconfig.exists()
    assert warns == []


def test_kube_up_recovered_then_api_never_answers_is_existing_but_down(
    monkeypatch, tmp_path
):
    """Recovery materialises a kubeconfig but the api still does not answer
    (an unhealthy recovered cluster). _kube_up must then warn it is NOT a fresh
    cluster, so the caller refuses to recreate nodes or re-bootstrap."""
    kubeconfig = tmp_path / "kubeconfig"

    def recover(*_a, **_k):
        kubeconfig.write_text("clusters: []\n")
        return True

    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", recover)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(
        kubeconfig, _cp_inventory("phoenix-controlplane-01"),
        recover=True, talosconfig=Path("talosconfig"),
        endpoint="phoenix-controlplane-01", node="phoenix-controlplane-01",
    ) is False
    joined = " ".join(warns)
    assert "machine(s) already exist" in joined
    assert "NOT a fresh cluster" in joined


def test_kube_up_recovery_producing_no_kubeconfig_stays_fresh(monkeypatch, tmp_path):
    """A never-bootstrapped first run (interrupted before bootstrap) has machines
    but the node serves no kubeconfig, so recovery writes nothing and _kube_up
    reports straight down for the caller to bootstrap -- no misleading warning
    about a wrongly-classified existing cluster."""
    kubeconfig = tmp_path / "kubeconfig"  # stays missing
    # no `_recover_missing_kubeconfig` stub that writes a file -> returns False
    monkeypatch.setattr(converge, "_recover_missing_kubeconfig", lambda *_a, **_k: False)
    monkeypatch.setattr(
        converge.kubectl, "cluster_up",
        lambda _kc: pytest.fail("a probe cannot succeed without a kubeconfig"),
    )
    monkeypatch.setattr(converge.time, "sleep", lambda _s: pytest.fail("must not sleep"))
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._kube_up(
        kubeconfig, _cp_inventory("phoenix-controlplane-01"),
        recover=True, talosconfig=Path("talosconfig"),
        endpoint="phoenix-controlplane-01", node="phoenix-controlplane-01",
    ) is False
    assert warns == []


def test_recover_missing_kubeconfig_fetches_from_the_control_plane(monkeypatch, tmp_path):
    """Recovery waits for the restored control plane to answer talos, then fetches
    a fresh kubeconfig through the restored identity and reports success."""
    kubeconfig = tmp_path / "kubeconfig"
    monkeypatch.setattr(converge, "_wait_reachable", lambda *a, **k: None)
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig",
        lambda _t, _e, _n, out: out.write_text("clusters: []\n"),
    )

    assert converge._recover_missing_kubeconfig(
        Path("talosconfig"), "phoenix-controlplane-01",
        "phoenix-controlplane-01", kubeconfig,
    ) is True
    assert kubeconfig.is_file() and kubeconfig.stat().st_size > 0


def test_recover_missing_kubeconfig_clears_a_partial_file_on_failure(
    monkeypatch, tmp_path
):
    """A failed fetch (a never-bootstrapped node serves no kubeconfig) must report
    failure and clear any partial file it left, so the caller still reads the
    cluster as fresh rather than probing garbage."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("truncated")  # garbage a failed fetch left behind
    monkeypatch.setattr(converge, "_wait_reachable", lambda *a, **k: None)

    def fail_kubeconfig(*_a, **_k):
        raise subprocess.CalledProcessError(1, "talosctl kubeconfig")

    monkeypatch.setattr(converge.talosctl, "kubeconfig", fail_kubeconfig)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert converge._recover_missing_kubeconfig(
        Path("talosconfig"), "phoenix-controlplane-01",
        "phoenix-controlplane-01", kubeconfig,
    ) is False
    assert not kubeconfig.exists()
    assert "could not recover the kubeconfig" in " ".join(warns)


def test_recover_missing_kubeconfig_reports_but_does_not_write_in_dry_run(
    monkeypatch, tmp_path
):
    """plan/dry-run must not mutate: recovery reports the fetch it would perform
    but writes no kubeconfig (and does not reach for the node). It signals the
    prognosis (True) so the caller reports the cluster UP instead of "will
    bootstrap if needed"."""
    kubeconfig = tmp_path / "kubeconfig"
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig",
        lambda *_a: pytest.fail("dry run must not fetch a kubeconfig"),
    )
    monkeypatch.setattr(
        converge, "_wait_reachable", lambda *_a, **_k: pytest.fail("dry run must not wait")
    )
    actions: list[str] = []
    monkeypatch.setattr(converge, "action", actions.append)

    assert converge._recover_missing_kubeconfig(
        Path("talosconfig"), "phoenix-controlplane-01",
        "phoenix-controlplane-01", kubeconfig,
    ) is True
    assert "recover kubeconfig" in " ".join(actions)
    assert not kubeconfig.exists()


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


def _endpoint_move_refs():
    host = "phoenix-controlplane-01"
    return host, NetworkResult(
        kubernetes=Endpoint(advertised_address="203.0.113.77"),
        machine_attachments={host: (NetworkAttachment(name="cluster", address="192.168.100.11"),)},
    ), InfrastructureInventory(
        machines={
            host: InfrastructureMachine(
                name=host, attachments=(NetworkAttachment(name="cluster", address="10.0.0.248"),)
            )
        }
    )


def test_finish_endpoint_move_writes_a_new_kubeconfig_and_waits(monkeypatch, tmp_path):
    """After the machine configs carry the new endpoint the move finishes with a
    fresh kubeconfig fetched from a control plane and a wait for the api."""
    host, refs, inv = _endpoint_move_refs()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("old")
    reached: list[str] = []
    monkeypatch.setattr(
        converge, "_wait_reachable", lambda _tc, e, n: reached.append(n) or None
    )
    fetched: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig",
        lambda _tc, e, _n, kc: fetched.append(e) or kc.write_text("new"),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)

    converge._finish_endpoint_move(_no_tailscale_cfg(), refs, inv, tmp_path / "t", kubeconfig)

    assert reached == ["192.168.100.11"]
    assert fetched == ["192.168.100.11"]  # fetched from a real control plane, not the VIP
    assert kubeconfig.read_text() == "new"


def test_finish_endpoint_move_raises_when_kube_api_never_answers(monkeypatch, tmp_path):
    host, refs, inv = _endpoint_move_refs()
    kubeconfig = tmp_path / "kubeconfig"
    monkeypatch.setattr(converge, "_wait_reachable", lambda *_a, **_k: None)
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig",
        lambda *_a, **_k: kubeconfig.write_text("new"),
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    clock = iter([0.0, 301.0])
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(clock))

    with pytest.raises(ReconcileError, match="203.0.113.77"):
        converge._finish_endpoint_move(
            _no_tailscale_cfg(), refs, inv, tmp_path / "totalsconfig", kubeconfig
        )


def test_apply_existing_configs_serializes_an_endpoint_move(monkeypatch):
    """On a kube-api endpoint move the control planes are re-configured first
    and settled one at a time, then the kubeconfig is regressed from a control
    plane, before the worker pass runs -- the old endpoint dies with the old
    VIP, and the workers need kubectl to see the nodes on the new one."""
    machines, inv, configs = _apply_configs_fixtures()
    calls: list[tuple] = []
    monkeypatch.setattr(
        converge, "_apply_configs",
        lambda cfg, m, i, r, c, t, k, **kw: calls.append(
            ("apply", kw.get("roles"), kw.get("settle"))
        ),
    )
    monkeypatch.setattr(
        converge, "_finish_endpoint_move", lambda *_a, **_k: calls.append(("move", None))
    )

    converge._apply_existing_configs(
        SimpleNamespace(name="phoenix", tailscale_enabled=True),
        machines, inv, NetworkResult(), configs,
        Path("talosconfig"), Path("kubeconfig"), moving_from="203.0.113.5",
    )

    # control planes settle one at a time, the kubeconfig is regressed, and only
    # then are the workers applied in a single pass
    assert calls == [
        ("apply", ("controlplane",), True),
        ("move", None),
        ("apply", ("worker",), None),
    ]


def test_apply_existing_configs_without_a_move_applies_all_roles(monkeypatch):
    """An unchanged endpoint takes the plain single apply over both roles with
    the default settle -- no endpoint-mode three-phase sequencing."""
    machines, inv, configs = _apply_configs_fixtures()
    calls: list[tuple] = []
    monkeypatch.setattr(
        converge, "_apply_configs",
        lambda cfg, m, i, r, c, t, k, **kw: calls.append(("apply", kw.get("roles"))),
    )
    monkeypatch.setattr(
        converge, "_finish_endpoint_move",
        lambda *_a, **_k: calls.append(("move", None)) or pytest.fail("no move expected"),
    )

    converge._apply_existing_configs(
        SimpleNamespace(name="phoenix", tailscale_enabled=True),
        machines, inv, NetworkResult(), configs,
        Path("talosconfig"), Path("kubeconfig"), moving_from="",
    )

    assert calls == [("apply", None)]  # default roles, settle left on


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

# ---- _apply_configs: control planes settle by default, waiting out reboots --

def _apply_configs_fixtures():
    machines = {
        "phoenix-controlplane-01": SimpleNamespace(role="controlplane"),
        "phoenix-controlplane-02": SimpleNamespace(role="controlplane"),
        "phoenix-worker-01": SimpleNamespace(role="worker"),
    }
    inv = _cp_inventory(*machines)
    configs = {h: f"config:{h}" for h in machines}
    return machines, inv, configs


def _no_op_reachable(monkeypatch):
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *a, **k: {})
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)


_APPLY_MACHINES = ["phoenix-controlplane-01", "phoenix-controlplane-02", "phoenix-worker-01"]
# _cp_inventory numbers these 192.0.2.1, .2, .3 in name order
_APPLY_ADDR = {h: f"192.0.2.{i + 1}" for i, h in enumerate(_APPLY_MACHINES)}


def test_apply_configs_settles_control_planes_by_default(monkeypatch):
    """`settle` defaults to True: a config apply that reports a reboot waits for
    each control plane to actually go down (a reboot started), come back, and
    pass a health check before the next one is touched, so a restart-requiring
    patch never restarts every control plane at once. Workers are applied last
    in a single pass and never settled."""
    machines, inv, configs = _apply_configs_fixtures()
    _no_op_reachable(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge.talosctl, "apply_config",
        lambda _tc, _e, node, _cfg: events.append(("apply", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_down",
        lambda _tc, _e, node: events.append(("down", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_reachable",
        lambda _tc, _e, node: events.append(("up", node)),
    )
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *_a, **_k: events.append(("health", None)) or True,
    )

    # note: settle is left at its default -- the caller at the normal-path call
    # site (converge, endpoint unchanged) passes no settle argument
    converge._apply_configs(
        SimpleNamespace(name="phoenix", tailscale_enabled=True),
        machines, inv, NetworkResult(), configs,
        Path("talosconfig"), Path("kubeconfig"),
    )

    cp1, cp2, worker = (_APPLY_ADDR[h] for h in _APPLY_MACHINES)
    assert events == [
        ("apply", cp1), ("down", cp1), ("up", cp1), ("health", None),
        ("apply", cp2), ("down", cp2), ("up", cp2), ("health", None),
        ("apply", worker),
    ]


def test_apply_configs_live_apply_skips_settle(monkeypatch):
    """A live/no-op apply reports that no reboot is pending, so no settle wait
    runs and the node is never health-checked off the back of a down: a silent
    apply never took the node down, so there is no restart to settle and no
    quorum risk."""
    machines, inv, configs = _apply_configs_fixtures()
    _no_op_reachable(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge.talosctl, "apply_config",
        lambda _tc, _e, node, _cfg: events.append(("apply", node)) or False,
    )
    monkeypatch.setattr(converge, "_wait_down", lambda *_a, **_k: events.append(("down", None)))
    monkeypatch.setattr(converge, "_wait_reachable", lambda *_a, **_k: events.append(("up", None)))
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *_a, **_k: events.append(("health", None)) or True,
    )

    converge._apply_configs(
        SimpleNamespace(name="phoenix", tailscale_enabled=True),
        machines, inv, NetworkResult(), configs,
        Path("talosconfig"), Path("kubeconfig"),
    )

    # every apply is a live/no-op, so none of the settle machinery fires
    assert events == [("apply", _APPLY_ADDR[h]) for h in _APPLY_MACHINES]


def test_apply_configs_refuses_unresolved_reboot(monkeypatch):
    """When a control-plane apply reports a reboot but apid never drops within
    the settle grace window, converge refuses to touch the next control plane
    instead of warn-and-continue: a node that never visibly rebooted may be
    stuck, and advancing past it costs quorum on the next restart."""
    machines, inv, configs = _apply_configs_fixtures()
    _no_op_reachable(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge.talosctl, "apply_config",
        lambda _tc, _e, node, _cfg: events.append(("apply", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_down",
        lambda _tc, _e, node: events.append(("down", node)) or False,
    )
    monkeypatch.setattr(converge, "_wait_reachable", lambda *_a, **_k: events.append(("up", None)))
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_k: True)

    with pytest.raises(ReconcileError, match="settle grace window"):
        converge._apply_configs(
            SimpleNamespace(name="phoenix", tailscale_enabled=True),
            machines, inv, NetworkResult(), configs,
            Path("talosconfig"), Path("kubeconfig"),
        )

    # control plane 1 is applied and its grace window expires unresolved; no
    # second control plane is ever touched
    cp1 = _APPLY_ADDR["phoenix-controlplane-01"]
    assert events == [("apply", cp1), ("down", cp1)]


def test_apply_configs_aborts_when_cluster_unhealthy_after_reboot(monkeypatch):
    """Even after an observed reboot (the node went down and its apid answered
    again), apid reachability alone does not prove the node rejoined etcd --
    converge requires cluster health before advancing, and aborts the rollout
    rather than touch another control plane past a member that never came back."""
    machines, inv, configs = _apply_configs_fixtures()
    _no_op_reachable(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge.talosctl, "apply_config",
        lambda _tc, _e, node, _cfg: events.append(("apply", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_down",
        lambda _tc, _e, node: events.append(("down", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_reachable",
        lambda _tc, _e, node: events.append(("up", node)),
    )
    # the control plane does not pass talosctl health after coming back -- a
    # responding apid was never enough
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *_a, **_k: events.append(("health", None)) or False,
    )

    with pytest.raises(ReconcileError, match="cluster unhealthy"):
        converge._apply_configs(
            SimpleNamespace(name="phoenix", tailscale_enabled=True),
            machines, inv, NetworkResult(), configs,
            Path("talosconfig"), Path("kubeconfig"),
        )

    cp1 = _APPLY_ADDR["phoenix-controlplane-01"]
    assert events == [("apply", cp1), ("down", cp1), ("up", cp1), ("health", None)]


def test_wait_down_returns_true_only_after_apid_stops_answering(monkeypatch):
    """_wait_down returns True the moment the node's apid stops answering, and
    False if it never drops (a live apply) -- the distinction that makes the
    settle wait real instead of relying on apid continuing to answer through a
    drain."""
    drops: list[bool] = [True, True, False]  # stay up, stay up, then down
    monkeypatch.setattr(converge.talosctl, "reachable", lambda *_a, **_k: drops.pop(0))
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    assert converge._wait_down(talosconfig=Path("tc"), endpoint="e", node="n",
                               grace_s=60, interval_s=5) is True

    # a node that stays up for the whole grace window is a live apply: the poll
    # loop must run until the deadline before returning False (grace_s=0 alone
    # would never enter the loop, so the expiration path would be untested)
    monkeypatch.setattr(converge.talosctl, "reachable", lambda *_a, **_k: True)
    expiry_clock = iter([0, 0, 5, 10])  # deadline, then each poll check
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(expiry_clock))
    assert converge._wait_down(talosconfig=Path("tc"), endpoint="e", node="n",
                               grace_s=10, interval_s=5) is False


def test_reachability_timeout_points_at_troubleshooting(monkeypatch):
    """A node that never answers times out with a pointer to the troubleshooting
    guide (the old message referenced a README section that no longer exists)."""
    monkeypatch.setattr(converge.talosctl, "reachable", lambda *_a, **_k: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    # deadline is computed from the first monotonic read, then the poll check
    # reads a later one -- jump past the deadline so the loop exits immediately
    monkeypatch.setattr(converge.time, "monotonic", iter([0, 1]).__next__)

    with pytest.raises(TimeoutError) as exc:
        converge._wait_reachable(Path("tc"), "cp-01", "192.0.2.1", timeout_s=1)

    assert "did not become reachable" in str(exc.value)
    assert ("docs/troubleshooting.md"
            "#recreating-a-cluster-reuses-stale-headscale-entries") in str(exc.value)
    assert "README" not in str(exc.value)


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


# converge() must not recreate nodes for an existing-but-unreachable cluster

class _ExistingDownBackend(_SecretsBackend):
    """Runs converge through the compute phase and records whether it would
    recreate existing nodes."""

    def __init__(self, inventory):
        super().__init__(inventory)
        self.mutations: list[str] = []
        self.plugin_converge: list[str] = []

    def default_node_tags(self):
        return {}

    def talos_contribution(self, _m, _refs):
        return {}

    def reconcile_network(self, _machines, _inventory):
        return NetworkResult(
            kubernetes=SimpleNamespace(advertised_address="192.0.2.5", vip="192.0.2.5"),
            ingress=SimpleNamespace(advertised_address="192.0.2.6", vip="192.0.2.6"),
        )

    def reconcile_machines(self, _machines, _inventory, _boot_image, _configs):
        self.mutations.append("reconcile")
        return set()

    def provider_status(self):
        return {}

    def finalize_machines(self, _inventory):
        return None


def _stub_converge_full(monkeypatch, tmp_path, state, backend, machine_cfg,
                        *, stub_health=False):
    """Wire converge() to run through the compute phase against fakes.

    `stub_health` replaces the health phase with no-ops -- used only for a path
    where the cluster is EXPECTED to come up (e.g. an interrupted first run that
    bootstraps); callers that want to observe that the health phase is skipped
    on an unreachable cluster leave it False and patch the phase themselves.
    """
    cfg = SimpleNamespace(
        name="phoenix", talos_version="v1.13.0", kubernetes_version="v1.31.0",
        extension_sets=lambda: [()],
        machines={"phoenix-controlplane-01": SimpleNamespace(role="controlplane")},
        tailscale_enabled=True,
    )
    secrets = SimpleNamespace(tailscale_auth_key=None)
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: secrets)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg, _secrets: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge.talosctl, "gen_talosconfig", lambda *a, **k: "talosconfig")
    monkeypatch.setattr(converge.machineconfig, "build_configs", lambda *a, **k: machine_cfg)
    monkeypatch.setattr(
        converge, "_run_plugins",
        lambda *a, **kw: backend.plugin_converge.append("converge") or 0,
    )
    if stub_health:
        monkeypatch.setattr(converge, "_require_final_health", lambda *a, **k: None)
        monkeypatch.setattr(converge, "_wait_nodes_ready", lambda *a, **k: None)
        monkeypatch.setattr(
            converge, "_reconcile_joined", lambda *a, **k: FakeBackend().load_inventory()
        )
        monkeypatch.setattr(converge.kubectl, "get_nodes_wide", lambda _kc: "")
    return converge.converge(tmp_path)


def test_converge_does_not_recreate_existing_nodes_when_api_is_down(monkeypatch, tmp_path):
    """When machines already exist and a kubeconfig was written earlier but the
    kube-api does not answer after every retry, converge treats the cluster as
    existing (not fresh): it warns loudly, does NOT reconcile/create nodes as if
    they were missing, defers the mutating plugin converge hooks (which would
    act against a cluster we cannot reach), skips the health phase (meaningless
    on a cluster known unreachable), and reports an incomplete converge with a
    nonzero exit instead of returning clean."""
    inventory = _cp_inventory("phoenix-controlplane-01")
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    backend = _ExistingDownBackend(inventory)
    (tmp_path / "kubeconfig").write_text("clusters: []\n")  # bootstrapped earlier
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)
    # the health phase must not run on an unreachable cluster -- it would hang
    # on talosctl retries or a 15m node_summary no-op -- so fail if reached
    monkeypatch.setattr(
        converge, "_require_final_health",
        lambda *a, **k: pytest.fail("health phase must be skipped when existing_but_down"),
    )
    monkeypatch.setattr(
        converge, "_wait_nodes_ready",
        lambda *a, **k: pytest.fail("node-wait must be skipped when existing_but_down"),
    )
    monkeypatch.setattr(
        converge, "_reconcile_joined",
        lambda *a, **k: pytest.fail("post-join reconcile must be skipped"),
    )
    monkeypatch.setattr(converge.kubectl, "get_nodes_wide",
                        lambda _kc: pytest.fail("status must be skipped"))

    assert _stub_converge_full(
        monkeypatch, tmp_path, state, backend,
        {"phoenix-controlplane-01": "config"},
    ) == 1  # an unreachable cluster is an incomplete converge, reported as failed

    assert backend.mutations == []  # reconcile_machines never ran -> no recreate
    assert backend.plugin_converge == []  # mutating plugin converge hooks deferred
    joined = " ".join(warns)
    assert "machine(s) already exist" in joined
    assert "refusing to recreate nodes" in joined
    assert "deferring plugin converge hooks" in joined


def test_converge_rebootstraps_an_interrupted_first_run(monkeypatch, tmp_path):
    """Machines existing in the inventory with NO kubeconfig means a first run
    that was interrupted before it reached bootstrap -- the cluster was never
    bootstrapped. Such a cluster is still fresh, so converge must attempt
    bootstrap (and write the kubeconfig) rather than refuse it as an
    'existing but down' cluster that can never come back."""
    inventory = _cp_inventory("phoenix-controlplane-01")
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    backend = _ExistingDownBackend(inventory)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    events: list[str] = []
    monkeypatch.setattr(converge, "_wait_reachable", lambda *a, **k: events.append("reachable"))
    monkeypatch.setattr(converge.talosctl, "bootstrap", lambda *a, **k: events.append("bootstrap"))
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig", lambda *a, **k: events.append("kubeconfig")
    )
    warns: list[str] = []
    monkeypatch.setattr(converge, "warn", warns.append)

    assert _stub_converge_full(
        monkeypatch, tmp_path, state, backend,
        {"phoenix-controlplane-01": "config"}, stub_health=True,
    ) == 0

    # bootstrap and the phase-9 kubeconfig both ran despite machines existing,
    # because there was no kubeconfig to prove an earlier bootstrap
    assert "reachable" in events and "bootstrap" in events and "kubeconfig" in events
    joined = " ".join(warns)
    assert "NOT a fresh cluster" not in joined


def test_converge_does_not_replace_the_prebootstrap_identity_through_bootstrap(
    monkeypatch, tmp_path
):
    """An interrupted first run mints talossecrets.yaml in the secrets phase --
    before the network, compute or bootstrap phases -- and bootstrap never
    replaces it. So the identity a backup predating the bootstrap captured is the
    running identity: when converge re-runs to bootstrap the unfinished cluster,
    it must not re-mint the secrets, leaving that preserved pre-bootstrap backup
    byte-identical and valid (see docs/backup.md)."""
    secrets_path = tmp_path / "talossecrets.yaml"
    backup = "cluster-CA-and-tokens-captured-after-the-interrupted-run\n"
    secrets_path.write_text(backup)
    writes: list[str] = []

    class _PersistingState(_FakeState):
        def write_secrets(self, contents):
            writes.append(contents)
            secrets_path.write_text(contents)

    state = _PersistingState(True, secrets_path)
    backend = _ExistingDownBackend(_cp_inventory("phoenix-controlplane-01"))
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    events: list[str] = []
    monkeypatch.setattr(converge, "_wait_reachable", lambda *a, **k: events.append("reachable"))
    monkeypatch.setattr(converge.talosctl, "bootstrap", lambda *a, **k: events.append("bootstrap"))
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig", lambda *a, **k: events.append("kubeconfig")
    )

    assert _stub_converge_full(
        monkeypatch, tmp_path, state, backend,
        {"phoenix-controlplane-01": "config"}, stub_health=True,
    ) == 0

    assert "bootstrap" in events  # the re-run bootstraps the unfinished cluster
    assert writes == []  # bootstrap never re-minted / replaced the identity
    assert secrets_path.read_text() == backup  # pre-bootstrap backup is current


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


# ---- plugin configuration is validated before any mutation -----------------

def test_converge_plugin_validation_aborts_before_any_mutation(monkeypatch, tmp_path):
    """A configured plugin with an invalid `cluster.yaml` / `secrets.yaml`
    section must stop converge in the validate phase -- before the image or
    network phases mutate anything -- instead of surfacing only at the end, once
    the cluster is already built. This is the 'validate plugin configuration
    before core mutations' guarantee."""
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    backend = _RecordingBackend()
    monkeypatch.setattr(converge, "dry_run", lambda: False)

    def bad_validate(ctx):
        raise ConfigError("unsupported option(s): gitlab")

    monkeypatch.setattr(converge.plugins, "validate", bad_validate)

    with pytest.raises(ConfigError, match="unsupported option"):
        _stub_converge(monkeypatch, tmp_path, state, backend)

    assert backend.mutations == []

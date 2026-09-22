"""Destructive-action confirmation and final health-check safety."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.config import Machine
from taloscluster.errors import ConfigError, ReconcileError, StateError
from taloscluster.infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
)

# A guaranteed-absent talosconfig path. The repository root is itself a cluster
# directory and may hold a real talosconfig, so tests exercising the
# no-client-config path must not depend on the working directory.
ABSENT_TALOSCONFIG = Path("/nonexistent/taloscluster/talosconfig")


@pytest.fixture(autouse=True)
def _no_kube_addresses(monkeypatch):
    """Scale-down reads the kube Nodes' InternalIPs as its last address source;
    stub it empty so a test that does not care never shells out to kubectl."""
    monkeypatch.setattr(converge.kubectl, "node_addresses", lambda _kc: {})


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
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit, match="aborted"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            ABSENT_TALOSCONFIG,
            Path("kubeconfig"), assume_yes=False,
        )

    assert mutations == []


def test_scale_down_refuses_even_controlplane_count(monkeypatch):
    """Removing a control plane when the desired count is even would break etcd
    quorum, so scale-down must refuse before prompting or mutating."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 2}, metal_servers={})
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
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_yes_skips_prompt_and_deletes(monkeypatch):
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), ABSENT_TALOSCONFIG,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["drain", "reset", "delete", "compute"]


def test_scale_down_continues_when_drain_fails_on_notready_node(monkeypatch):
    """A previous run may have drained and reset the node but not deleted it.
    Re-running converge must skip the failed drain and still delete the node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), ABSENT_TALOSCONFIG,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["reset", "delete", "compute"]


def test_scale_down_aborts_when_drain_fails_on_ready_node(monkeypatch):
    """A PDB or eviction failure on a live node must abort, not delete the node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_deletes_notready_node_with_no_address(monkeypatch):
    """A node already shut down by a previous reset has no resolvable address
    and is NotReady in k8s. Scale-down must still delete it."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
        ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["delete", "compute"]


def test_scale_down_aborts_when_no_address_but_node_ready(monkeypatch):
    """No address + Ready node means a discovery failure, not an already-reset node."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: True)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="no address for old-worker but node is Ready"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_when_drain_fails_and_status_unknown(monkeypatch):
    """kubectl API failure during drain must not be treated as NotReady."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_when_no_address_and_status_unknown(monkeypatch):
    """No address + unknown node status must not be treated as confirmed down."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: None)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    with pytest.raises(ReconcileError, match="no address for old-worker but node is unknown"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_addressless_control_plane_when_still_a_member(monkeypatch, tmp_path):
    """An addressless NotReady control plane that is still registered as a talos
    etcd member must abort, not delete the VM: NotReady does not prove it left
    etcd, so deleting would bypass the reset-failure protection."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
    # the node is addressless: discovery reports no reachable address for it
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-03": ""},
    )
    # the failed-reset node is still a live etcd member on the surviving control plane
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-03": "8eb052c9"},
    )

    with pytest.raises(ReconcileError, match="still an etcd member"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            talosconfig, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_addressless_cp_absent_from_discovery_but_in_etcd(monkeypatch, tmp_path):
    """A node absent from `get members` discovery data is NOT proof it left etcd:
    discovery is not etcd membership and can drop the node entirely. The node
    must be checked against the surviving control plane's live etcd member list,
    which still lists it -- so scale-down must abort, not delete."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
    # discovery (member_addresses) drops the addressless node, so an
    # addressless-only check would wrongly conclude it left -- but the surviving
    # control plane's authoritative etcd member list still has it.
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-02": "192.0.2.2"},
    )
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {
            "testcluster-controlplane-02": "9eb1f01d",
            "testcluster-controlplane-03": "8eb052c9",
        },
    )

    with pytest.raises(ReconcileError, match="still an etcd member"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            talosconfig, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_aborts_addressless_control_plane_when_etcd_query_fails(monkeypatch):
    """Without an etcd member list we cannot positively establish that an
    addressless control plane left etcd, so relying on NotReady alone must
    abort (fail closed on missing evidence)."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl, "node_names", lambda _kc: ["testcluster-controlplane-03"]
    )
    monkeypatch.setattr(converge.kubectl, "node_ready", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))

    def fail_etcd(*_a, **_k):
        raise ReconcileError(
            "could not read etcd membership from control plane ep"
        )
    monkeypatch.setattr(converge.talosctl, "etcd_members", fail_etcd)

    with pytest.raises(ReconcileError, match="could not read etcd membership"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_deletes_addressless_control_plane_when_removal_established(
    monkeypatch, tmp_path
):
    """An addressless NotReady control plane whose node is confirmed absent from
    the surviving control plane's AUTHORITATIVE etcd member list has positively
    left etcd and can be deleted."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
    # discovery works and reports the surviving control planes -- not the node
    # under removal, which is addressless and must be confirmed via etcd instead
    monkeypatch.setattr(
        converge.talosctl, "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-02": "192.0.2.2"},
    )
    # the surviving control plane's live etcd membership no longer lists the node
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-02": "9eb1f01d"},
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
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
            NetworkResult(), ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    # drained but the VM is NOT deleted once the reset failed
    assert mutations == ["drain"]


def test_scale_down_health_checks_between_successive_control_plane_removals(monkeypatch):
    """Removing one control plane at a time must health-check before the next."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
        NetworkResult(), ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == [
        "drain", "reset", "delete", "compute",
        "drain", "reset", "delete", "compute",
    ]
    assert len(checks) == 1  # only between the two control-plane removals


def test_scale_down_stops_before_second_control_plane_when_unhealthy(monkeypatch):
    """An unhealthy cluster between control-plane removals must abort the rollout."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
            NetworkResult(), ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
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
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
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
            NetworkResult(), ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
        )

    # only the first control plane was removed; the second is untouched because
    # the real helper refused the kube-api fallback for a control plane
    assert mutations == ["drain", "reset", "delete", "compute"]


def test_scale_down_rerun_resumes_vm_delete_after_kube_node_already_gone(monkeypatch):
    """A prior run deleted the kube Node but its provider-VM delete failed; the
    rerun sees no Node in `kubectl.get nodes`, so the undesired owned machine
    must be reconciled from the provider inventory and its VM deleted -- not
    ignored forever."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    # the kube Node is gone on the rerun...
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    # ...and the owned VM is still in the inventory
    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    # no kube Node exists, so no drain and no kubectl node delete should run
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("inventory-only VM must not be drained")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("inventory-only VM has no kube Node to delete"),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), ABSENT_TALOSCONFIG,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["reset", "compute"]


def test_scale_down_removes_owned_machine_that_never_joined_kubernetes(monkeypatch):
    """An owned undesired provider machine that never registered as a kube Node
    (e.g. a worker whose first boot failed) is invisible to `kubectl.get nodes`
    and must still be removed from the provider inventory."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    inventory = InfrastructureInventory(
        machines={
            "stranded-worker": InfrastructureMachine(
                "stranded-worker",
                attachments=(NetworkAttachment("private", "192.0.2.11"),),
            )
        }
    )
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("never-joined VM must not be drained")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("never-joined VM has no kube Node to delete"),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), ABSENT_TALOSCONFIG,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["reset", "compute"]


def test_scale_down_rerun_deletes_cp_with_known_address_whose_reset_fails_and_is_out_of_etcd(
    monkeypatch,
):
    """A control plane whose kube Node is already gone but whose address the
    provider still knows (a wiped/powered-off VM from a prior reset whose delete
    failed) must not abort the scale-down forever on a rerun when its reset
    fails: treat the failure as a request for evidence, fall through to the
    authoritative etcd-member check, and delete the VM once the node is out of
    etcd."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("must not drain a kube-less CP")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("must not delete a kube Node that does not exist"),
    )

    def fail_reset(*_a, **_k):
        raise ReconcileError("graceful reset of control plane testcluster-controlplane-03 failed")
    monkeypatch.setattr(converge.talosctl, "reset", fail_reset)
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    inventory = InfrastructureInventory(
        machines={
            "testcluster-controlplane-03": InfrastructureMachine(
                "testcluster-controlplane-03",
                attachments=(NetworkAttachment("cluster", "192.0.2.30"),),
            )
        }
    )
    # the wiped node is absent from the surviving control plane's etcd member list
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-02": "8c2aa1e0"},
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), ABSENT_TALOSCONFIG,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["compute"]


def test_scale_down_rerun_aborts_cp_with_known_address_whose_reset_fails_but_is_still_in_etcd(
    monkeypatch, tmp_path
):
    """Even with a known address and no kube Node, a control plane whose reset
    fails must not delete the VM while it is still an etcd member: the reset
    failure is treated as a request for evidence, and the authoritative
    etcd-member check keeps the quorum safeguard."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("must not drain a kube-less CP")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("must not delete a kube Node that does not exist"),
    )

    def fail_reset(*_a, **_k):
        raise ReconcileError("graceful reset of control plane testcluster-controlplane-03 failed")
    monkeypatch.setattr(converge.talosctl, "reset", fail_reset)
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    inventory = InfrastructureInventory(
        machines={
            "testcluster-controlplane-03": InfrastructureMachine(
                "testcluster-controlplane-03",
                attachments=(NetworkAttachment("cluster", "192.0.2.30"),),
            )
        }
    )
    # the wiped node is still a live etcd member on the surviving control plane
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-03": "9eb1f01d"},
    )

    with pytest.raises(ReconcileError, match="still an etcd member"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), talosconfig,
            Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_preserves_etcd_safeguard_for_owned_control_plane_inventory_only(
    monkeypatch, tmp_path
):
    """An addressless owned control-plane machine whose kube Node is already gone
    is still a potential etcd member, so its removal must keep the authoritative
    etcd-membership safeguard: abort while the node is still a member rather than
    delete the VM."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("must not drain an addressless CP")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("must not delete a kube Node that does not exist"),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    inventory = InfrastructureInventory(
        machines={
            "testcluster-controlplane-03": InfrastructureMachine("testcluster-controlplane-03")
        }
    )
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-03": "9eb1f01d"},
    )

    with pytest.raises(ReconcileError, match="still an etcd member"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), talosconfig,
            Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_deletes_owned_control_plane_inventory_only_once_out_of_etcd(
    monkeypatch, tmp_path
):
    """Once the owned addressless control plane is affirmatively absent from the
    surviving control plane's etcd member list, its VM may be deleted even though
    no kube Node is left."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("must not drain a kube-less CP")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node",
        lambda *_a: pytest.fail("must not delete a kube Node that does not exist"),
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: mutations.append("reset"))
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    inventory = InfrastructureInventory(
        machines={
            "testcluster-controlplane-03": InfrastructureMachine("testcluster-controlplane-03")
        }
    )
    monkeypatch.setattr(
        converge.talosctl, "etcd_members",
        lambda *_a, **_k: {"testcluster-controlplane-02": "8c2aa1e0"},
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), talosconfig,
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["compute"]


def test_scale_down_never_removes_a_joined_metal_node(monkeypatch):
    """A metal server `metal join` installed is a live Kubernetes Node no VM
    provider manages, and its name never carries the VM controlplane pattern.
    Both a metal worker and a metal control plane are desired nodes, so
    scale-down must not drain, reset, delete or prompt for either."""
    cfg = SimpleNamespace(
        name="testcluster",
        controlplane={"count": 3},
        metal_servers={"rp001-worker": "worker", "rp001-cp": "controlplane"},
    )
    machines = {
        f"testcluster-controlplane-{i:02d}": Machine(
            name=f"testcluster-controlplane-{i:02d}",
            role="controlplane",
            pool="controlplane",
            disk=40,
            extensions=(),
            config_patches=(),
        )
        for i in range(1, 4)
    }
    mutations: list[str] = []
    monkeypatch.setattr(
        converge.kubectl,
        "node_names",
        lambda _kc: [
            "testcluster-controlplane-01",
            "testcluster-controlplane-02",
            "testcluster-controlplane-03",
            "rp001-worker",
            "rp001-cp",
        ],
    )
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda *_a: pytest.fail("metal node must not be drained")
    )
    monkeypatch.setattr(
        converge.talosctl, "reset", lambda *_a, **_k: pytest.fail("metal node must not be reset")
    )
    monkeypatch.setattr(
        converge.kubectl,
        "delete_node",
        lambda *_a: pytest.fail("metal node must not be deleted"),
    )
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, machines, InfrastructureInventory(), NetworkResult(),
        ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == []


def test_scale_down_resets_a_dropped_metal_node_using_its_kube_node_address(monkeypatch):
    """A metal machine dropped from the config belongs to no provider inventory,
    so commenting out its config takes its static address with it, and a
    NotReady node is absent from Talos discovery too. The kube Node still
    carries the InternalIP kubelet registered, so the node is reset -- not
    merely deleted from Kubernetes, which would leave a live machine whose
    kubelet re-registers it seconds later."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["rp002"])
    monkeypatch.setattr(converge.kubectl, "node_addresses", lambda _kc: {"rp002": "172.29.21.6"})
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(
        converge.talosctl,
        "reset",
        lambda _tc, _ep, node, **kw: mutations.append(
            f"reset {node} maintenance={kw['to_maintenance']}"
        ),
    )
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr(
        converge.kubectl,
        "node_ready",
        lambda *_a: pytest.fail("a resolvable address must not fall through to the ready check"),
    )

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
        ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["drain", "reset 172.29.21.6 maintenance=True", "delete", "compute"]


def test_scale_down_prefers_discovery_over_the_kube_node_address(monkeypatch):
    """A Node object can outlive the machine that registered it, so its
    InternalIP is the last fallback -- live Talos discovery wins."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["rp002"])
    monkeypatch.setattr(converge.kubectl, "node_addresses", lambda _kc: {"rp002": "172.29.21.99"})
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_k: {"rp002": "172.29.21.6"}
    )
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: None)
    monkeypatch.setattr(
        converge.talosctl,
        "reset",
        lambda _tc, _ep, node, **_k: mutations.append(f"reset {node}"),
    )
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: None)
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_k: "10.0.0.1")

    converge._scale_down(
        FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
        Path(__file__), Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["reset 172.29.21.6", "compute"]


def test_scale_down_wipes_a_vm_completely_and_leaves_metal_reusable(monkeypatch):
    """A VM is deleted seconds after its reset, so it is wiped whole and shut
    down. Hardware is deleted by nothing, so only STATE and EPHEMERAL go and it
    reboots into maintenance mode -- reusable, not blank. The provider
    inventory is what tells the two apart, since a removal is by definition
    absent from the config."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    inv = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "172.29.21.50"),),
            )
        }
    )
    resets: list[tuple[str, bool]] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker", "rp002"])
    monkeypatch.setattr(converge.kubectl, "node_addresses", lambda _kc: {"rp002": "172.29.21.6"})
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: None)
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: None)
    monkeypatch.setattr(
        converge.talosctl,
        "reset",
        lambda _tc, _ep, node, **kw: resets.append((node, kw["to_maintenance"])),
    )

    converge._scale_down(
        FakeBackend([]), cfg, {}, inv, NetworkResult(),
        ABSENT_TALOSCONFIG, Path("kubeconfig"), assume_yes=True,
    )

    assert resets == [("172.29.21.50", False), ("172.29.21.6", True)]


def test_scale_down_treats_a_removed_metal_control_plane_as_a_control_plane(
    monkeypatch, tmp_path
):
    """A metal control plane whose server entry was removed from the config is a
    live etcd member no name pattern identifies. The surviving control plane's
    etcd membership must classify it: reset with the control-plane safeguards
    and health-checked like any control-plane removal, never reset as a
    worker."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3}, metal_servers={})
    machines = {
        f"testcluster-controlplane-{i:02d}": Machine(
            name=f"testcluster-controlplane-{i:02d}",
            role="controlplane",
            pool="controlplane",
            disk=40,
            extensions=(),
            config_patches=(),
        )
        for i in range(1, 4)
    }
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    calls: list[str] = []
    monkeypatch.setattr(
        converge.kubectl,
        "node_names",
        lambda _kc: [
            "testcluster-controlplane-01",
            "testcluster-controlplane-02",
            "testcluster-controlplane-03",
            "testcluster-controlplane-04",
            "rp001-cp",
        ],
    )
    monkeypatch.setattr(
        converge.talosctl,
        "member_addresses",
        lambda *_a, **_k: {
            "testcluster-controlplane-04": "192.0.2.40",
            "rp001-cp": "192.0.2.50",
        },
    )
    # every removal is still a live etcd member of the surviving control planes
    monkeypatch.setattr(
        converge.talosctl,
        "etcd_members",
        lambda *_a, **_k: {
            "testcluster-controlplane-01": "9eb1f01d",
            "testcluster-controlplane-02": "8c2aa1e0",
            "testcluster-controlplane-03": "7d3b99c4",
            "testcluster-controlplane-04": "6c4e8d33",
            "rp001-cp": "5af07e12",
        },
    )

    def reset(_tc, _ep, target, *, control_plane=False, to_maintenance=False):
        calls.append(f"reset:{target}:cp={control_plane}")

    monkeypatch.setattr(converge.talosctl, "reset", reset)
    monkeypatch.setattr(
        converge.kubectl, "drain", lambda _kc, node: calls.append(f"drain:{node}")
    )
    monkeypatch.setattr(
        converge.kubectl, "delete_node", lambda _kc, node: calls.append(f"delete:{node}")
    )
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback", lambda *_a, **_kw: calls.append("health") or True
    )

    converge._scale_down(
        FakeBackend(calls), cfg, machines, InfrastructureInventory(), NetworkResult(),
        talosconfig, Path("kubeconfig"), assume_yes=True,
    )

    assert calls == [
        "drain:testcluster-controlplane-04",
        "reset:192.0.2.40:cp=True",
        "delete:testcluster-controlplane-04",
        "compute",
        "health",
        "drain:rp001-cp",
        "reset:192.0.2.50:cp=True",
        "delete:rp001-cp",
        "compute",
    ]


def test_scale_down_refuses_to_remove_a_metal_control_plane_that_would_break_quorum(
    monkeypatch, tmp_path
):
    """The quorum guard keys on role, not the VM name pattern: removing a metal
    control plane whose server entry left the config is refused when the desired
    controlplane count would be even, before anything is mutated."""
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 2}, metal_servers={})
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["rp001-cp"])
    monkeypatch.setattr(
        converge.talosctl, "etcd_members", lambda *_a, **_k: {"rp001-cp": "5af07e12"}
    )
    monkeypatch.setattr(
        converge.kubectl,
        "drain",
        lambda *_a: pytest.fail("the quorum guard must fire before any mutation"),
    )

    with pytest.raises(ReconcileError, match="refusing to remove controlplane rp001-cp"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            talosconfig, Path("kubeconfig"), assume_yes=True,
        )

    assert mutations == []


def test_scale_down_counts_a_configured_metal_control_plane_toward_quorum(
    monkeypatch, tmp_path
):
    """A metal control plane the config still lists belongs in the desired etcd
    total: with two VM control planes plus a metal one, removing a VM control
    plane lands on an odd member count and must be allowed -- the metal member
    flips the parity a VM-only view would refuse."""
    cfg = SimpleNamespace(
        name="testcluster",
        controlplane={"count": 2},
        metal_servers={"rp001-cp": "controlplane"},
    )
    machines = {
        f"testcluster-controlplane-{i:02d}": Machine(
            name=f"testcluster-controlplane-{i:02d}",
            role="controlplane",
            pool="controlplane",
            disk=40,
            extensions=(),
            config_patches=(),
        )
        for i in range(1, 3)
    }
    calls: list[str] = []
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("contexts: {}")
    monkeypatch.setattr(
        converge.kubectl,
        "node_names",
        lambda _kc: [
            "testcluster-controlplane-01",
            "testcluster-controlplane-02",
            "testcluster-controlplane-03",
        ],
    )
    monkeypatch.setattr(
        converge.talosctl,
        "member_addresses",
        lambda *_a, **_k: {"testcluster-controlplane-03": "192.0.2.30"},
    )
    monkeypatch.setattr(
        converge.talosctl,
        "etcd_members",
        lambda *_a, **_k: {
            "testcluster-controlplane-01": "9eb1f01d",
            "testcluster-controlplane-02": "8c2aa1e0",
            "testcluster-controlplane-03": "7d3b99c4",
        },
    )
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a, **_k: calls.append("reset"))
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: calls.append("drain"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: calls.append("delete"))

    converge._scale_down(
        FakeBackend(calls), cfg, machines, InfrastructureInventory(), NetworkResult(),
        talosconfig, Path("kubeconfig"), assume_yes=True,
    )

    assert calls == ["drain", "reset", "delete", "compute"]


# ---- converge reconfigures joined metal machines like the VM pools ----------

# A `metal:` group on the cluster L2 (no network block -> network.cluster), so
# no KubeSpan requirement, with the cabling plan the static address comes from.
METAL_GROUP = {
    "role": "worker",
    "redfish": False,
    "disk": "/dev/sda",
    "servers": {},
}


def _metal_cfg(make_config, servers: dict):
    return make_config(
        {"metal": {"site": {**METAL_GROUP, "servers": servers}}}
    )


def test_apply_configs_reconfigures_a_joined_metal_node(monkeypatch, make_config):
    """A joined metal machine gets the same config push as the VM pools, at the
    static address of its cluster link; one with no kube Node has never joined
    and is skipped, and an unreachable VM address still blocks nothing."""
    cfg = _metal_cfg(
        make_config,
        {
            "rp001": {"interfaces": {"enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}}},
            "rp002": {"interfaces": {"enp1s0f0": {"role": "cluster", "ip": "192.168.0.6/21"}}},
        },
    )
    machines = {"testcluster-controlplane-01": SimpleNamespace(role="controlplane")}
    inv = _cp_inventory("testcluster-controlplane-01")
    configs = {
        "testcluster-controlplane-01": "config:vm",
        "rp001": "config:metal",
        "rp002": "config:metal",
    }
    _no_op_reachable(monkeypatch)
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_k: "ep")
    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config",
        lambda _tc, _e, node, _c: applied.append((node, _c)) or False,
    )
    # rp001 has joined (a kube Node exists); rp002 never did
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, n: n != "rp002")

    converge._apply_configs(
        cfg, machines, inv, NetworkResult(), configs,
        Path("talosconfig"), Path("kubeconfig"),
    )

    # the VM control plane through its discovered address, the metal worker at
    # its static cluster address; rp002 never joined and is skipped
    assert applied == [("192.0.2.1", "config:vm"), ("192.168.0.5", "config:metal")]


def test_apply_configs_settles_a_joined_metal_control_plane(monkeypatch, make_config):
    """A metal control plane's reboot-requiring apply is settled at its static
    address -- waited down, back up and health-checked -- before the rollout
    advances, under the same quorum safeguards as a VM control plane's."""
    cfg = _metal_cfg(
        make_config,
        {
            "rp001": {
                "role": "controlplane",
                "interfaces": {"enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}},
            },
        },
    )
    events: list[tuple] = []
    _no_op_reachable(monkeypatch)
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_k: "ep")
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config",
        lambda _tc, _e, node, _c: events.append(("apply", node, _c)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_down",
        lambda _tc, _e, node: events.append(("down", node)) or True,
    )
    monkeypatch.setattr(
        converge, "_wait_reachable", lambda _tc, _e, node: events.append(("up", node))
    )
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *_a, **_k: events.append(("health", None)) or True,
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)

    converge._apply_configs(
        cfg, {}, InfrastructureInventory(), NetworkResult(), {"rp001": "config:metal"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    assert events == [
        ("apply", "192.168.0.5", "config:metal"),
        ("down", "192.168.0.5"),
        ("up", "192.168.0.5"),
        ("health", None),
    ]


def test_destroy_warns_that_joined_metal_machines_keep_running(
    monkeypatch, tmp_path, capsys
):
    """Destroy deletes only the provider's resources: a joined metal machine
    keeps running the destroyed cluster under the identity this removes, so the
    summary must name every metal server and the reset its hardware needs."""
    cfg = SimpleNamespace(
        name="testcluster", metal_servers={"rp001": "worker", "rp002": "controlplane"}
    )
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_kw: 0)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    assert converge.destroy(tmp_path, assume_yes=True) == 0

    err = capsys.readouterr().err
    assert "rp001" in err and "rp002" in err
    assert "talosctl --talosconfig talosconfig -n <node> reset" in err
    assert backend.mutations == ["destroy"]


def test_destroy_without_metal_machines_makes_no_metal_claim(
    monkeypatch, tmp_path, capsys
):
    """A VM-only cluster's destroy says nothing about bare metal."""
    cfg = SimpleNamespace(name="testcluster", metal_servers={})
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "backend_for", lambda *_a: FakeBackend())
    monkeypatch.setattr(converge, "_run_plugins", lambda *_a, **_kw: 0)

    converge.destroy(tmp_path, assume_yes=True)

    assert "bare-metal" not in capsys.readouterr().err


def test_destroy_decline_happens_before_plugin_teardown(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="testcluster")
    plugin_calls: list[str] = []
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
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


def test_recorded_endpoint_treats_a_non_mapping_kubeconfig_as_unknown(tmp_path):
    path = tmp_path / "kubeconfig"
    path.write_text("truncated")  # a hand-edited file that parses to a scalar
    assert converge._recorded_endpoint(path, "phoenix") == ""
    path.write_text("- just\n- a\n- list\n")
    assert converge._recorded_endpoint(path, "phoenix") == ""


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


def test_talosconfig_endpoint_treats_a_non_mapping_file_as_unknown(tmp_path):
    path = tmp_path / "talosconfig"
    path.write_text("truncated")  # a hand-edited file that parses to a scalar
    assert converge._talosconfig_endpoint(path, "phoenix") == ""
    path.write_text("- just\n- a\n- list\n")
    assert converge._talosconfig_endpoint(path, "phoenix") == ""


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


def test_talos_endpoint_with_a_keyed_tailscale_section_is_the_magicdns_name(make_config):
    cfg = make_config({"tailscale": {"auth_key": "tskey-auth-abc123"}})
    assert cfg.tailscale_enabled and cfg.tailscale_active
    assert converge._talos_endpoint(cfg) == "testcluster-controlplane-01"


def test_talos_endpoint_with_a_keyless_tailscale_section_uses_the_real_address(
    make_config, tmp_path
):
    """A `tailscale:` section without an auth_key leaves the extension idle:
    the node never registers, so the MagicDNS name would never resolve and
    talosctl must take the real-address path instead of hanging on it."""
    cfg = make_config({"tailscale": {"login_server": "https://headscale.example.edu"}})
    assert cfg.tailscale_enabled and not cfg.tailscale_active
    host = "testcluster-controlplane-01"
    refs = NetworkResult(
        machine_attachments={host: (NetworkAttachment(name="cluster", address="192.168.100.11"),)}
    )
    assert converge._talos_endpoint(cfg, refs) == "192.168.100.11"
    assert (
        converge._talos_endpoint(cfg, talosconfig=tmp_path / "none", required=False) == host
    )
    with pytest.raises(ReconcileError, match=f"no address known for {host}"):
        converge._talos_endpoint(cfg, NetworkResult(), InfrastructureInventory())


def test_talos_endpoint_duck_typed_section_without_active_attr_still_registers():
    # a duck-typed config (test fixture, older plugin) without a
    # tailscale_active attribute keeps the section-presence behaviour
    cfg = SimpleNamespace(name="phoenix", tailscale_enabled=True, tailscale_auth_key=None)
    assert converge._talos_endpoint(cfg) == "phoenix-controlplane-01"


# ---- validate phase: a live tailscale toggle is refused ---------------------

def _recorded_talosconfig(tmp_path, endpoint):
    """A talosconfig as a previous converge wrote it: the context endpoint is
    cp-01's MagicDNS name on a registered cluster, its real address without."""
    path = tmp_path / "talosconfig"
    path.write_text(
        "context: testcluster\ncontexts:\n  testcluster:\n    endpoints:\n"
        f"    - {endpoint}\n"
    )
    return path


def test_removing_tailscale_from_a_registered_cluster_is_refused(
    monkeypatch, make_config, tmp_path
):
    """The cluster registered (discovery reports tailnet addresses), so
    dropping the section reinstalls every node onto a schematic without the
    extension -- and the rollout then polls the tailscale address the upgraded
    node just lost. Refused in validate, before anything mutates."""
    cfg = make_config()
    assert "siderolabs/tailscale" not in cfg.machines["testcluster-controlplane-01"].extensions
    talosconfig = _recorded_talosconfig(tmp_path, "testcluster-controlplane-01")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: ["schematic", "siderolabs/tailscale"],
    )
    monkeypatch.setattr(
        converge.talosctl,
        "tailnet_member_addresses",
        lambda *_a: {"testcluster-controlplane-01": "100.64.0.68"},
    )
    with pytest.raises(ReconcileError, match="toggling tailscale on a live cluster"):
        converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_removing_tailscale_after_a_live_key_removal_is_refused(
    monkeypatch, make_config, tmp_path
):
    """A keyed section whose auth_key was removed on a live cluster: converge
    rewrote the recorded endpoint to cp-01's real address, but the nodes kept
    their tailnet registration -- discovery still reports it, so the removal
    would deadlock the rollout exactly like a registered cluster. Refused even
    though the section being removed is keyless and the endpoint is a real
    address."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: ["schematic", "siderolabs/tailscale"],
    )
    monkeypatch.setattr(
        converge.talosctl,
        "tailnet_member_addresses",
        lambda *_a: {"testcluster-controlplane-01": "100.64.0.68"},
    )
    with pytest.raises(ReconcileError, match="toggling tailscale on a live cluster"):
        converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_adding_a_keyed_tailscale_section_to_a_live_cluster_is_refused(
    monkeypatch, make_config, tmp_path
):
    """With a key the endpoint becomes cp-01's MagicDNS name, which does not
    resolve until the reinstalled nodes have joined the tailnet -- discovery
    comes back empty and the config push fails. Refused in validate."""
    cfg = make_config({"tailscale": {"auth_key": "tskey-auth-abc123"}})
    assert cfg.tailscale_active
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(converge.talosctl, "running_extensions", lambda *_a: ["schematic"])
    with pytest.raises(ReconcileError, match="toggling tailscale on a live cluster"):
        converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_adding_a_key_to_a_live_keyless_section_is_refused(monkeypatch, make_config, tmp_path):
    """A keyless section already installed the extension, so adding the
    auth_key changes no schematic and the extension comparison would wave the
    run through -- but management would move to cp-01's MagicDNS name, which
    does not resolve because cp-01 never registered. Refused from the recorded
    endpoint's shape alone, without probing."""
    cfg = make_config({"tailscale": {"auth_key": "tskey-auth-abc123"}})
    assert cfg.tailscale_active
    assert "siderolabs/tailscale" in cfg.machines["testcluster-controlplane-01"].extensions
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: pytest.fail("the shape check must refuse without probing"),
    )
    with pytest.raises(ReconcileError, match="toggling tailscale on a live cluster"):
        converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_removing_the_key_from_a_live_keyed_section_is_refused(monkeypatch, make_config, tmp_path):
    """Dropping the auth_key keeps the extension installed, so no schematic
    changes and the extension comparison would wave the run through -- but
    management would move off cp-01's MagicDNS name while the nodes keep their
    tailnet registration, and discovery keeps handing the rollout addresses the
    nodes are about to lose. Refused from the recorded endpoint's shape alone,
    without probing."""
    cfg = make_config({"tailscale": {}})
    assert cfg.tailscale_active is False
    assert "siderolabs/tailscale" in cfg.machines["testcluster-controlplane-01"].extensions
    talosconfig = _recorded_talosconfig(tmp_path, "testcluster-controlplane-01")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: pytest.fail("the shape check must refuse without probing"),
    )
    with pytest.raises(ReconcileError, match="toggling tailscale on a live cluster"):
        converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_removing_a_keyless_tailscale_section_is_allowed(monkeypatch, make_config, tmp_path):
    """A keyless section never registered the nodes: discovery reports no
    tailnet address, so dropping the extension reinstalls the node in place
    and the rollout survives on real addresses."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: ["schematic", "siderolabs/tailscale"],
    )
    monkeypatch.setattr(
        converge.talosctl,
        "tailnet_member_addresses",
        lambda *_a: {},
    )
    converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_adding_a_keyless_tailscale_section_is_allowed(monkeypatch, make_config, tmp_path):
    """Adding the extension without a key only changes the installer image: it
    idles and management stays on real addresses."""
    cfg = make_config({"tailscale": {}})
    assert cfg.tailscale_active is False
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(converge.talosctl, "running_extensions", lambda *_a: ["schematic"])
    converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_the_toggle_check_passes_a_matching_schematic(monkeypatch, make_config, tmp_path):
    cfg = make_config({"tailscale": {"auth_key": "tskey-auth-abc123"}})
    talosconfig = _recorded_talosconfig(tmp_path, "testcluster-controlplane-01")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: ["schematic", "siderolabs/tailscale"],
    )
    converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_the_toggle_check_skips_an_unreachable_node(monkeypatch, make_config, tmp_path):
    """An unreachable cluster fails on its own later; the check must not guess
    from a node it cannot read. The recorded endpoint and the configuration
    agree on the real-address path, so only the probe could decide."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")

    def unreachable(*_a):
        raise subprocess.CalledProcessError(1, "talosctl")

    monkeypatch.setattr(converge.talosctl, "running_extensions", unreachable)
    converge._validate_tailscale_toggle(cfg, cfg.machines, talosconfig)


def test_the_toggle_check_skips_without_a_recorded_endpoint(monkeypatch, make_config, tmp_path):
    """No talosconfig from a previous run -- a first converge -- has no running
    state to compare against."""
    cfg = make_config()
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: pytest.fail("must not probe without a recorded endpoint"),
    )
    converge._validate_tailscale_toggle(cfg, cfg.machines, tmp_path / "none")


def test_the_toggle_check_skips_a_duck_typed_machine(monkeypatch, make_config, tmp_path):
    """Test fixtures and older plugins hand in machines without the resolved
    extension set; without it there is nothing to compare against."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "testcluster-controlplane-01")
    monkeypatch.setattr(
        converge.talosctl,
        "running_extensions",
        lambda *_a: pytest.fail("must not probe a machine with no extension set"),
    )
    converge._validate_tailscale_toggle(
        cfg, {"testcluster-controlplane-01": SimpleNamespace(role="controlplane")}, talosconfig
    )


# ---- validate phase: a talos downgrade is refused ---------------------------

def test_an_older_talos_pin_is_refused(monkeypatch, make_config, tmp_path):
    """A pin older than what the cluster runs -- a typo, a reverted commit --
    would make `_reconcile_talos` roll a downgrade across the control planes.
    Refused in validate, before anything mutates, like the kubernetes one."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    seen = []

    def version(_tc, endpoint, node):
        seen.append((endpoint, node))
        return "v1.14.2"

    monkeypatch.setattr(converge.talosctl, "server_version", version)
    with pytest.raises(
        ReconcileError,
        match="talos.version v1.13.9 is older than the running v1.14.2; "
        "talos downgrades are not supported",
    ):
        converge._validate_talos_downgrade(cfg, talosconfig)
    # asked at the recorded endpoint, which is also the dial target
    assert seen == [("192.0.2.10", "192.0.2.10")]


def test_a_matching_or_older_running_talos_passes(monkeypatch, make_config, tmp_path):
    """The pin equal to the running version is the settled state; a pin newer
    than what runs is the ordinary upgrade path. Neither is a downgrade."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    converge._validate_talos_downgrade(cfg, talosconfig)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.5")
    converge._validate_talos_downgrade(cfg, talosconfig)


def test_the_downgrade_check_skips_a_node_that_answers_nothing(
    monkeypatch, make_config, tmp_path
):
    """An unreadable version must not read as a downgrade; an unreachable
    cluster fails on its own later."""
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "")
    converge._validate_talos_downgrade(cfg, talosconfig)


def test_the_downgrade_check_skips_an_unreachable_cluster(
    monkeypatch, make_config, tmp_path
):
    cfg = make_config()
    talosconfig = _recorded_talosconfig(tmp_path, "192.0.2.10")

    def unreachable(*_a):
        raise subprocess.CalledProcessError(1, "talosctl")

    monkeypatch.setattr(converge.talosctl, "server_version", unreachable)
    converge._validate_talos_downgrade(cfg, talosconfig)


def test_the_downgrade_check_skips_without_a_recorded_endpoint(
    monkeypatch, make_config, tmp_path
):
    """No talosconfig from a previous run -- a first converge -- has no running
    version to compare against."""
    cfg = make_config()
    monkeypatch.setattr(
        converge.talosctl,
        "server_version",
        lambda *_a: pytest.fail("must not probe without a recorded endpoint"),
    )
    converge._validate_talos_downgrade(cfg, tmp_path / "none")


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
                        *, stub_health=False, cfg=None):
    """Wire converge() to run through the compute phase against fakes.

    `stub_health` replaces the health phase with no-ops -- used only for a path
    where the cluster is EXPECTED to come up (e.g. an interrupted first run that
    bootstraps); callers that want to observe that the health phase is skipped
    on an unreachable cluster leave it False and patch the phase themselves.
    `cfg` defaults to a duck-typed enabled-tailscale config; pass a real Config
    (e.g. from make_config) to exercise loader-backed behaviour such as a
    keyless tailscale section.
    """
    if cfg is None:
        cfg = SimpleNamespace(
            name="phoenix", talos_version="v1.13.0", kubernetes_version="v1.31.0",
            extension_sets=lambda: [()],
            machines={"phoenix-controlplane-01": SimpleNamespace(role="controlplane")},
            tailscale_enabled=True,
            tailscale_auth_key=None,
        )
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg: backend)
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


def test_converge_bootstraps_a_keyless_tailscale_cluster_on_the_real_address(
    monkeypatch, tmp_path, make_config
):
    """A `tailscale:` section without an auth_key leaves the extension idle:
    the node never registers, so its MagicDNS name never resolves and waiting
    on it would hang bootstrap for 15 minutes. Converge must resolve cp-01's
    real address and dial that instead."""
    cfg = make_config(
        {
            "name": "phoenix",
            "controlplane": {"count": 1},
            "tailscale": {"login_server": "https://headscale.example.edu"},
        },
    )
    assert cfg.tailscale_enabled and not cfg.tailscale_active
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    backend = _ExistingDownBackend(_cp_inventory("phoenix-controlplane-01"))
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    waits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        converge, "_wait_reachable",
        lambda _t, endpoint, node, **_k: waits.append((endpoint, node)),
    )
    events: list[str] = []
    monkeypatch.setattr(converge.talosctl, "bootstrap", lambda *a, **k: events.append("bootstrap"))
    monkeypatch.setattr(
        converge.talosctl, "kubeconfig", lambda *a, **k: events.append("kubeconfig")
    )

    assert _stub_converge_full(
        monkeypatch, tmp_path, state, backend,
        {"phoenix-controlplane-01": "config"}, stub_health=True, cfg=cfg,
    ) == 0

    assert "bootstrap" in events and "kubeconfig" in events
    # every wait dialed the real inventory address, never the MagicDNS name
    assert waits
    assert set(waits) == {("192.0.2.1", "192.0.2.1")}


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
        tailscale_auth_key=None,
    )
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg: backend)
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


# ---- converge wiring: the metal section's machines ride the same phases -----

class _MetalConfigBackend(_ExistingDownBackend):
    """Records the machine configs the compute phase is handed."""

    def reconcile_machines(self, _machines, _inventory, _boot_image, _configs):
        self.seen_configs = dict(_configs)
        return super().reconcile_machines(_machines, _inventory, _boot_image, _configs)


def test_converge_reconfigures_and_upgrades_metal_machines_with_the_vms(
    monkeypatch, tmp_path
):
    """The metal section's machines ride the same converge phases as the VM
    pools: their machine config is generated beside the VMs' from the metal
    installer and the running kubernetes version, and the upgrade and post-join
    phases carry the metal installer ref and schematic -- so a talos.version,
    extension or patch edit reaches metal nodes too instead of stopping at the
    VM pools and leaving them in permanent drift."""
    inventory = _cp_inventory("phoenix-controlplane-01")
    state = _FakeState(True, tmp_path / "talossecrets.yaml")
    backend = _MetalConfigBackend(inventory)
    (tmp_path / "kubeconfig").write_text("clusters: []\n")
    metal = SimpleNamespace(
        groups={"site": SimpleNamespace(
            servers={"rp001": SimpleNamespace(
                name="rp001", role="worker", redfish=False, disk="/dev/sda"
            )}
        )}
    )
    cfg = SimpleNamespace(
        name="phoenix", talos_version="v1.13.0", kubernetes_version="v1.31.0",
        extension_sets=lambda: [()],
        machines={"phoenix-controlplane-01": SimpleNamespace(role="controlplane")},
        tailscale_enabled=True,
        tailscale_auth_key=None,
        metal_servers={"rp001": "worker"},
        metal=metal,
    )
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "preflight_tools", lambda: None)
    monkeypatch.setattr(converge, "validate_warnings", lambda _cfg: [])
    monkeypatch.setattr(converge, "backend_for", lambda _cfg: backend)
    monkeypatch.setattr(converge, "State", lambda _root: state)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge.talosctl, "gen_talosconfig", lambda *a, **k: "talosconfig")
    monkeypatch.setattr(
        converge.machineconfig, "build_configs",
        lambda *a, **k: {"phoenix-controlplane-01": "vm-config"},
    )
    monkeypatch.setattr(
        converge.metal_talos, "installer",
        lambda _cfg: ("m-sch", "factory.talos.dev/metal-installer/m-sch:v1.13.0"),
    )
    built: list[dict] = []

    def fake_build(server, _cfg, _secrets, installer, endpoint, kubernetes_version=None):
        built.append({
            "server": server.name,
            "installer": installer,
            "endpoint": endpoint,
            "kubernetes_version": kubernetes_version,
        })
        return f"metal-config:{server.name}"

    monkeypatch.setattr(converge.metal_talos, "build_config", fake_build)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.31.0")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: False)
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: [])
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_k: {})
    monkeypatch.setattr(converge, "_require_final_health", lambda *a, **k: None)
    monkeypatch.setattr(converge, "_wait_nodes_ready", lambda *a, **k: None)
    monkeypatch.setattr(converge.kubectl, "get_nodes_wide", lambda _kc: "")
    joined: list[dict] = []

    def fake_joined(*_args, metal_installer="", metal_schematic="", **_kw):
        joined.append({"installer": metal_installer, "schematic": metal_schematic})
        return inventory

    monkeypatch.setattr(converge, "_reconcile_joined", fake_joined)
    monkeypatch.setattr(converge, "_run_plugins", lambda *a, **kw: 0)
    # rp001 has no kube Node, so the compute phase joins it: it waits in
    # maintenance mode, which needs no BMC and is what `redfish: false` expects
    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: True)
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config_insecure",
        lambda ip, config: applied.append((ip, config)),
    )

    assert converge.converge(tmp_path) == 0

    # the compute phase joined it with the config this run generated
    assert applied == [("192.0.2.61", "metal-config:rp001")]

    # the metal config was generated beside the VMs', from the metal installer
    # at the cluster's running kubernetes version and against the endpoint the
    # network phase resolved (the same one the VM configs carry), and reached
    # the compute phase
    assert built == [{
        "server": "rp001",
        "installer": "factory.talos.dev/metal-installer/m-sch:v1.13.0",
        "endpoint": SimpleNamespace(advertised_address="192.0.2.5", vip="192.0.2.5"),
        "kubernetes_version": "v1.31.0",
    }]
    assert backend.seen_configs == {
        "phoenix-controlplane-01": "vm-config",
        "rp001": "metal-config:rp001",
    }
    # the post-join reconcile carries the metal installer ref and schematic
    assert joined == [{
        "installer": "factory.talos.dev/metal-installer/m-sch:v1.13.0",
        "schematic": "m-sch",
    }]


def _pending_metal_cfg(**server_kw):
    """A cfg whose one metal server is configured but not in the cluster."""
    fields = {
        "name": "rp001", "role": "worker", "disk": "/dev/sda", "redfish": False,
        "boot_timeout": 600, "bmc": SimpleNamespace(ip="172.28.50.5"),
    }
    server = SimpleNamespace(**{**fields, **server_kw})
    return SimpleNamespace(
        name="testcluster",
        talos_version="v1.13.10",
        metal=SimpleNamespace(groups={"phoenix": SimpleNamespace(servers={"rp001": server})}),
        metal_servers={"rp001": "worker"},
    )


def test_validate_refuses_a_metal_machine_converge_can_neither_reach_nor_boot(
    monkeypatch, tmp_path
):
    """A machine the config lists, that is not in the cluster, does not answer
    the maintenance apid and has no BMC to power on, is a change converge can
    never reconcile -- refused in validate while the cluster is untouched,
    like an unsupported provider change."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: False)
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)

    with pytest.raises(ReconcileError, match="not joinable: rp001"):
        converge._validate_metal_joinable(_pending_metal_cfg(), kubeconfig)


def test_validate_allows_a_redfish_machine_that_is_not_in_maintenance(monkeypatch, tmp_path):
    """Converge can power this one on itself, so the compute phase handles it."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: False)
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)

    converge._validate_metal_joinable(_pending_metal_cfg(redfish=True), kubeconfig)


def test_validate_skips_the_joinable_check_without_a_kubeconfig(monkeypatch):
    """A missing kubeconfig means the kube phase has not yet told a fresh
    cluster from a live one whose kubeconfig was lost, so a joined machine
    cannot be told from a pending one and must not abort the run as
    unjoinable -- the kube phase recovers or bootstraps first."""
    monkeypatch.setattr(
        converge.talosctl,
        "maintenance_reachable",
        lambda _ip: pytest.fail("nothing to decide before the kubeconfig is settled"),
    )

    converge._validate_metal_joinable(_pending_metal_cfg(), Path("/nonexistent/kubeconfig"))


def test_validate_ignores_a_failed_node_query(monkeypatch, tmp_path):
    """A node query the api cannot answer (5xx, connection refused, an expired
    kubeconfig) leaves the machine's presence unknown: it must not read as
    unjoined and be refused as unjoinable."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: None)
    monkeypatch.setattr(
        converge.talosctl,
        "maintenance_reachable",
        lambda _ip: pytest.fail("an unknown presence must not be probed"),
    )

    converge._validate_metal_joinable(_pending_metal_cfg(), kubeconfig)


def test_validate_allows_a_joined_metal_machine(monkeypatch, tmp_path):
    """A machine already in the cluster is not pending, so its maintenance apid
    is never probed -- it answers the configured api, not the insecure one."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(
        converge.talosctl,
        "maintenance_reachable",
        lambda _ip: pytest.fail("a joined machine must not be probed"),
    )

    converge._validate_metal_joinable(_pending_metal_cfg(), kubeconfig)


def test_join_metal_applies_the_config_to_a_machine_in_maintenance(monkeypatch):
    """The cheap path: the machine is already waiting, so no BMC is touched and
    it joins with the config this converge run generated."""
    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: True)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _e: "sch")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(
        converge.talosctl, "apply_config_insecure",
        lambda ip, config: applied.append((ip, config)),
    )
    monkeypatch.setattr(
        converge.metal_redfish,
        "Redfish",
        lambda _bmc: pytest.fail("a machine in maintenance mode needs no BMC"),
    )

    converge._join_metal(
        _pending_metal_cfg(), {"rp001": "rp001-config"},
        ABSENT_TALOSCONFIG, Path("/nonexistent/kubeconfig"),
    )

    assert applied == [("192.0.2.61", "rp001-config")]


def test_join_metal_boots_a_redfish_machine_then_applies(monkeypatch):
    """Not in maintenance but drivable: mount media, one-time boot, power on,
    wait for the maintenance apid, then apply -- `metal join`'s order."""
    calls: list[str] = []
    reachable = iter([False, False, True])
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(
        converge.talosctl, "maintenance_reachable", lambda _ip: next(reachable, True)
    )
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _e: "sch")
    monkeypatch.setattr(converge.factory, "nocloud_iso_url", lambda _s, _v: "http://iso")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    class FakeRedfish:
        def __init__(self, _bmc):
            pass

        def eject_media(self):
            calls.append("eject")
            return False

        def insert_media(self, url):
            calls.append(f"insert:{url}")

        def boot_once_cd(self):
            calls.append("boot_once_cd")

        def power_on(self):
            calls.append("power_on")

    monkeypatch.setattr(converge.metal_redfish, "Redfish", FakeRedfish)
    monkeypatch.setattr(
        converge.talosctl, "apply_config_insecure", lambda _ip, _c: calls.append("apply")
    )

    converge._join_metal(
        _pending_metal_cfg(redfish=True), {"rp001": "rp001-config"},
        ABSENT_TALOSCONFIG, Path("/nonexistent/kubeconfig"),
    )

    assert calls == ["eject", "insert:http://iso", "boot_once_cd", "power_on", "apply"]


def test_join_metal_honours_the_machines_own_boot_timeout(monkeypatch):
    """A group of cold-booting hardware raises its budget once; the wait must
    use the machine's value, not a module constant."""
    slept: list[float] = []
    now = iter([0.0] + [float(t) for t in range(0, 4000, 10)])
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(converge.time, "sleep", lambda s: slept.append(s))

    server = _pending_metal_cfg(boot_timeout=1800).metal.groups["phoenix"].servers["rp001"]
    assert converge._wait_maintenance(server, "192.0.2.61") is False
    # polled across the full 1800s budget, not the 600s default
    assert len(slept) * converge._METAL_MAINTENANCE_INTERVAL_S >= 1700


def test_join_metal_skips_a_machine_that_never_reaches_maintenance(monkeypatch):
    """A machine that does not come up is reported, not fatal: the rest of the
    converge is unaffected and the next run picks it up."""
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _e: "sch")
    monkeypatch.setattr(converge.factory, "nocloud_iso_url", lambda _s, _v: "http://iso")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(converge, "_wait_maintenance", lambda *_a: False)
    monkeypatch.setattr(converge, "_boot_metal", lambda *_a: None)
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config_insecure",
        lambda *_a: pytest.fail("a machine that never came up must not be applied"),
    )

    converge._join_metal(
        _pending_metal_cfg(redfish=True), {"rp001": "rp001-config"},
        ABSENT_TALOSCONFIG, Path("/nonexistent/kubeconfig"),
    )


def test_join_metal_touches_nothing_in_a_dry_run(monkeypatch):
    """Plan reports what a converge would join and mutates nothing."""
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _e: "sch")
    monkeypatch.setattr(converge.factory, "nocloud_iso_url", lambda _s, _v: "http://iso")
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(
        converge.metal_redfish,
        "Redfish",
        lambda _bmc: pytest.fail("a dry run must not reach a BMC"),
    )
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config_insecure",
        lambda *_a: pytest.fail("a dry run must not apply a config"),
    )

    converge._join_metal(
        _pending_metal_cfg(redfish=True), {"rp001": "rp001-config"},
        ABSENT_TALOSCONFIG, Path("/nonexistent/kubeconfig"),
    )


def test_join_metal_skips_a_joined_machine_whose_kube_node_is_missing(
    monkeypatch, tmp_path, capsys
):
    """A redfish machine that answers apid with the cluster's identity but has
    no kube Node -- an operator deleted the Node object while repairing the
    machine, say -- looks unjoined to the compute phase. Booting it would
    force-restart a live node into the install media and wipe it (a control
    plane's etcd with it), so the probe `metal join` refuses on fires before
    any BMC action and the machine is skipped instead."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: testcluster\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: False)
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.talosctl, "maintenance_reachable", lambda _ip: False)
    monkeypatch.setattr(converge.talosctl, "reachable", lambda *_a, **_k: True)
    monkeypatch.setattr(converge.factory, "schematic_id", lambda _e: "sch")
    monkeypatch.setattr(converge, "dry_run", lambda: False)
    monkeypatch.setattr(
        converge.metal_redfish,
        "Redfish",
        lambda _bmc: pytest.fail("a joined machine must not be driven through its BMC"),
    )
    monkeypatch.setattr(
        converge.talosctl,
        "apply_config_insecure",
        lambda *_a: pytest.fail("a joined machine must not have a config applied"),
    )

    converge._join_metal(
        _pending_metal_cfg(redfish=True), {"rp001": "rp001-config"},
        talosconfig, kubeconfig,
    )

    assert "not reinstalling" in capsys.readouterr().err


def test_metal_unjoined_ignores_a_failed_node_query(monkeypatch, tmp_path):
    """A node query the api cannot answer (5xx, connection refused, an expired
    kubeconfig) is not an answer: the machine must not read as absent and be
    installed. Only an api answer that lacks the Node makes it pending."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: None)

    assert converge._metal_unjoined(_pending_metal_cfg(), kubeconfig) == []

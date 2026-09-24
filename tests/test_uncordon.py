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


# ---------------------------------------------------------------------------
# _reconcile_talos: SecureBoot installer guard
# ---------------------------------------------------------------------------

SB_IMAGE = "factory.talos.dev/nocloud-installer-secureboot/sch-123:v1.13.9"
PLAIN_IMAGE = "factory.talos.dev/nocloud-installer/sch-123:v1.13.9"


def _upgrade_image_guard_setup(monkeypatch, enforced):
    """Common wiring: one node an upgrade behind the target, the security state
    it reports captured by the guard probe."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9")
    machines = {"w-01": SimpleNamespace(role="worker", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"w-01": InfrastructureMachine("w-01")})
    upgrades: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"w-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.12.0")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "sch-123")
    monkeypatch.setattr(
        converge.talosctl, "secureboot_enforced", lambda *_a, **_kw: enforced
    )
    monkeypatch.setattr(
        converge.talosctl, "upgrade",
        lambda *_a, **_kw: upgrades.append(_kw.get("image") or _a[3]),
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    return cfg, machines, inventory, upgrades


def test_reconcile_talos_keeps_the_plain_installer_where_secure_boot_is_off(monkeypatch):
    """A node created before Secure Boot support booted the plain ISO and never
    enrolled the factory keys: it must not be pushed onto the UKI installer
    whether that boots there is still unverified. Its reported security state
    decides -- Secure Boot unenforced keeps the plain installer."""
    cfg, machines, inventory, upgrades = _upgrade_image_guard_setup(monkeypatch, False)

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base",): SB_IMAGE}, {("base",): "sch-123"},
        Path("talosconfig"), Path("kubeconfig"),
        plain_installer_images={("base",): PLAIN_IMAGE},
    )

    assert upgrades == [PLAIN_IMAGE]


def test_reconcile_talos_keeps_the_secureboot_installer_where_it_is_enforced(monkeypatch):
    """A node whose firmware enrolled the keys (every VM created since the
    SecureBoot switch) gets the SecureBoot installer, like before the guard."""
    cfg, machines, inventory, upgrades = _upgrade_image_guard_setup(monkeypatch, True)

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base",): SB_IMAGE}, {("base",): "sch-123"},
        Path("talosconfig"), Path("kubeconfig"),
        plain_installer_images={("base",): PLAIN_IMAGE},
    )

    assert upgrades == [SB_IMAGE]


def test_reconcile_talos_refuses_upgrade_on_an_unreadable_security_state(monkeypatch):
    """Unknown firmware state must never select either installer by guesswork."""
    cfg, machines, inventory, upgrades = _upgrade_image_guard_setup(monkeypatch, None)

    with pytest.raises(ReconcileError, match="cannot determine Secure Boot"):
        converge._reconcile_talos(
            cfg, machines, inventory, NetworkResult(),
            {("base",): SB_IMAGE}, {("base",): "sch-123"},
            Path("talosconfig"), Path("kubeconfig"),
            plain_installer_images={("base",): PLAIN_IMAGE},
        )

    assert upgrades == []


def test_reconcile_talos_probes_secure_boot_only_with_a_fallback_available(monkeypatch):
    """A backend that never boots a SecureBoot ISO (OpenStack) gets no probe:
    every extra talosctl call on the rollout path is one that can fail."""
    cfg, machines, inventory, upgrades = _upgrade_image_guard_setup(monkeypatch, True)
    probed: list[str] = []
    monkeypatch.setattr(
        converge.talosctl, "secureboot_enforced",
        lambda *_a, **_kw: probed.append("probe") or True,
    )

    converge._reconcile_talos(
        cfg, machines, inventory, NetworkResult(),
        {("base",): "installer:v1.13.9"}, {("base",): "sch-123"},
        Path("talosconfig"), Path("kubeconfig"),
    )

    assert probed == []
    assert upgrades == ["installer:v1.13.9"]


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


def test_reconcile_talos_upgrades_a_joined_metal_node(monkeypatch, make_config):
    """A joined metal machine is upgraded like any node -- reached at the static
    address of its cluster link and reinstalled onto the metal installer image
    its schematic resolves to -- while a machine with no kube Node has never
    joined and is skipped."""
    cfg = make_config(
        {
            "network": {"cluster": {"gateway": "192.168.0.1"}},
            "metal": {
                "site": {
                    "role": "worker",
                    "redfish": False,
                    "disk": "/dev/sda",
                    "servers": {
                        "srv01": {
                            "interfaces": {
                                "enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}
                            }
                        },
                        "srv02": {
                            "interfaces": {
                                "enp1s0f0": {"role": "cluster", "ip": "192.168.0.6/21"}
                            }
                        },
                    },
                }
            }
        }
    )
    upgrades: list[tuple[str, str]] = []
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.8")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "old-sch")
    monkeypatch.setattr(
        converge.talosctl,
        "upgrade",
        lambda _tc, _e, node, image: upgrades.append((node, image)),
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    # srv01 has joined (a kube Node exists); srv02 never did
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, n: n == "srv01")

    converge._reconcile_talos(
        cfg, {}, InfrastructureInventory(), NetworkResult(), {}, {},
        Path("talosconfig"), Path("kubeconfig"),
        metal_installer="factory.talos.dev/metal-installer/m-sch:v1.13.9",
        metal_schematic="m-sch",
    )

    assert upgrades == [
        ("192.168.0.5", "factory.talos.dev/metal-installer/m-sch:v1.13.9")
    ]


def test_reconcile_talos_reinstalls_a_metal_node_joined_by_an_early_dev_build(
    monkeypatch, make_config, capsys
):
    """A machine joined by a pre-81696eb 0.8.0 dev build runs the schematic
    computed before the metal trim -- the base set, qemu-guest-agent included --
    so at the same talos version only the running schematic differs. The one-off
    reinstall on the first converge is the accepted resolution (the changelog
    and the metal provider guide say so): the node reports "extensions changed"
    and is upgraded onto the metal installer, not treated as at-target."""
    cfg = make_config(
        {
            "network": {"cluster": {"gateway": "192.168.0.1"}},
            "metal": {
                "site": {
                    "role": "worker",
                    "redfish": False,
                    "disk": "/dev/sda",
                    "servers": {
                        "srv01": {
                            "interfaces": {
                                "enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}
                            }
                        },
                    },
                }
            }
        }
    )
    # what a pre-81696eb build baked into the metal installer: the base set
    # without the metal trim, so exactly the VM-only extensions differ
    assert (
        set(cfg._resolve_extensions({})) - set(cfg._resolve_extensions({}, metal=True))
        == {"siderolabs/qemu-guest-agent"}
    )
    upgrades: list[tuple[str, str]] = []
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: cfg.talos_version)
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "dev-sch")
    monkeypatch.setattr(
        converge.talosctl,
        "upgrade",
        lambda _tc, _e, node, image: upgrades.append((node, image)),
    )
    monkeypatch.setattr(converge, "_wait_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: True)
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, n: n == "srv01")

    converge._reconcile_talos(
        cfg, {}, InfrastructureInventory(), NetworkResult(), {}, {},
        Path("talosconfig"), Path("kubeconfig"),
        metal_installer="factory.talos.dev/metal-installer/m-sch:v1.13.9",
        metal_schematic="m-sch",
    )

    assert upgrades == [
        ("192.168.0.5", "factory.talos.dev/metal-installer/m-sch:v1.13.9")
    ]
    assert "srv01: extensions changed" in capsys.readouterr().out


def test_reconcile_talos_health_checks_a_metal_control_plane_at_target(
    monkeypatch, make_config
):
    """A metal control plane already at the target is not upgraded, but the
    resumed-rollout health barrier is still re-established before anything else
    is touched -- its apid answering proves nothing about etcd."""
    cfg = make_config(
        {
            "network": {"cluster": {"gateway": "192.168.0.1"}},
            "metal": {
                "site": {
                    "role": "controlplane",
                    "redfish": False,
                    "disk": "/dev/sda",
                    "servers": {
                        "srv01": {
                            "interfaces": {
                                "enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}
                            }
                        },
                    },
                }
            }
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", lambda *_a: "m-sch")
    monkeypatch.setattr(converge, "_uncordon_stale", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge, "_health_or_kube_fallback",
        lambda *_a, **_kw: calls.append("barrier") or True,
    )
    monkeypatch.setattr(
        converge.talosctl,
        "upgrade",
        lambda *_a, **_kw: pytest.fail("a node at the target must not be upgraded"),
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda *_a: True)

    converge._reconcile_talos(
        cfg, {}, InfrastructureInventory(), NetworkResult(), {}, {},
        Path("talosconfig"), Path("kubeconfig"),
        metal_installer="factory.talos.dev/metal-installer/m-sch:v1.13.9",
        metal_schematic="m-sch",
    )

    assert calls == ["barrier"]


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


def test_wait_version_treats_a_timed_out_probe_as_still_down(monkeypatch):
    """The version probe is bounded by a subprocess timeout, and a hung apid
    (one that accepted TCP but never answers) expires like any other failed
    read: the wait retries it instead of aborting or hanging past its own
    deadline."""
    polls = iter(["timeout", "v1.13.9"])

    def fake_version(*_a):
        result = next(polls)
        if result == "timeout":
            raise subprocess.TimeoutExpired("talosctl", 15)
        return result

    monkeypatch.setattr(converge.talosctl, "server_version", fake_version)
    monkeypatch.setattr(converge.time, "sleep", lambda s: None)
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(clock))

    converge._wait_version(Path("talosconfig"), "ep", "cp-01", "v1.13.9", timeout_s=60)
    # reached the target without error


def test_wait_version_treats_a_timed_out_schematic_read_as_still_down(monkeypatch):
    """Same for the schematic probe an extension-only upgrade polls: an expired
    read is the node still rebooting, not a failed rollout."""
    schematic_polls = iter(["timeout", "sch-123"])

    def fake_schematic(*_args):
        result = next(schematic_polls)
        if result == "timeout":
            raise subprocess.TimeoutExpired("talosctl", 15)
        return result

    monkeypatch.setattr(converge.talosctl, "server_version", lambda *_a: "v1.13.9")
    monkeypatch.setattr(converge.talosctl, "running_schematic", fake_schematic)
    monkeypatch.setattr(converge.time, "sleep", lambda s: None)
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(converge.time, "monotonic", lambda: next(clock))

    converge._wait_version(Path("talosconfig"), "ep", "cp-01", "v1.13.9",
                           want_schematic="sch-123", timeout_s=60)
    # reached the target without error


# ---------------------------------------------------------------------------
# _upgrade: kube-api must come back before the k8s version steps
# ---------------------------------------------------------------------------

def test_upgrade_aborts_when_kube_api_never_stabilizes(monkeypatch):
    """A k8s upgrade is refused if the api server never comes back after the
    machine-config apply (12 probes of 2 consecutive answering probes)."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    # running 1.34.2 means the kube-api branch (not the early return) runs
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.2")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda *_a: False)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    with pytest.raises(ReconcileError, match="did not stabilize before k8s upgrade"):
        converge._upgrade(
            cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
            {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
        )


def test_upgrade_aborts_when_kube_api_stabilizes_but_version_is_still_unknown(monkeypatch):
    """Two consecutive healthy probes are not enough: the version must be
    readable before stepping, or the upgrade is refused rather than guessed."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda *_a: True)
    calls = {"n": 0}

    def fake_version(*_a):
        calls["n"] += 1
        return "v1.34.2" if calls["n"] == 1 else None

    monkeypatch.setattr(converge.kubectl, "server_version", fake_version)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    with pytest.raises(ReconcileError, match="server version is still unavailable"):
        converge._upgrade(
            cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
            {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
        )


def test_upgrade_retries_a_timed_out_version_read(monkeypatch):
    """A hung `kubectl version` (`TimeoutExpired`) in the upgrade phase is a
    failed read that gets retried, as `_running_kubernetes_version` does, not a
    reason to abort the whole converge with the generic timeout message."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    calls = {"n": 0}

    def flaky_version(*_a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired("kubectl", 30)
        return "v1.34.2"

    monkeypatch.setattr(converge.kubectl, "server_version", flaky_version)
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda *_a: True)
    monkeypatch.setattr(converge.kubectl, "unschedulable", lambda _kc: [])
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge, "_k8s_upgrade_path", lambda *_a, **_kw: ["v1.35.8"])
    monkeypatch.setattr(converge.talosctl, "upgrade_k8s", lambda *_a, **_kw: None)

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )
    assert calls["n"] >= 2  # the timed-out first read was retried


def test_upgrade_dry_run_with_no_kubeconfig_skips_version_read_retries(monkeypatch, capsys):
    """A plan (dry run) with no non-empty kubeconfig on disk short-circuits
    before reading the version: the merged helper returns up front, so version
    reads and their 2s retry sleeps never run and no "retrying..." line prints
    before the "skipped in plan" verdict."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "dry_run", lambda: True)
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(converge, "_cluster_vips", lambda *_a, **_kw: [])
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    seen = {"reads": 0, "sleeps": []}

    def fake_version(*_a):
        seen["reads"] += 1
        return "v1.34.2"

    monkeypatch.setattr(converge.kubectl, "server_version", fake_version)
    monkeypatch.setattr(converge.time, "sleep", lambda s: seen["sleeps"].append(s))

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("/nonexistent/kubeconfig"),
    )
    out = capsys.readouterr().out
    assert seen["reads"] == 0  # no version read in a dry run with no kubeconfig
    assert seen["sleeps"] == []  # no retry sleeps either
    assert "skipped in plan" in out
    assert "retrying" not in out.lower()


def test_upgrade_stabilization_retries_a_timed_out_probe(monkeypatch):
    """A hung `cluster_up` probe in the stabilization loop counts as not-up and
    is retried, so one timeout does not abort the upgrade."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.2")
    monkeypatch.setattr(converge.kubectl, "unschedulable", lambda _kc: [])
    probes = {"n": 0}

    def flaky_up(*_a):
        probes["n"] += 1
        if probes["n"] == 1:
            raise subprocess.TimeoutExpired("kubectl", 30)
        return True

    monkeypatch.setattr(converge.kubectl, "cluster_up", flaky_up)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge, "_k8s_upgrade_path", lambda *_a, **_kw: ["v1.35.8"])
    upgraded = {"n": 0}
    monkeypatch.setattr(
        converge.talosctl, "upgrade_k8s",
        lambda *_a, **_kw: upgraded.__setitem__("n", upgraded["n"] + 1),
    )

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )
    assert upgraded["n"] == 1


def test_upgrade_k8s_sweep_uncordons_metal_nodes_too(monkeypatch, make_config):
    """upgrade-k8s cordons every node whose kubelet it swaps, the metal machines
    included, so the post-upgrade sweep must lift a cordon left on a metal node
    as well -- not only on the VM pools' nodes."""
    cfg = make_config(
        {
            "network": {"cluster": {"gateway": "192.168.0.1"}},
            "metal": {
                "site": {
                    "role": "controlplane",
                    "redfish": False,
                    "disk": "/dev/sda",
                    "servers": {
                        "srv01": {
                            "interfaces": {
                                "enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}
                            }
                        },
                    },
                }
            },
        }
    )
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, _n: True)
    # a running version older than the pin sends _upgrade through upgrade-k8s
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.30.2")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda *_a: True)
    monkeypatch.setattr(converge.kubectl, "unschedulable", lambda _kc: ["cp-01", "srv01"])
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge, "_k8s_upgrade_path", lambda *_a, **_kw: ["v1.31.0"])
    monkeypatch.setattr(converge.talosctl, "upgrade_k8s", lambda *_a, **_kw: None)
    uncordoned: list[str] = []
    monkeypatch.setattr(
        converge, "_uncordon_stale", lambda _kc, host: uncordoned.append(host)
    )

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )

    assert sorted(uncordoned) == ["cp-01", "srv01"]


def test_upgrade_aborts_when_version_unresolved_and_no_control_plane_resolves(monkeypatch):
    """An unresolved version must fail even when no control-plane address
    resolves (which would otherwise let the retry fall through silently)."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    # no member addresses and no inventory/network address -> cp1_address stays ""
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: None)
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    with pytest.raises(ReconcileError, match="no control-plane address resolved"):
        converge._upgrade(
            cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
            {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
        )


def test_upgrade_aborts_when_an_older_cluster_has_no_control_plane_address(monkeypatch):
    """A running cluster older than the target with no resolvable control-plane
    address must fail rather than skip the kubernetes upgrade: the compute phase
    would otherwise generate new-node configs at the target version, so new
    nodes and joining metal control planes come up a minor ahead of the
    running cluster."""
    cfg = SimpleNamespace(name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8")
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    # no member addresses and no inventory/network address -> nothing resolves
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    # a valid older running version: the silent-skip case, not the unknown one
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.2")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    with pytest.raises(ReconcileError, match="from v1.34.2 to v1.35.8"):
        converge._upgrade(
            cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
            {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
        )


def test_upgrade_drives_through_a_joined_metal_control_plane(monkeypatch):
    """With no VM control plane address resolving, a joined metal control plane
    is a valid upgrade-k8s target at its static cluster address."""
    metal = SimpleNamespace(groups={"site": SimpleNamespace(servers={
        "srv01": SimpleNamespace(name="srv01", role="controlplane"),
    })})
    cfg = SimpleNamespace(
        name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8",
        metal=metal,
    )
    inventory = InfrastructureInventory(machines={})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, _n: True)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.2")
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda *_a: True)
    monkeypatch.setattr(converge.kubectl, "unschedulable", lambda _kc: [])
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(converge, "_k8s_upgrade_path", lambda *_a, **_kw: ["v1.35.8"])
    seen: list[str] = []
    monkeypatch.setattr(
        converge.talosctl,
        "upgrade_k8s",
        lambda _tc, _ep, node, _step: seen.append(node),
    )

    converge._upgrade(
        cfg, {}, inventory, NetworkResult(), {}, {},
        Path("talosconfig"), Path("kubeconfig"),
    )
    assert seen == ["192.0.2.61"]


def test_upgrade_ignores_a_metal_control_plane_that_has_not_joined(monkeypatch):
    """A metal control plane with no kube Node has never joined, so there is
    nothing on it to upgrade: it is not an upgrade-k8s target and the run
    fails rather than skipping."""
    metal = SimpleNamespace(groups={"site": SimpleNamespace(servers={
        "srv01": SimpleNamespace(name="srv01", role="controlplane"),
    })})
    cfg = SimpleNamespace(
        name="test", talos_version="v1.13.9", kubernetes_version="v1.35.8",
        metal=metal,
    )
    inventory = InfrastructureInventory(machines={})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(converge.talosctl, "member_addresses", lambda *_a, **_kw: {})
    monkeypatch.setattr(converge.metal_talos, "cluster_ip", lambda _s: "192.0.2.61")
    monkeypatch.setattr(converge.kubectl, "node_exists", lambda _kc, _n: False)
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.34.2")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)

    with pytest.raises(ReconcileError, match="no control-plane address resolved"):
        converge._upgrade(
            cfg, {}, inventory, NetworkResult(), {}, {},
            Path("talosconfig"), Path("kubeconfig"),
        )


def test_upgrade_noop_for_unprefixed_kubernetes_pin(make_config, monkeypatch):
    """An unprefixed `kubernetes.version: 1.31.0` pin must be canonicalized to
    `v1.31.0` so converge does not schedule an upgrade against a server already
    reporting `v1.31.0` on every run. `make_config` loads through `load_config`,
    which applies the normalization, so any upgrade-k8s call here is a bug."""
    cfg = make_config({"kubernetes": {"version": "1.31.0"}})
    assert cfg.kubernetes_version == "v1.31.0"
    machines = {"cp-01": SimpleNamespace(role="controlplane", extensions=("base",))}
    inventory = InfrastructureInventory(machines={"cp-01": InfrastructureMachine("cp-01")})
    monkeypatch.setattr(converge, "_reconcile_talos", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        converge.talosctl, "member_addresses", lambda *_a, **_kw: {"cp-01": "192.0.2.1"}
    )
    # make_config builds a real Config (tailscale_enabled = False), so the
    # endpoint lookup would read a CWD talosconfig that a clean checkout lacks
    # and fail; stub it like the neighbors/machinery discovery above.
    monkeypatch.setattr(converge, "_talos_endpoint", lambda *_a, **_kw: "ep")
    monkeypatch.setattr(converge.kubectl, "server_version", lambda *_a: "v1.31.0")
    monkeypatch.setattr(converge.talosctl, "upgrade_k8s", lambda *_a, **_kw: pytest.fail(
        "converged unprefixed pin must not schedule a kubernetes upgrade"
    ))

    converge._upgrade(
        cfg, machines, inventory, NetworkResult(), {("base",): "installer:v1.13.9"},
        {("base",): "sch-123"}, Path("talosconfig"), Path("kubeconfig"),
    )

"""Tests for OpenStack compute unsupported-change detection, restarts and subnet DNS.

Servers are create-only on OpenStack, so a flavor, disk or availability-zone
edit on an existing server is refused before any converge phase mutates, an
existing subnet's DNS nameservers are reconciled in place instead of being
ignored, and a restart goes through Nova as a soft reboot. These tests use
fake Connection/Inventory objects; no cloud access.
"""

from __future__ import annotations

import types
from unittest import mock

import pytest

from taloscluster.errors import ReconcileError
from taloscluster.openstack import compute, network
from taloscluster.openstack.session import Inventory
from taloscluster.output import set_dry_run


def _server(name="testcluster-controlplane-01", flavor="gp.medium", az="nova", disk=40):
    return types.SimpleNamespace(
        name=name,
        flavor=types.SimpleNamespace(original_name=flavor, id="flavor-implicit"),
        availability_zone=az,
        attached_volumes=[{"id": "vol-boot-1", "delete_on_termination": True}],
        volumes=None,
    )


class FakeConn:
    def __init__(self, disk=40, flavor="gp.medium", volumes=None):
        # volumes the cloud actually knows; get_volume on any other id 404s.
        self.volumes = dict(volumes or {"vol-boot-1": disk})
        self.flavor = flavor
        self.get_flavor_calls = 0
        self.findById = "flavor-123"  # the id find_flavor resolves name -> id to
        self.volume = types.SimpleNamespace(get_volume=self._get_volume)
        self.compute = types.SimpleNamespace(
            get_flavor=self._get_flavor, find_flavor=self._find_flavor
        )

    def _get_volume(self, volume_id):
        if volume_id not in self.volumes:
            raise RuntimeError(f"volume {volume_id} not found (lookup by wrong id?)")
        return types.SimpleNamespace(id=volume_id, size=self.volumes[volume_id])

    def _get_flavor(self, flavor_id):
        self.get_flavor_calls += 1
        return types.SimpleNamespace(id=flavor_id, name=self.flavor)

    def _find_flavor(self, name_or_id):
        return types.SimpleNamespace(id=self.findById, name=self.flavor)


@pytest.fixture(autouse=True)
def _reset_dry_run():
    set_dry_run(False)
    yield
    set_dry_run(False)


def _inventory_with(server):
    inv = Inventory(mock.Mock(), "testcluster")
    inv._by_name["servers"] = {server.name: server}
    return inv


def test_validate_accepts_a_matching_existing_server(make_config):
    cfg = make_config()
    conn = FakeConn()
    server = _server()

    compute.validate(conn, cfg, cfg.machines, _inventory_with(server))  # must not raise


def test_validate_refuses_a_flavor_change(make_config):
    cfg = make_config()
    conn = FakeConn()
    server = _server(flavor="gp.xlarge")

    with pytest.raises(
        ReconcileError, match="refusing unsupported change to existing server"
    ):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_refuses_an_availability_zone_change(make_config):
    cfg = make_config()
    conn = FakeConn()
    server = _server(az="edge")

    with pytest.raises(
        ReconcileError, match="availability zone"
    ):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_refuses_a_disk_change(make_config):
    cfg = make_config()
    conn = FakeConn(disk=80)
    server = _server()

    with pytest.raises(
        ReconcileError, match="boot volume 80GB != configured 40GB"
    ):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_reads_disk_from_volume_id_not_attachment_id(make_config):
    # The attachment's id is the attachment UUID, not the Cinder volume.
    # FakeConn knows only vol-boot-1, so a lookup keyed on the attachment id
    # would 404 and be silently skipped -- the disk change would go undetected.
    cfg = make_config()
    conn = FakeConn(disk=80)
    server = _server()
    server.attached_volumes = [{"id": "attach-boot-1", "volume_id": "vol-boot-1"}]

    with pytest.raises(ReconcileError, match="boot volume 80GB != configured 40GB"):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_selects_the_boot_attachment(make_config):
    cfg = make_config()
    conn = FakeConn(volumes={"vol-data-1": 10, "vol-boot-1": 80})
    server = _server()
    server.attached_volumes = [
        {"id": "attach-data-1", "volume_id": "vol-data-1", "boot_index": 1},
        {"id": "attach-boot-1", "volume_id": "vol-boot-1", "boot_index": 0},
    ]

    with pytest.raises(ReconcileError, match="boot volume 80GB != configured 40GB"):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_selects_the_boot_attachment_without_boot_index(make_config):
    # VolumeAttachment has no boot_index, but marks the boot volume as deleted
    # with the server -- so a data volume listed first must not win.
    cfg = make_config()
    conn = FakeConn(volumes={"vol-data-1": 10, "vol-boot-1": 80})
    server = _server()
    server.attached_volumes = [
        {"id": "attach-data-1", "volume_id": "vol-data-1", "delete_on_termination": False},
        {"id": "attach-boot-1", "volume_id": "vol-boot-1", "delete_on_termination": True},
    ]

    with pytest.raises(ReconcileError, match="boot volume 80GB != configured 40GB"):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_selects_the_boot_volume_from_delete_on_termination_alone(make_config):
    # Nova reports attachments as {"id", "delete_on_termination"} with no
    # volume_id or boot_index, and a CSI data volume may be listed first --
    # its size must not stand in for the boot volume's.
    cfg = make_config()
    conn = FakeConn(volumes={"vol-data-1": 10, "vol-boot-1": 80})
    server = _server()
    server.attached_volumes = [
        {"id": "vol-data-1", "delete_on_termination": False},
        {"id": "vol-boot-1", "delete_on_termination": True},
    ]

    with pytest.raises(ReconcileError, match="boot volume 80GB != configured 40GB"):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_reads_disk_from_an_id_only_attachment(make_config):
    # Legacy nova replies carried only "id" for a single boot volume.
    cfg = make_config()
    conn = FakeConn(volumes={"vol-implicit-1": 80})
    server = _server()
    server.attached_volumes = [{"id": "vol-implicit-1"}]

    with pytest.raises(ReconcileError, match="boot volume 80GB != configured 40GB"):
        compute.validate(conn, cfg, cfg.machines, _inventory_with(server))


def test_validate_accepts_a_flavor_configured_as_an_id(make_config):
    # find_flavor accepts a flavor id at create time, so a config that names a
    # flavor by id must not be refused when the server reports that same id.
    cfg = make_config(
        {"controlplane": {"flavor": "flavor-123"}},
    )
    conn = FakeConn(flavor="something-else")
    server = _server()
    server.flavor = types.SimpleNamespace(original_name="", id="flavor-123")

    compute.validate(conn, cfg, cfg.machines, _inventory_with(server))  # no raise


def test_validate_resolves_a_config_flavor_id_against_the_server_name(make_config):
    # Configured as a flavor id, the server reports the flavor by name only;
    # the id is resolved through find_flavor so the cluster is not refused.
    cfg = make_config(
        {"controlplane": {"flavor": "flavor-123"}},
    )
    conn = FakeConn()
    conn.findById = "flavor-123"
    server = _server()  # reports name "gp.medium", id "flavor-implicit"
    server.flavor = types.SimpleNamespace(original_name="gp.medium", id="")

    compute.validate(conn, cfg, cfg.machines, _inventory_with(server))  # no raise


def test_validate_skips_comparison_when_the_flavor_is_unreadable(make_config):
    cfg = make_config()
    conn = FakeConn()
    server = _server()
    server.flavor = types.SimpleNamespace(original_name="", id="")  # nothing known

    compute.validate(conn, cfg, cfg.machines, _inventory_with(server))  # no raise


def test_validate_resolves_a_flavor_id_only_reference(make_config):
    cfg = make_config()
    conn = FakeConn()
    server = _server()
    server.flavor = types.SimpleNamespace(original_name="", id="flavor-123")
    conn.flavor = "gp.medium"

    # resolves through compute.get_flavor(id); must not raise
    compute.validate(conn, cfg, cfg.machines, _inventory_with(server))
    assert conn.get_flavor_calls == 1


def test_validate_tolerates_an_unreadable_boot_volume(make_config):
    cfg = make_config()
    server = _server()
    server.attached_volumes = None

    compute.validate(FakeConn(), cfg, cfg.machines, _inventory_with(server))  # no raise


# ---- restart -------------------------------------------------------------------

class FakeRebootConn:
    """A compute proxy cycling one server through a soft reboot.

    ``get_server`` hands out the scripted statuses in order and repeats the
    last one; ``wait_for_server`` records the status of the server it was
    handed, so a wait fed the cached already-ACTIVE inventory object (which
    the SDK returns from immediately) cannot pass for a waited-out reboot.
    """

    def __init__(self, statuses=("ACTIVE", "REBOOT", "REBOOT")):
        self.calls = []
        self._statuses = list(statuses)
        self.compute = types.SimpleNamespace(
            reboot_server=self._reboot, get_server=self._get, wait_for_server=self._wait
        )

    def _reboot(self, server_id, reboot_type):
        self.calls.append(("reboot", server_id, reboot_type))

    def _get(self, server_id):
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        self.calls.append(("get", status))
        return types.SimpleNamespace(id=server_id, status=status)

    def _wait(self, server, status, wait):
        self.calls.append(("wait", getattr(server, "status", None), status, wait))


def _owned_server():
    server = _server()
    server.id = "server-1"
    # Nova keeps a server ACTIVE until the guest starts shutting down, so the
    # inventory object carries exactly the stale-ACTIVE status a soft reboot
    # starts from
    server.status = "ACTIVE"
    return server


def test_restart_node_polls_a_fresh_server_until_the_reboot_starts(make_config, monkeypatch):
    monkeypatch.setattr("taloscluster.openstack.compute.time.sleep", lambda _s: None)
    conn = FakeRebootConn()

    compute.restart_node(conn, "testcluster-controlplane-01", _inventory_with(_owned_server()))

    # the inventory object is already ACTIVE; the reboot is only waited out
    # through fresh get_server polls -- down first, then the SDK's wait for the
    # new boot, fed the freshly fetched (non-ACTIVE) server
    assert conn.calls == [
        ("reboot", "server-1", "SOFT"),
        ("get", "ACTIVE"),
        ("get", "REBOOT"),
        ("get", "REBOOT"),
        ("wait", "REBOOT", "ACTIVE", 300),
    ]


def test_restart_node_fails_when_the_server_never_leaves_active(make_config, monkeypatch):
    monkeypatch.setattr("taloscluster.openstack.compute.time.sleep", lambda _s: None)
    clock = iter([0, 301])
    monkeypatch.setattr(
        "taloscluster.openstack.compute.time.monotonic", lambda: next(clock)
    )
    conn = FakeRebootConn(["ACTIVE"])

    with pytest.raises(ReconcileError, match="never left ACTIVE after the soft reboot"):
        compute.restart_node(conn, "testcluster-controlplane-01", _inventory_with(_owned_server()))


def test_restart_node_refuses_an_unknown_server(make_config):
    conn = FakeRebootConn()

    with pytest.raises(ReconcileError, match="cannot restart unknown OpenStack server"):
        compute.restart_node(conn, "foreign", _inventory_with(_owned_server()))
    assert conn.calls == []


def test_restart_node_is_read_only_under_plan(make_config):
    conn = FakeRebootConn()
    set_dry_run(True)

    compute.restart_node(conn, "testcluster-controlplane-01", _inventory_with(_owned_server()))

    assert conn.calls == []


# ---- subnet DNS reconciliation -------------------------------------------------

def _subnet(name, dns=None):
    return types.SimpleNamespace(name=name, id="subnet-1", dns_nameservers=list(dns) if dns else [])


class FakeNetConn:
    def __init__(self):
        self.updated = []
        self.network = types.SimpleNamespace(update_subnet=self._update_subnet)

    def _update_subnet(self, sub, **kwargs):
        self.updated.append((sub.name, kwargs))


def _subnet_inv(sub):
    inv = Inventory(mock.Mock(), "testcluster")
    inv._by_name["subnets"] = {sub.name: sub}
    return inv


def test_existing_subnet_dns_is_updated_in_place(make_config, capsys):
    cfg = make_config({"network": {"dns": ["1.1.1.1", "8.8.8.8"]}})
    conn = FakeNetConn()
    sub = _subnet("testcluster-subnet", dns=["1.1.1.1"])
    net = types.SimpleNamespace(id="net-1")

    network._ensure_subnet(conn, cfg, net, _subnet_inv(sub), ["tag"])

    assert conn.updated == [("testcluster-subnet", {"dns_nameservers": ["1.1.1.1", "8.8.8.8"]})]
    msg = "update subnet testcluster-subnet dns (1.1.1.1 -> 1.1.1.1, 8.8.8.8)"
    assert msg in capsys.readouterr().out


def test_existing_subnet_dns_matching_is_left_alone(make_config):
    cfg = make_config({"network": {"dns": ["1.1.1.1"]}})
    conn = FakeNetConn()
    sub = _subnet("testcluster-subnet", dns=["1.1.1.1"])

    network._ensure_subnet(conn, cfg, types.SimpleNamespace(id="net-1"), _subnet_inv(sub), ["tag"])

    assert conn.updated == []


def test_existing_subnet_dns_update_is_reported_by_plan(make_config, capsys):
    cfg = make_config({"network": {"dns": ["1.1.1.1", "8.8.8.8"]}})
    conn = FakeNetConn()
    sub = _subnet("testcluster-subnet", dns=["1.1.1.1"])
    set_dry_run(True)

    network._ensure_subnet(conn, cfg, types.SimpleNamespace(id="net-1"), _subnet_inv(sub), ["tag"])

    assert "[dry-run] update subnet testcluster-subnet dns" in capsys.readouterr().out
    assert conn.updated == []

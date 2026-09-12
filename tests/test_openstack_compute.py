"""Tests for OpenStack compute unsupported-change detection and subnet DNS.

Servers are create-only on OpenStack, so a flavor, disk or availability-zone
edit on an existing server is refused before any converge phase mutates, and
an existing subnet's DNS nameservers are reconciled in place instead of being
ignored. These tests use fake Connection/Inventory objects; no cloud access.
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
        attached_volumes=[
            {"id": "attach-boot-1", "volume_id": "vol-boot-1", "boot_index": 0}
        ],
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

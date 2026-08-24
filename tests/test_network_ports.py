"""Tests for the port reconcilers in taloscluster.openstack.network.

A reserved VIP port is the port a floating ip is associated with, so it has to
carry the cluster security group -- Neutron would otherwise drop a new port into
the project's `default` group, which allows nothing inbound. These tests use a
fake Connection/Inventory; no cloud access.
"""

from __future__ import annotations

import types

import pytest

from taloscluster.openstack import network
from taloscluster.output import set_dry_run

SG = types.SimpleNamespace(id="sg-cluster", name="testcluster")
OTHER_SG = "sg-project-default"
NET = types.SimpleNamespace(id="net-1")


class FakeConn:
    """Records create_port / update_port calls."""

    def __init__(self):
        self.created: list[dict] = []
        self.updated: list[tuple] = []
        self.network = types.SimpleNamespace(
            create_port=self._create_port,
            update_port=self._update_port,
            set_tags=lambda *a, **k: None,
        )

    def _create_port(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(
            name=kwargs["name"], id="port-new",
            security_group_ids=kwargs.get("security_group_ids", []),
        )

    def _update_port(self, port, **kwargs):
        self.updated.append((port.name, kwargs))
        return port


class FakeInv:
    def __init__(self, existing=None):
        self.existing = existing or {}

    def get(self, kind, name):
        return self.existing.get((kind, name))

    def put(self, kind, obj):
        return obj


@pytest.fixture(autouse=True)
def _live_run():
    """These tests exercise the real (non-dry-run) path."""
    set_dry_run(False)
    yield
    set_dry_run(False)


def _port(name, sgs):
    return types.SimpleNamespace(name=name, id=f"id-{name}", security_group_ids=list(sgs),
                                 allowed_address_pairs=[])


def test_new_reserved_port_gets_the_cluster_sg():
    conn, inv = FakeConn(), FakeInv()
    network._ensure_port(conn, "testcluster-ingress", NET, inv, ["tag"], SG)
    assert conn.created[0]["security_group_ids"] == ["sg-cluster"]


def test_existing_reserved_port_in_the_default_sg_is_corrected():
    """The bug this closes: ports created before the SG was passed sit in the
    project default group, so the floating ip's own port allows nothing."""
    conn = FakeConn()
    inv = FakeInv({("ports", "testcluster-ingress"): _port("testcluster-ingress", [OTHER_SG])})
    network._ensure_port(conn, "testcluster-ingress", NET, inv, ["tag"], SG)
    assert conn.updated == [("testcluster-ingress", {"security_groups": ["sg-cluster"]})]


def test_existing_reserved_port_already_correct_is_left_alone():
    conn = FakeConn()
    inv = FakeInv({("ports", "testcluster-ingress"): _port("testcluster-ingress", ["sg-cluster"])})
    network._ensure_port(conn, "testcluster-ingress", NET, inv, ["tag"], SG)
    assert conn.updated == []
    assert conn.created == []


def test_port_sg_reconcile_is_a_noop_without_an_sg():
    """dry-run/plan can reach here with sg=None; it must not touch the port."""
    conn = FakeConn()
    network._reconcile_port_sg(conn, _port("p", [OTHER_SG]), None)
    assert conn.updated == []


def test_dry_run_reports_but_does_not_update():
    conn = FakeConn()
    set_dry_run(True)
    network._reconcile_port_sg(conn, _port("p", [OTHER_SG]), SG)
    assert conn.updated == []

"""Tests for the reconcilers in taloscluster.openstack.network.

A reserved VIP port is the port a floating ip is associated with, so it has to
carry the cluster security group -- Neutron would otherwise drop a new port into
the project's `default` group, which allows nothing inbound. The network itself
states the cluster's jumbo MTU, since a tenant network left at the cloud default
drops the large on-subnet frames the node links carry. These tests use a fake
Connection/Inventory; no cloud access.
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
        self.created_networks: list[dict] = []
        self.updated: list[tuple] = []
        self.deleted: list[str] = []
        self.network = types.SimpleNamespace(
            create_network=self._create_network,
            create_port=self._create_port,
            update_port=self._update_port,
            get_tags=lambda resource: list(resource.tags),
            set_tags=self._set_tags,
            delete_port=self.deleted.append,
        )

    def _create_network(self, **kwargs):
        self.created_networks.append(kwargs)
        return types.SimpleNamespace(
            name=kwargs["name"], id="net-new",
            tags=list(kwargs.get("tags", [])),
            mtu=kwargs.get("mtu"),
        )

    def _create_port(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(
            name=kwargs["name"], id="port-new",
            tags=list(kwargs.get("tags", [])),
            security_group_ids=kwargs.get("security_group_ids", []),
        )

    def _set_tags(self, resource, tags):
        resource.tags = list(tags)
        return resource

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

    def all(self, kind):
        return {name: obj for (k, name), obj in self.existing.items() if k == kind}

    def drop(self, kind, name):
        self.existing.pop((kind, name), None)


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
    assert conn.created[0]["tags"] == ["tag"]


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


def test_dry_run_does_not_attach_router_interface():
    calls = []
    conn = types.SimpleNamespace(
        network=types.SimpleNamespace(
            add_interface_to_router=lambda *_args, **_kwargs: calls.append("attach")
        )
    )
    router = types.SimpleNamespace(name="testcluster-router")
    subnet = types.SimpleNamespace(name="testcluster-subnet")

    set_dry_run(True)
    network._ensure_router_interface(conn, router, subnet)

    assert calls == []


# ---- the network's own MTU ---------------------------------------------------

def _cfg(mtu):
    return types.SimpleNamespace(
        name="testcluster",
        network=types.SimpleNamespace(cluster=types.SimpleNamespace(mtu=mtu)),
    )


def _net(mtu):
    return types.SimpleNamespace(name="testcluster-net", id="net-old", mtu=mtu)


def test_new_network_states_the_jumbo_mtu():
    conn, inv = FakeConn(), FakeInv()
    network._ensure_network(conn, _cfg(8950), inv, ["tag"])
    assert conn.created_networks[0]["mtu"] == 8950


def test_new_network_at_the_default_mtu_states_none():
    """1500 is what Neutron derives for a flat network anyway, and an overlay
    cloud's real path can sit below it, so the default stays unstated."""
    conn, inv = FakeConn(), FakeInv()
    network._ensure_network(conn, _cfg(1500), inv, ["tag"])
    assert "mtu" not in conn.created_networks[0]


def test_existing_network_below_the_cluster_mtu_warns(capsys):
    """The bug this closes: the node links state the jumbo MTU while a network
    created before it was stated still advertises the cloud default, and every
    large on-subnet frame is dropped with nothing reported."""
    conn = FakeConn()
    inv = FakeInv({("networks", "testcluster-net"): _net(1450)})
    network._ensure_network(conn, _cfg(8950), inv, ["tag"])
    err = capsys.readouterr().err
    assert "network testcluster-net MTU is below the cluster MTU 8950" in err
    assert "1450" in err
    assert conn.created_networks == []


def test_existing_network_carrying_the_cluster_mtu_is_left_alone(capsys):
    conn = FakeConn()
    inv = FakeInv({("networks", "testcluster-net"): _net(9000)})
    network._ensure_network(conn, _cfg(8950), inv, ["tag"])
    assert capsys.readouterr().err == ""


def test_no_mtu_warning_when_the_cloud_reports_none(capsys):
    """A cloud without the network-MTU extension reports no mtu to compare."""
    conn = FakeConn()
    inv = FakeInv({("networks", "testcluster-net"): _net(None)})
    network._ensure_network(conn, _cfg(8950), inv, ["tag"])
    assert capsys.readouterr().err == ""


def test_no_mtu_warning_at_the_default_mtu(capsys):
    """A 1500 cluster on an overlay network advertising less is what the cloud
    hands out via DHCP, so it does not warn."""
    conn = FakeConn()
    inv = FakeInv({("networks", "testcluster-net"): _net(1450)})
    network._ensure_network(conn, _cfg(1500), inv, ["tag"])
    assert capsys.readouterr().err == ""


def test_dry_run_does_not_create_the_network():
    conn, inv = FakeConn(), FakeInv()
    set_dry_run(True)
    network._ensure_network(conn, _cfg(8950), inv, ["tag"])
    assert conn.created_networks == []


# ---- stale machine ports ---------------------------------------------------

def test_stale_machine_port_is_deleted_when_no_longer_desired():
    """A network-only machine (port exists, no server) that a later converge no
    longer wants must have its port reclaimed; reserved VIP ports survive."""
    conn = FakeConn()
    inv = FakeInv({
        ("ports", "testcluster-worker-02"): _port("testcluster-worker-02", [SG.id]),
        ("ports", "testcluster-kubeapi"): _port("testcluster-kubeapi", [SG.id]),
        ("ports", "testcluster-ingress"): _port("testcluster-ingress", [SG.id]),
    })
    network._drop_stale_machine_ports(
        conn, "testcluster", {"testcluster-worker-01"}, inv
    )
    assert conn.deleted == ["id-testcluster-worker-02"]
    assert set(inv.all("ports")) == {
        "testcluster-kubeapi",
        "testcluster-ingress",
    }


def test_stale_port_with_a_live_server_is_left_for_scale_down():
    """A scaled-down machine whose server still exists is (drain + reset then)
    removed by ``_scale_down``; the network phase must not tear off its NIC."""
    conn = FakeConn()
    inv = FakeInv({
        ("ports", "testcluster-worker-02"): _port("testcluster-worker-02", [SG.id]),
        ("servers", "testcluster-worker-02"): types.SimpleNamespace(
            id="server-worker-02", name="testcluster-worker-02"
        ),
    })
    network._drop_stale_machine_ports(
        conn, "testcluster", {"testcluster-worker-01"}, inv
    )
    assert conn.deleted == []
    assert set(inv.all("ports")) == {"testcluster-worker-02"}


def test_desired_machine_port_is_kept():
    conn = FakeConn()
    inv = FakeInv({
        ("ports", "testcluster-worker-01"): _port("testcluster-worker-01", [SG.id]),
    })
    network._drop_stale_machine_ports(
        conn, "testcluster", {"testcluster-worker-01"}, inv
    )
    assert conn.deleted == []
    assert set(inv.all("ports")) == {"testcluster-worker-01"}


def test_dry_run_reports_but_does_not_delete_stale_port():
    conn = FakeConn()
    inv = FakeInv({
        ("ports", "testcluster-worker-02"): _port("testcluster-worker-02", [SG.id]),
    })
    set_dry_run(True)
    network._drop_stale_machine_ports(conn, "testcluster", {"testcluster-worker-01"}, inv)
    assert conn.deleted == []

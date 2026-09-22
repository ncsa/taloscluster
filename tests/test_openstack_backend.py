"""Backend-level tests for the OpenStack infrastructure backend.

`current_network` reports the cluster's converged addresses from the
reserved VIP ports and their floating ips, and `destroy_resources` tears
the cluster down in dependency order -- servers first (their ports ride
along), then floating ips and the reserved ports, then the router after
its managed subnets are detached, and finally subnet, network and security
group. These tests use fake Connection/Inventory objects; no cloud access.
"""

from __future__ import annotations

import types
from unittest import mock

import pytest

from taloscluster.infrastructure import Endpoint, InfrastructureInventory
from taloscluster.openstack.backend import OpenStackBackend
from taloscluster.openstack.session import Inventory
from taloscluster.output import set_dry_run


@pytest.fixture(autouse=True)
def _reset_dry_run():
    set_dry_run(False)
    yield
    set_dry_run(False)


def _backend(cfg, conn):
    backend = object.__new__(OpenStackBackend)
    backend.cfg = cfg
    backend.conn = conn
    return backend


def _port(name, ip):
    return types.SimpleNamespace(
        name=name, id=f"port-{name}", fixed_ips=[{"ip_address": ip}]
    )


def _fip(description, address, id_):
    return types.SimpleNamespace(
        description=description, id=id_, floating_ip_address=address
    )


def _inventory() -> Inventory:
    """A converged cluster: two servers with their ports, the reserved VIP
    ports with floating ips, and the managed network stack."""
    inv = Inventory(mock.Mock(), "testcluster")
    inv._by_name["servers"] = {
        "testcluster-controlplane-01": types.SimpleNamespace(id="server-cp"),
        "testcluster-worker-01": types.SimpleNamespace(id="server-w"),
    }
    inv._by_name["ports"] = {
        "testcluster-controlplane-01": _port("testcluster-controlplane-01", "192.168.0.5"),
        "testcluster-worker-01": _port("testcluster-worker-01", "192.168.0.6"),
        "testcluster-kubeapi": _port("testcluster-kubeapi", "192.168.0.10"),
        "testcluster-ingress": _port("testcluster-ingress", "192.168.0.11"),
    }
    inv._by_name["ips"] = {
        "testcluster-kubeapi": _fip("testcluster-kubeapi", "203.0.113.10", "ip-kubeapi"),
        "testcluster-ingress": _fip("testcluster-ingress", "203.0.113.20", "ip-ingress"),
    }
    inv._by_name["routers"] = {"testcluster-router": types.SimpleNamespace(id="router-1")}
    inv._by_name["subnets"] = {"testcluster-subnet": types.SimpleNamespace(id="subnet-1")}
    inv._by_name["networks"] = {"testcluster-net": types.SimpleNamespace(id="net-1")}
    inv._by_name["security_groups"] = {"testcluster": types.SimpleNamespace(id="sg-1")}
    return inv


class FakeDestroyConn:
    """Records every teardown call in order."""

    def __init__(self):
        self.calls = []
        self.compute = types.SimpleNamespace(
            delete_server=lambda sid: self.calls.append(("delete_server", sid)),
            wait_for_delete=lambda server: self.calls.append(
                ("wait_for_delete", server.id)
            ),
        )
        self.network = types.SimpleNamespace(
            delete_ip=lambda fid: self.calls.append(("delete_ip", fid)),
            delete_port=lambda pid: self.calls.append(("delete_port", pid)),
            remove_interface_from_router=self._detach,
            delete_router=lambda rid: self.calls.append(("delete_router", rid)),
            delete_subnet=lambda sid: self.calls.append(("delete_subnet", sid)),
            delete_network=lambda nid: self.calls.append(("delete_network", nid)),
            delete_security_group=lambda gid: self.calls.append(
                ("delete_security_group", gid)
            ),
        )

    def _detach(self, router, subnet):
        self.calls.append(("detach_subnet", router.id, subnet))


# ---- current_network ---------------------------------------------------------


def test_current_network_reads_the_converged_endpoints(make_config):
    """The kubeapi port's fixed ip is the VIP with its floating ip in front of
    it, the ingress port is the MetalLB address, and both endpoints report
    their own floating ip as the advertised address."""
    inventory = InfrastructureInventory(provider_data=_inventory())
    network = _backend(make_config(), None).current_network(inventory)

    assert network.kubernetes == Endpoint(vip="192.168.0.10", advertised_address="203.0.113.10")
    assert network.ingress == Endpoint(vip="192.168.0.11", advertised_address="203.0.113.20")
    assert network.metallb == ("192.168.0.11",)


def test_current_network_without_floating_ips_reports_empty_addresses(make_config):
    """Before the network phase (or without external connectivity) the ports
    still hold the fixed ips, but nothing is advertised."""
    inv = Inventory(mock.Mock(), "testcluster")
    inv._by_name["ports"] = {
        "testcluster-kubeapi": _port("testcluster-kubeapi", "192.168.0.10"),
        "testcluster-ingress": _port("testcluster-ingress", "192.168.0.11"),
    }
    inventory = InfrastructureInventory(provider_data=inv)
    network = _backend(make_config(), None).current_network(inventory)

    assert network.kubernetes == Endpoint(vip="192.168.0.10", advertised_address="")
    assert network.ingress == Endpoint(vip="192.168.0.11", advertised_address="")
    assert network.metallb == ("192.168.0.11",)


# ---- destroy_resources -------------------------------------------------------


def test_destroy_resources_tears_the_cluster_down_in_dependency_order(make_config):
    """Servers go first and consume their own ports, then the floating ips and
    the reserved ports, then the router -- with the managed subnets detached
    first -- and finally subnet, network and security group."""
    inv = _inventory()
    conn = FakeDestroyConn()

    _backend(make_config(), conn).destroy_resources(
        InfrastructureInventory(provider_data=inv)
    )

    assert conn.calls == [
        ("delete_server", "server-cp"),
        ("wait_for_delete", "server-cp"),
        ("delete_port", "port-testcluster-controlplane-01"),
        ("delete_server", "server-w"),
        ("wait_for_delete", "server-w"),
        ("delete_port", "port-testcluster-worker-01"),
        ("delete_ip", "ip-kubeapi"),
        ("delete_ip", "ip-ingress"),
        ("delete_port", "port-testcluster-kubeapi"),
        ("delete_port", "port-testcluster-ingress"),
        ("detach_subnet", "router-1", "subnet-1"),
        ("delete_router", "router-1"),
        ("delete_subnet", "subnet-1"),
        ("delete_network", "net-1"),
        ("delete_security_group", "sg-1"),
    ]
    # the machine ports were consumed by the server deletion, not re-deleted
    assert inv.all("ports") == {}


def test_destroy_resources_is_read_only_under_plan(make_config):
    conn = FakeDestroyConn()
    set_dry_run(True)

    _backend(make_config(), conn).destroy_resources(
        InfrastructureInventory(provider_data=_inventory())
    )

    assert conn.calls == []

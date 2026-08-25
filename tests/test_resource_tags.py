"""Managed Neutron resources receive ownership tags in their create request."""

from __future__ import annotations

from types import SimpleNamespace

from taloscluster.openstack import network, security


class EmptyInventory:
    def get(self, _kind, _name):
        return None

    def put(self, _kind, obj):
        return obj

    def put_keyed(self, _kind, _key, obj):
        return obj


class NetworkProxy:
    def __init__(self):
        self.created: dict[str, dict] = {}

    def _create(self, kind, **kwargs):
        self.created[kind] = kwargs
        return SimpleNamespace(
            id=f"{kind}-id",
            name=kwargs.get("name", kind),
            description=kwargs.get("description", kind),
            fixed_ips=[{"ip_address": "192.0.2.10"}],
            floating_ip_address="198.51.100.10",
        )

    def create_network(self, **kwargs):
        return self._create("network", **kwargs)

    def create_subnet(self, **kwargs):
        return self._create("subnet", **kwargs)

    def create_router(self, **kwargs):
        return self._create("router", **kwargs)

    def create_port(self, **kwargs):
        return self._create("port", **kwargs)

    def create_ip(self, **kwargs):
        return self._create("ip", **kwargs)

    def create_security_group(self, **kwargs):
        return self._create("security_group", **kwargs)

    def security_group_rules(self, **_kwargs):
        return []

    def create_security_group_rule(self, **_kwargs):
        return None


def test_all_neutron_creates_include_ownership_tags():
    tags = ["managed-by=taloscluster", "cluster=testcluster"]
    proxy = NetworkProxy()
    conn = SimpleNamespace(network=proxy)
    inv = EmptyInventory()
    cfg = SimpleNamespace(
        name="testcluster",
        cidr="192.0.2.0/24",
        dns=["1.1.1.1"],
        security_talos={},
        security_kubernetes={},
    )
    net = network._ensure_network(conn, cfg.name, inv, tags)
    network._ensure_subnet(conn, cfg, net, inv, tags)
    network._ensure_router(conn, cfg.name, SimpleNamespace(id="external"), inv, tags)
    port = network._ensure_port(conn, "testcluster-ingress", net, inv, tags, None)
    network._ensure_fip(
        conn, "testcluster-ingress", SimpleNamespace(id="external"), port, inv, tags
    )
    security.reconcile(conn, cfg, inv)

    assert set(proxy.created) == {
        "network", "subnet", "router", "port", "ip", "security_group",
    }
    assert all(call["tags"] == tags for call in proxy.created.values())

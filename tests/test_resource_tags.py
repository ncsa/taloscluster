"""Managed Neutron resources receive ownership tags in their create request."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openstack import exceptions

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
            tags=list(kwargs.get("tags", [])),
            fixed_ips=[{"ip_address": "192.0.2.10"}],
            floating_ip_address="198.51.100.10",
        )

    def get_tags(self, resource):
        return list(resource.tags)

    def set_tags(self, resource, tags):
        resource.tags = list(tags)
        return resource

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
        security={},
        open_ports=lambda: (80, 443),
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


def test_security_group_tags_after_create_when_post_rejects_them():
    tags = ["managed-by=taloscluster", "cluster=testcluster"]

    class LegacyProxy(NetworkProxy):
        def __init__(self):
            super().__init__()
            self.attempts = []
            self.tagged = None

        def create_security_group(self, **kwargs):
            self.attempts.append(kwargs)
            if "tags" in kwargs:
                raise exceptions.BadRequestException("Attribute 'tags' not allowed in POST")
            return self._create("security_group", **kwargs)

        def set_tags(self, resource, resource_tags):
            self.tagged = (resource, resource_tags)
            resource.tags = list(resource_tags)
            return resource

        def delete_security_group(self, _resource_id):
            raise AssertionError("successful tagging must not delete the resource")

    proxy = LegacyProxy()
    conn = SimpleNamespace(network=proxy)
    cfg = SimpleNamespace(
        name="testcluster", security={}, open_ports=lambda: (80, 443)
    )

    sg = security.reconcile(conn, cfg, EmptyInventory())

    assert proxy.attempts[0]["tags"] == tags
    assert "tags" not in proxy.attempts[1]
    assert proxy.tagged == (sg, tags)


def test_silently_ignored_create_tags_are_applied_afterward():
    tags = ["managed-by=taloscluster", "cluster=testcluster"]

    class SilentProxy(NetworkProxy):
        def create_port(self, **kwargs):
            without_tags = {key: value for key, value in kwargs.items() if key != "tags"}
            return self._create("port", **without_tags)

    proxy = SilentProxy()
    conn = SimpleNamespace(network=proxy)
    network_resource = SimpleNamespace(id="network-id")

    port = network._ensure_port(
        conn, "testcluster-ingress", network_resource, EmptyInventory(), tags, None
    )

    assert port.tags == tags


def test_failed_fallback_tagging_deletes_the_untagged_resource():
    class FailingProxy(NetworkProxy):
        def __init__(self):
            super().__init__()
            self.deleted = []

        def create_security_group(self, **kwargs):
            if "tags" in kwargs:
                raise exceptions.BadRequestException("Attribute 'tags' not allowed in POST")
            return self._create("security_group", **kwargs)

        def set_tags(self, _resource, _tags):
            raise RuntimeError("tagging failed")

        def delete_security_group(self, resource_id):
            self.deleted.append(resource_id)

    proxy = FailingProxy()
    conn = SimpleNamespace(network=proxy)
    cfg = SimpleNamespace(
        name="testcluster", security={}, open_ports=lambda: (80, 443)
    )

    with pytest.raises(RuntimeError, match="tagging failed"):
        security.reconcile(conn, cfg, EmptyInventory())
    assert proxy.deleted == ["security_group-id"]

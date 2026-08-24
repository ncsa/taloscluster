"""Tests for taloscluster.openstack.session.Inventory tag discovery.

Resources created before the clusterctl -> taloscluster rename carry
`managed-by=clusterctl`; both values must still be found.
"""

from __future__ import annotations

from types import SimpleNamespace

from taloscluster.openstack.session import Inventory

CLUSTER = "mycluster"


def _obj(name, tags):
    return SimpleNamespace(name=name, description=name, tags=tags)


def _conn(objs):
    empty = lambda *a, **k: []  # noqa: E731
    network = SimpleNamespace(
        networks=lambda: objs, subnets=empty, routers=empty, ports=empty,
        security_groups=empty, ips=empty,
    )
    return SimpleNamespace(network=network, compute=SimpleNamespace(servers=empty))


def _load(objs):
    return Inventory(_conn(objs), CLUSTER).load().all("networks")


def test_finds_both_managed_by_values():
    found = _load([
        _obj("new", ["managed-by=taloscluster", "cluster=mycluster"]),
        _obj("old", ["managed-by=clusterctl", "cluster=mycluster"]),
    ])
    assert set(found) == {"new", "old"}


def test_ignores_other_clusters_and_untagged():
    found = _load([
        _obj("other", ["managed-by=taloscluster", "cluster=elsewhere"]),
        _obj("foreign", ["cluster=mycluster"]),
        _obj("bare", []),
    ])
    assert found == {}

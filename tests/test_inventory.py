"""Tests for taloscluster.openstack.session.Inventory tag discovery.

Resources created before the clusterctl -> taloscluster rename carry
`managed-by=clusterctl`; both values must still be found.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taloscluster.errors import ReconcileError
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


def test_get_rejects_an_untagged_name_collision():
    inv = Inventory(_conn([_obj("expected", [])]), CLUSTER).load()
    with pytest.raises(ReconcileError, match="ownership tags"):
        inv.get("networks", "expected")


def test_get_rejects_a_foreign_cluster_name_collision():
    inv = Inventory(_conn([
        _obj("expected", ["managed-by=taloscluster", "cluster=elsewhere"]),
    ]), CLUSTER).load()
    with pytest.raises(ReconcileError, match="refusing to adopt"):
        inv.get("networks", "expected")


def test_get_rejects_duplicate_managed_names():
    tags = ["managed-by=taloscluster", "cluster=mycluster"]
    inv = Inventory(_conn([_obj("duplicate", tags), _obj("duplicate", tags)]), CLUSTER).load()
    with pytest.raises(ReconcileError, match="multiple managed"):
        inv.get("networks", "duplicate")


def test_project_name_is_empty_without_an_auth_plugin():
    from taloscluster.openstack.session import project_name

    conn = SimpleNamespace(session=SimpleNamespace(auth=None))
    assert project_name(conn) == ""


def test_project_name_uses_auth_access_when_available():
    from taloscluster.openstack.session import project_name

    auth = SimpleNamespace(
        get_access=lambda session: SimpleNamespace(project_name="project-a")
    )
    conn = SimpleNamespace(session=SimpleNamespace(auth=auth))
    assert project_name(conn) == "project-a"


def _secret():
    from taloscluster.config import OpenStackSecrets, Secrets

    return Secrets(provider=OpenStackSecrets(credential_id="cid", credential_secret="csec"))


def test_connect_uses_the_configured_region(monkeypatch):
    """The session region comes from cluster.yaml's openstack.region, not RegionOne."""
    import openstack
    from taloscluster.openstack import session

    seen = {}
    monkeypatch.setattr(openstack, "connect", lambda **kw: seen.update(kw) or object())
    cfg = SimpleNamespace(openstack_url="https://cloud:5000/v3/", region="region-b")
    session.connect(cfg, _secret())
    assert seen["region_name"] == "region-b"
    cfg = SimpleNamespace(openstack_url="https://cloud:5000/v3/", region="RegionOne")
    seen.clear()
    session.connect(cfg, _secret())
    assert seen["region_name"] == "RegionOne"

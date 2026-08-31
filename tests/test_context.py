"""The Context handed to plugins.

The point of the pre-filled status payload is that a converge already knows the
ingress VIP and the OpenStack project, so a plugin must never trigger a second
round-trip to the cloud to learn them.
"""

from __future__ import annotations

import pytest

from taloscluster import converge as _converge
from taloscluster.context import Context


@pytest.fixture
def spy(monkeypatch):
    """Count calls to status_report, which is the expensive OpenStack path."""
    calls = []

    def fake(root):
        calls.append(root)
        return {
            "infrastructure": {"provider": "openstack", "url": "https://cloud"},
            "openstack": {"url": "https://cloud", "region": "RegionOne", "project": "proj"},
            "kubernetes": {"floating_ip": "1.2.3.4", "vip": "10.0.0.1",
                           "endpoint": "https://1.2.3.4:6443"},
            "ingress": {"floating_ip": "1.2.3.5", "vip": "10.0.0.2"},
        }

    monkeypatch.setattr(_converge, "status_report", fake)
    return calls


def test_from_converge_never_calls_status_report(spy, tmp_path, make_config):
    """The whole reason Context exists: converge hands over what it already has."""
    cfg = make_config()
    ctx = Context.from_converge(
        tmp_path, cfg,
        kubeapi={"floating_ip": "1.2.3.4", "vip": "10.0.0.1", "endpoint": "https://1.2.3.4:6443"},
        ingress={"floating_ip": "1.2.3.5", "vip": "10.0.0.2"},
        infrastructure={"provider": "openstack", "url": "https://cloud"},
        openstack={"url": "https://cloud", "region": "RegionOne", "project": "proj"},
    )
    assert ctx.ingress["vip"] == "10.0.0.2"
    assert ctx.infrastructure["provider"] == "openstack"
    assert ctx.openstack["project"] == "proj"
    assert ctx.kubernetes["endpoint"] == "https://1.2.3.4:6443"
    assert spy == []


def test_standalone_fetches_once_and_caches(spy, tmp_path, make_config):
    ctx = Context(root=tmp_path, cfg=make_config())
    assert ctx.ingress["vip"] == "10.0.0.2"
    assert ctx.openstack["project"] == "proj"
    assert ctx.kubernetes["vip"] == "10.0.0.1"
    assert len(spy) == 1


def test_a_plugin_that_asks_for_nothing_pays_nothing(spy, tmp_path, make_config):
    """rancher never looks at the cloud facts, so it must not trigger the fetch."""
    ctx = Context(root=tmp_path, cfg=make_config())
    assert ctx.root == tmp_path
    assert ctx.kubeconfig == tmp_path / "kubeconfig"
    assert ctx.talosconfig == tmp_path / "talosconfig"
    assert spy == []


def test_load_reads_cluster_yaml(spy, tmp_path, make_config):
    make_config()  # writes cluster.yaml into tmp_path
    ctx = Context.load(tmp_path)
    assert ctx.cfg.name == "testcluster"
    assert spy == []


def test_results_start_empty(tmp_path):
    """A plugin run on its own sees no earlier results, so consuming one must be
    optional."""
    assert Context(root=tmp_path, cfg=None).results == {}

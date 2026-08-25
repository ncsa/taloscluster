"""The rancher plugin's status/check reports.

Rancher is faked at the Client boundary -- these describe what converge would
change, which is exactly what `taloscluster check` gates CI on.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.context import Context

from taloscluster_rancher import reconcile as _converge
from taloscluster_rancher.client import MemberBinding, RancherCluster

CLUSTER = RancherCluster(id="c-abc12", name="testcluster", state="active")


class FakeClient:
    """Just the four calls status/check make."""

    def __init__(self, cluster=CLUSTER, bindings=(), principals=None):
        self._cluster = cluster
        self._bindings = list(bindings)
        self._principals = principals if principals is not None else {}

    def find_cluster(self, name):
        return self._cluster

    def list_member_bindings(self, cluster_id):
        return list(self._bindings)

    def resolve_principal(self, netid):
        pid = self._principals.get(netid)
        return {"id": pid} if pid else None


def binding(pid, role, bid="b-1"):
    return MemberBinding(id=bid, userPrincipalId=pid, groupPrincipalId=None,
                         roleTemplateId=role)


@pytest.fixture
def cluster_dir(tmp_path):
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "rancher": {"admins": ["alice"], "users": ["carol"]},
    }))
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({
        "rancher": {"url": "https://rancher.example.com", "token": "token-x:y"},
    }))
    return tmp_path


@pytest.fixture
def wire(monkeypatch):
    """Point the plugin at a FakeClient and a chosen agent state."""

    def _wire(client, agent_installed=True):
        monkeypatch.setattr(_converge, "_client", lambda secrets: client)
        monkeypatch.setattr(_converge, "downstream_rancher_id",
                            lambda root: "c-abc12" if agent_installed else None)

    return _wire


ALICE, CAROL = "ldap_user://alice", "ldap_user://carol"
PRINCIPALS = {"alice": ALICE, "carol": CAROL}
OWNER = binding("ldap_user://owner", "cluster-owner", bid="c-abc12:creator-cluster-owner")


def test_check_ok_when_everything_matches(cluster_dir, wire):
    wire(FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"),
                  binding(CAROL, "cluster-member", "b-2"), OWNER],
        principals=PRINCIPALS,
    ))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is True
    assert report["missing_members"] == []
    assert report["stale_members"] == []


def test_creator_binding_is_never_stale(cluster_dir, wire):
    """The cluster creator is preserved by converge, so it must not read as drift."""
    wire(FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"),
                  binding(CAROL, "cluster-member", "b-2"), OWNER],
        principals=PRINCIPALS,
    ))
    assert _converge.check(Context(root=cluster_dir, cfg=None))["stale_members"] == []


def test_check_reports_a_missing_member(cluster_dir, wire):
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner")], principals=PRINCIPALS))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["missing_members"] == [f"{CAROL} (cluster-member)"]


def test_check_reports_a_stale_member(cluster_dir, wire):
    wire(FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"),
                  binding(CAROL, "cluster-member", "b-2"),
                  binding("ldap_user://dave", "cluster-member", "b-3")],
        principals=PRINCIPALS,
    ))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["stale_members"] == ["ldap_user://dave (cluster-member)"]


def test_check_not_ok_without_the_agent(cluster_dir, wire):
    wire(
        FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"),
                             binding(CAROL, "cluster-member", "b-2")],
                   principals=PRINCIPALS),
        agent_installed=False,
    )
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["agent_installed"] is False


def test_check_not_ok_when_unregistered(cluster_dir, wire):
    wire(FakeClient(cluster=None))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["registered"] is False


def test_status_lists_members(cluster_dir, wire):
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner")], principals=PRINCIPALS))
    report = _converge.status(Context(root=cluster_dir, cfg=None))
    assert report["registered"] is True
    assert report["cluster_id"] == "c-abc12"
    assert report["members"] == [f"{ALICE} (cluster-owner)"]


def test_unresolvable_netid_shows_up_as_missing(cluster_dir, wire):
    """A typo'd netid must not raise; it reads as a member that is not there."""
    wire(FakeClient(bindings=[], principals={"alice": ALICE}))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["missing_members"] == [f"{ALICE} (cluster-owner)"]

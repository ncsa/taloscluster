"""The rancher plugin's status/check reports.

Rancher is faked at the Client boundary -- these describe what converge would
change, which is exactly what `taloscluster check` gates CI on.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.config import ConfigError
from taloscluster.context import Context

from taloscluster_rancher import reconcile as _converge
from taloscluster_rancher.client import MemberBinding, RancherCluster
from taloscluster_rancher.errors import RancherError

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

    def _wire(client, agent_installed=True, downstream_id="c-abc12"):
        monkeypatch.setattr(_converge, "_client", lambda secrets: client)
        monkeypatch.setattr(
            _converge, "downstream_rancher_id",
            lambda root: downstream_id if agent_installed else None,
        )

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
    # the report carries the cluster id in the shape argocd consumes
    assert report["cluster_id"] == "c-abc12"


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


def test_check_fails_on_downstream_id_mismatch(cluster_dir, wire):
    """A downstream agent whose id differs from the Rancher cluster id is the
    unrelated-registration converge refuses; check must fail even when the
    memberships otherwise match."""
    wire(
        FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"),
                             binding(CAROL, "cluster-member", "b-2"), OWNER],
                   principals=PRINCIPALS),
        downstream_id="c-unrelated",
    )
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["agent_installed"] is True
    assert report["id_match"] is False
    assert report["downstream_id"] == "c-unrelated"
    assert "c-abc12" in report["id_mismatch_reason"]
    assert "c-unrelated" in report["id_mismatch_reason"]


def test_check_publishes_downstream_id_on_mismatch(cluster_dir, wire):
    """The todo's regression: on a mismatch the Rancher cluster bearing the name
    is NOT ours, so the id argocd consumes (`cluster_id`) must be the downstream
    agent's own id, not the foreign `c-abc12` a converge would refuse to stamp."""
    wire(
        FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
                   principals=PRINCIPALS),
        downstream_id="c-ours",
    )
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["id_match"] is False
    assert report["ok"] is False
    assert report["cluster_id"] == "c-ours"
    assert report["downstream_id"] == "c-ours"


def test_check_publishes_no_id_when_there_is_no_agent(cluster_dir, wire):
    """With no downstream agent there is no id a converge would attach to, so the
    report publishes none rather than the foreign Rancher cluster's id."""
    wire(
        FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
                   principals=PRINCIPALS),
        downstream_id="c-unrelated",
        agent_installed=False,
    )
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["agent_installed"] is False
    assert report["id_match"] is False
    assert report["cluster_id"] is None


def test_check_keeps_the_matching_rancher_id(cluster_dir, wire):
    """When the ids match the Rancher cluster is ours, so its id is published."""
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
                    principals=PRINCIPALS))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["id_match"] is True
    assert report["cluster_id"] == "c-abc12"


def test_status_reports_downstream_id_mismatch(cluster_dir, wire):
    """status exposes both ids and the id_match flag so an unrelated registration
    bearing the same name is visible instead of being reported as installed."""
    wire(
        FakeClient(bindings=[binding(ALICE, "cluster-owner")], principals=PRINCIPALS),
        downstream_id="c-unrelated",
    )
    report = _converge.status(Context(root=cluster_dir, cfg=None))
    assert report["registered"] is True
    assert report["cluster_id"] == "c-abc12"
    assert report["downstream_id"] == "c-unrelated"
    assert report["agent_installed"] is True
    assert report["id_match"] is False


def test_check_not_ok_when_unregistered(cluster_dir, wire):
    wire(FakeClient(cluster=None))
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["registered"] is False


def test_check_reports_orphaned_agent_when_no_rancher_cluster(cluster_dir, wire):
    """A downstream agent whose id matches no Rancher cluster bearing the name is
    an orphaned registration; check must report the downstream id and an orphan
    reason instead of a bare `registered: false` with no ids, so the operator
    sees converge refuses because the agent is stranded, not because there is no
    registration at all."""
    wire(FakeClient(cluster=None), downstream_id="c-old")
    report = _converge.check(Context(root=cluster_dir, cfg=None))
    assert report["ok"] is False
    assert report["registered"] is False
    assert report["downstream_id"] == "c-old"
    assert report["agent_installed"] is True
    assert report["id_match"] is False
    assert "no Rancher cluster named 'testcluster'" in report["orphan_reason"]
    assert "c-old" in report["orphan_reason"]


def test_status_reports_orphaned_agent_when_no_rancher_cluster(cluster_dir, wire):
    """status reports the downstream id and an orphan reason when the agent is
    registered but no Rancher cluster bears the configured name."""
    wire(FakeClient(cluster=None), downstream_id="c-old")
    report = _converge.status(Context(root=cluster_dir, cfg=None))
    assert report["registered"] is False
    assert report["downstream_id"] == "c-old"
    assert report["orphan_reason"]
    assert "c-old" in report["orphan_reason"]


def test_status_lists_members(cluster_dir, wire):
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner")], principals=PRINCIPALS))
    report = _converge.status(Context(root=cluster_dir, cfg=None))
    assert report["registered"] is True
    assert report["cluster_id"] == "c-abc12"
    assert report["members"] == [f"{ALICE} (cluster-owner)"]


def test_unresolvable_netid_is_a_failed_check(cluster_dir, wire):
    """A typo'd or unresolvable netid must read as a failed check, not a clean
    pass: skipping it would let a missing binding be ignored and an existing
    admin keep being reported ok while still unresolved."""
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
                    principals={"alice": ALICE}))  # carol is configured but unresolved
    with pytest.raises(RancherError, match="could not resolve Rancher principals.*carol"):
        _converge.check(Context(root=cluster_dir, cfg=None))


def test_check_reports_unresolved_existing_admin(cluster_dir, wire):
    """An admin whose principal search temporarily returns empty already holds a
    binding; the check must fail rather than report ok (an existing binding the
    search cannot reach must not be counted as resolved)."""
    wire(FakeClient(bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
                    principals={}))  # neither alice nor carol resolves now
    with pytest.raises(RancherError, match="could not resolve Rancher principals"):
        _converge.check(Context(root=cluster_dir, cfg=None))


def test_netid_listed_as_both_admin_and_user_is_rejected(cluster_dir, wire):
    """Membership under two tiers is ambiguous and would flap; refuse to load."""
    (cluster_dir / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "rancher": {"admins": ["alice", "bob"], "users": ["carol", "bob"]},
    }))
    with pytest.raises(ConfigError, match="'admins' and 'users'"):
        _converge.check(Context(root=cluster_dir, cfg=None))


def test_alias_that_resolves_to_same_principal_is_rejected(cluster_dir, wire):
    """`alice` (admin) and `alice@example.com` (user) pass the literal overlap
    check but strip to the same Rancher principal, which would flap; refuse it."""
    (cluster_dir / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "rancher": {"admins": ["alice"], "users": ["alice@example.com"]},
    }))
    wire(FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"),
                  binding(ALICE, "cluster-member", "b-2"), OWNER],
        principals={"alice": ALICE, "alice@example.com": ALICE},
    ))
    with pytest.raises(ConfigError, match="both resolve to the same Rancher principal"):
        _converge.check(Context(root=cluster_dir, cfg=None))

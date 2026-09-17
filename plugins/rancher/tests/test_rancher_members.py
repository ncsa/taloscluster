"""The rancher plugin's member reconciliation (`ensure_members`).

Rancher is faked at the Client boundary; these describe how converge converges
the cluster's member bindings and must refuse an ambiguous membership before it
mutates anything.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.context import Context
from taloscluster.errors import ConfigError

from taloscluster_rancher import reconcile
from taloscluster_rancher.client import MemberBinding
from taloscluster_rancher.config import Config
from taloscluster_rancher.errors import RancherError


class FakeClient:
    """Records the bindings `ensure_members` asks Rancher to change."""

    def __init__(self, bindings=(), principals=None):
        self._bindings = list(bindings)
        self._principals = principals if principals is not None else {}
        self.added = []
        self.removed = []

    def resolve_principal(self, netid):
        pid = self._principals.get(netid)
        return {"id": pid} if pid else None

    def list_member_bindings(self, cluster_id):
        return list(self._bindings)

    def add_member(self, cluster_id, principal_id, role):
        self.added.append((principal_id, role))

    def remove_member(self, binding_id):
        self.removed.append(binding_id)


def binding(pid, role, bid="b-1"):
    return MemberBinding(id=bid, userPrincipalId=pid, groupPrincipalId=None,
                         roleTemplateId=role)


ALICE, CAROL = "ldap_user://alice", "ldap_user://carol"
OWNER = binding("ldap_user://owner", "cluster-owner", bid="c-abc12:creator-cluster-owner")
PRINCIPALS = {"alice": ALICE, "carol": CAROL}


def _cfg(root, admins, users):
    (root / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "rancher": {"admins": admins, "users": users},
    }))
    (root / "secrets.yaml").write_text(yaml.safe_dump({
        "rancher": {"url": "https://rancher.example.com", "token": "token-x:y"},
    }))
    return Config.load(root)


def test_converges_new_members_and_prunes_stale(tmp_path):
    client = FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
        principals=PRINCIPALS,
    )
    cfg = _cfg(tmp_path, admins=["alice"], users=["carol"])

    members = reconcile.ensure_members(client, "c-abc12", cfg)

    assert client.added == [(CAROL, "cluster-member")]
    assert client.removed == []
    assert members == [{"netid": "alice", "role": "cluster-owner"},
                       {"netid": "carol", "role": "cluster-member"}]


def test_owner_binding_is_never_pruned(tmp_path):
    client = FakeClient(bindings=[OWNER], principals=PRINCIPALS)
    cfg = _cfg(tmp_path, admins=["alice"], users=["carol"])

    reconcile.ensure_members(client, "c-abc12", cfg)

    assert client.removed == []
    assert client.added == [(ALICE, "cluster-owner"), (CAROL, "cluster-member")]


def test_same_principal_under_two_tiers_is_rejected_before_mutation(tmp_path):
    """`alice` (admin) and `alice@example.com` (user) pass the literal overlap
    check but resolve_principal strips the email suffix, so both come back as the
    same principal; that is ambiguous and would flap between the two roles on
    alternating runs, so converge must refuse it before changing any binding."""
    client = FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"),
                  binding(CAROL, "cluster-member", "b-2"), OWNER],
        principals={"alice": ALICE, "alice@example.com": ALICE, "carol": CAROL},
    )
    cfg = _cfg(tmp_path, admins=["alice"], users=["alice@example.com"])

    with pytest.raises(ConfigError, match="both resolve to the same Rancher principal"):
        reconcile.ensure_members(client, "c-abc12", cfg)

    assert client.added == []
    assert client.removed == []


def test_unresolvable_existing_admin_is_not_deleted_as_stale(tmp_path):
    """A search that temporarily returns empty for an existing admin must abort
    before any binding mutation, so reconcile cannot delete that user's binding by
    treating it as stale: once gone, check would report ok even though the admin
    is still unresolved."""
    client = FakeClient(
        bindings=[binding(ALICE, "cluster-owner", "b-1"), OWNER],
        principals={},  # alice resolves to nothing right now
    )
    cfg = _cfg(tmp_path, admins=["alice"], users=["carol"])

    with pytest.raises(RancherError, match="could not resolve Rancher principals.*alice"):
        reconcile.ensure_members(client, "c-abc12", cfg)

    assert client.removed == []
    assert client.added == []


def test_unresolvable_never_created_member_aborts(tmp_path):
    """A configured member who was never created (search backs no result) must
    fail reconciliation rather than being silently skipped out of the desired
    set, so the missing admin is reported instead of a clean pass."""
    client = FakeClient(bindings=[OWNER], principals={})  # neither resolves
    cfg = _cfg(tmp_path, admins=["alice"], users=["carol"])

    with pytest.raises(RancherError, match="could not resolve Rancher principals"):
        reconcile.ensure_members(client, "c-abc12", cfg)

    assert client.removed == []
    assert client.added == []


def test_converge_refuses_when_downstream_id_matches_no_rancher_cluster(tmp_path, monkeypatch):
    """converge must refuse (not create a fresh import cluster) when the
    downstream agent id matches no Rancher cluster bearing the configured name --
    the renamed/deleted-in-UI case. If it created `c-new` under the agent's old
    `c-old`, members would bind to a cluster that stays pending and destroy would
    refuse c-old != c-new; converge must leave Rancher untouched instead."""
    class ConvergeClient(FakeClient):
        def __init__(self):
            super().__init__(principals=PRINCIPALS)
            self._cluster = None

        def ensure_cluster(self, name, downstream_id=None):
            # mirrors the real client: a registered agent that matches no named
            # cluster is refused before create_import_cluster is reached
            raise RancherError(
                f"the downstream cluster's cattle-cluster-agent is registered as "
                f"{downstream_id}, but no Rancher cluster named {name!r} exists"
            )

    client = ConvergeClient()
    monkeypatch.setattr(reconcile, "_client", lambda secrets: client)
    monkeypatch.setattr(reconcile, "downstream_rancher_id", lambda root: "c-old")
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "rancher": {"admins": ["alice"], "users": ["carol"]},
    }))
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({
        "rancher": {"url": "https://rancher.example.com", "token": "token-x:y"},
    }))

    with pytest.raises(RancherError, match="no Rancher cluster named 'testcluster'"):
        reconcile.converge(Context(root=tmp_path, cfg=None))

    assert client.added == []
    assert client.removed == []

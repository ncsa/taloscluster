"""Rancher client error-detail and principal-resolution behaviour.

`resolve_principal` sends a Rancher principal search and must return a principal
only when its id matches the requested netid exactly. A short or misspelled netid
that only matches a prefix or a different user must resolve to None rather than
granting `cluster-owner` to whoever the search returns first.

`_error_detail` turns a Rancher error response behind an HTTP error status into a
readable message: a string `message` is returned verbatim, a list of `errors`
dicts is joined by `; `, and a non-JSON body falls back to its raw text.
"""

from __future__ import annotations

import json

import pytest
import requests

from taloscluster_rancher.client import Client, _error_detail
from taloscluster_rancher.errors import RancherError


@pytest.fixture(autouse=True)
def _force_not_dry_run(monkeypatch):
    """Pin the real HTTP path: another test file leaves the global dry-run flag
    set, which would silently turn add/remove/delete into no-ops here."""
    from taloscluster_rancher import client as cmod

    monkeypatch.setattr(cmod, "dry_run", lambda: False)


def _client(principals_for):
    client = Client("https://rancher.example.com", "token-x:y")
    client._post = lambda path, body: {
        "data": principals_for.get(body["name"], []),
    }
    return client


USER = "ldap://uid={uid},ou=People,dc=example,dc=edu"
LOCAL = "local://{uid}"


def test_resolves_exactly_matching_principal_id():
    client = _client({"alice": [{"id": USER.format(uid="alice"), "principalType": "user"}]})
    assert client.resolve_principal("alice")["id"] == USER.format(uid="alice")


def test_resolves_an_exact_match_that_is_not_first_in_search_results():
    """A non-matching user earlier in the results must not win over an exact match."""
    client = _client(
        {
            "ali": [
                {"id": LOCAL.format(uid="alice"), "principalType": "user"},
                {"id": USER.format(uid="ali"), "principalType": "user"},
            ]
        }
    )
    assert client.resolve_principal("ali")["id"] == USER.format(uid="ali")


def test_exact_match_prefers_user_over_a_group_principal():
    client = _client(
        {
            "alice": [
                {"id": "ldap://cn=Team,ou=Groups,dc=example", "principalType": "group"},
                {"id": USER.format(uid="alice"), "principalType": "user"},
            ]
        }
    )
    assert client.resolve_principal("alice")["id"] == USER.format(uid="alice")


def test_prefix_match_is_rejected():
    """A netid that only prefixes a real user must not resolve to that user."""
    client = _client({"ali": [{"id": USER.format(uid="alice"), "principalType": "user"}]})
    assert client.resolve_principal("ali") is None


def test_no_match_returns_none():
    client = _client({})
    assert client.resolve_principal("nobody") is None


def test_misspelled_netid_returns_none():
    client = _client({"alice": [{"id": USER.format(uid="alice"), "principalType": "user"}]})
    assert client.resolve_principal("alicx") is None
    assert client.resolve_principal("alicee") is None


def _resp(payload=None, text=""):
    resp = requests.Response()
    resp._content = json.dumps(payload).encode() if payload is not None else text.encode()
    return resp


def test_error_detail_keeps_a_string_message_whole():
    """A string `message` must not be joined character by character."""
    assert _error_detail(_resp(payload={"message": "namespace not found"})) == "namespace not found"


def test_error_detail_joins_list_message_with_separators():
    assert _error_detail(_resp(payload={"message": ["a", "b", ""]})) == "a; b"


def test_error_detail_joins_errors_dicts():
    body = {"errors": [{"message": "first"}, {"message": "second"}]}
    assert _error_detail(_resp(payload=body)) == "first; second"


def test_error_detail_falls_back_to_raw_text_for_empty_body():
    assert _error_detail(_resp(payload={})) == "{}"
    assert _error_detail(_resp(text="boom")) == "boom"


def test_error_detail_non_json_body_returns_raw_text():
    assert _error_detail(_resp(text="<html>gateway error</html>")) == "<html>gateway error</html>"


# ---------------------------------------------------------------------------
# HTTP verbs that drive ensure_members / destroy
# ---------------------------------------------------------------------------

def _http_client(get=None, post=None, delete=None):
    client = Client("https://rancher.example.com", "token-x:y")
    if get is not None:
        client._get = get
    if post is not None:
        client._post = post
    if delete is not None:
        client._delete = delete
    return client


def test_resolve_principal_strips_an_email_suffix():
    client = _client({"alice": [{"id": USER.format(uid="alice"), "principalType": "user"}]})
    assert client.resolve_principal("alice@example.com")["id"] == USER.format(uid="alice")


def test_resolve_principal_skips_principals_without_an_id():
    client = _client(
        {
            "alice": [
                {"principalType": "user"},
                {"id": USER.format(uid="alice"), "principalType": "user"},
            ]
        }
    )
    assert client.resolve_principal("alice")["id"] == USER.format(uid="alice")


def test_find_cluster_queries_by_name_filter():
    seen = {}

    def fake_get(path, **kw):
        seen.update(kw)
        return {"data": [{"id": "c-1", "name": "example", "state": "active"}]}

    cluster = _http_client(get=fake_get).find_cluster("example")
    assert seen["params"] == {"filter": "name=example"}
    assert cluster.id == "c-1"
    assert cluster.state == "active"


def test_find_cluster_returns_none_when_absent():
    got = _http_client(get=lambda path, **kw: {"data": []}).find_cluster("example")
    assert got is None


def test_find_cluster_refuses_ambiguous_names():
    data = {
        "data": [
            {"id": "c-1", "name": "example", "state": "active"},
            {"id": "c-2", "name": "example", "state": "active"},
        ]
    }
    with pytest.raises(RancherError, match="multiple Rancher clusters named 'example'"):
        _http_client(get=lambda path, **kw: data).find_cluster("example")


def test_list_member_bindings_parses_rows():
    data = {
        "data": [
            {"id": "b-1", "userPrincipalId": "ldap://alice", "roleTemplateId": "cluster-owner"},
            {"id": "b-2", "groupPrincipalId": "ldap://team", "roleTemplateId": "cluster-member"},
            {"id": "b-3"},
        ]
    }
    bindings = _http_client(get=lambda path, **kw: data).list_member_bindings("c-1")
    assert bindings[0].id == "b-1"
    assert bindings[0].userPrincipalId == "ldap://alice"
    assert bindings[0].roleTemplateId == "cluster-owner"
    assert bindings[1].userPrincipalId is None and bindings[1].groupPrincipalId == "ldap://team"
    assert bindings[2].roleTemplateId == ""


def test_add_member_posts_the_binding():
    posted = {}
    client = _http_client(post=lambda path, body: posted.update({path: body}) or {})
    client.add_member("c-1", "ldap://alice", "cluster-owner")
    assert posted == {
        "/v3/clusterroletemplatebindings": {
            "clusterId": "c-1",
            "userPrincipalId": "ldap://alice",
            "roleTemplateId": "cluster-owner",
        }
    }


def test_remove_member_deletes_the_binding():
    deleted = []
    client = _http_client(delete=lambda path, **kw: deleted.append(path))
    client.remove_member("binding-9")
    assert deleted == ["/v3/clusterroletemplatebindings/binding-9"]


def test_delete_cluster_deletes_via_the_api():
    deleted = []
    client = _http_client(delete=lambda path, **kw: deleted.append(path))
    client.delete_cluster("c-7")
    assert deleted == ["/v3/clusters/c-7"]


def test_ensure_cluster_creates_a_fresh_import_when_no_downstream_id():
    """Without an agent (no downstream id) and no existing cluster, a fresh
    import cluster is created -- the normal first-registration path."""
    posted = []
    client = Client("https://rancher.example.com", "token-x:y")
    client._get = lambda path, **kw: {"data": []}
    client._post = lambda path, body: posted.append((path, body)) or {
        "id": "c-new", "name": "example", "state": "provisioning"
    }
    cluster = client.ensure_cluster("example", downstream_id=None)
    assert posted == [("/v3/clusters", {"type": "cluster", "name": "example"})]
    assert cluster.id == "c-new"


def test_ensure_cluster_reuses_the_cluster_matching_the_downstream_id():
    """A re-run on our own cluster: the existing Rancher id equals the downstream
    id, so it is reused without creating anything."""
    data = {"data": [{"id": "c-old", "name": "example", "state": "active"}]}
    client = _http_client(get=lambda path, **kw: data)
    client._post = lambda path, body: (_ for _ in ()).throw(
        AssertionError("must not create a cluster on a re-run")
    )
    cluster = client.ensure_cluster("example", downstream_id="c-old")
    assert cluster.id == "c-old"


def test_ensure_cluster_refuses_when_downstream_id_matches_no_named_cluster():
    """A registered agent (downstream id set) but no Rancher cluster bearing the
    configured name means the registration was renamed or deleted and recreated
    in the Rancher UI. Converge must refuse rather than create a fresh cluster
    that can never match the agent's existing id (which stranded the members on a
    cluster that stays pending and made destroy refuse c-old != c-new)."""
    post_hits = []
    client = _http_client(get=lambda path, **kw: {"data": []})
    client._post = lambda path, body: post_hits.append((path, body)) or {}
    with pytest.raises(
        RancherError,
        match="downstream cluster's cattle-cluster-agent is registered as c-old",
    ):
        client.ensure_cluster("example", downstream_id="c-old")
    assert post_hits == []


def test_ensure_cluster_orphan_message_directs_to_plugin_destroy():
    """Deleting the stale registration in Rancher will not clear the downstream
    agent, so the refuse message must direct the operator to the *plugin's*
    `destroy` -- not the top-level `taloscluster destroy`, which would tear the
    whole cluster down -- the only exit from the deadlock."""
    client = _http_client(get=lambda path, **kw: {"data": []})
    with pytest.raises(
        RancherError, match="run 'taloscluster plugin rancher destroy'"
    ):
        client.ensure_cluster("example", downstream_id="c-old")


def test_ensure_cluster_refuses_an_existing_cluster_whose_id_differs():
    """An existing Rancher cluster that shares the name but whose id does not
    match the downstream agent is an unrelated cluster; do not attach to it."""
    data = {"data": [{"id": "c-new", "name": "example", "state": "active"}]}
    client = _http_client(get=lambda path, **kw: data)
    with pytest.raises(RancherError, match="does not match the downstream"):
        client.ensure_cluster("example", downstream_id="c-old")


def _cluster_payload(conditions):
    return {"id": "c-1", "name": "example", "state": "pending", "conditions": conditions}


def _cluster_get(conditions):
    client = _http_client()
    client._get = lambda path, **kw: _cluster_payload(conditions)
    return client


def test_backing_namespace_not_ready_when_condition_status_false():
    conds = [{"type": "BackingNamespaceCreated", "status": "False", "reason": "CreationPending"}]
    assert _cluster_get(conds).get_cluster("c-1").namespace_ready is False


def test_backing_namespace_not_ready_when_condition_status_unknown():
    conds = [{"type": "BackingNamespaceCreated", "status": "Unknown"}]
    assert _cluster_get(conds).get_cluster("c-1").namespace_ready is False


def test_backing_namespace_not_ready_when_condition_absent_or_without_status():
    assert _cluster_get([]).get_cluster("c-1").namespace_ready is False
    conds = [{"type": "BackingNamespaceCreated"}]
    assert _cluster_get(conds).get_cluster("c-1").namespace_ready is False


def test_backing_namespace_ready_when_condition_status_true():
    conds = [{"type": "BackingNamespaceCreated", "status": "True"}]
    assert _cluster_get(conds).get_cluster("c-1").namespace_ready is True


def test_wait_for_namespace_waits_for_true_transition():
    calls = []

    def fake_get(path, **kw):
        calls.append(path)
        if len(calls) == 1:
            return _cluster_payload([{"type": "BackingNamespaceCreated", "status": "False"}])
        if len(calls) == 2:
            return _cluster_payload([{"type": "BackingNamespaceCreated", "status": "Unknown"}])
        return _cluster_payload([{"type": "BackingNamespaceCreated", "status": "True"}])

    client = _http_client()
    client._get = fake_get
    client.wait_for_namespace("c-1", timeout=5, poll=0)
    assert len(calls) == 3


def test_member_mutations_are_noops_in_dry_run(monkeypatch):
    from taloscluster_rancher import client as cmod

    monkeypatch.setattr(cmod, "dry_run", lambda: True)
    client = _http_client()
    hits: list[str] = []
    client._delete = lambda path, **kw: hits.append(path)
    client._post = lambda path, body: hits.append(path)

    client.add_member("c-1", "ldap://alice", "cluster-owner")
    client.remove_member("binding-9")
    client.delete_cluster("c-7")

    assert hits == []

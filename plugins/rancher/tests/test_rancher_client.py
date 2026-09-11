"""The rancher client's principal resolution requires an exact id match.

`resolve_principal` sends a Rancher principal search and must return a principal
only when its id matches the requested netid exactly. A short or misspelled netid
that only matches a prefix or a different user must resolve to None rather than
granting `cluster-owner` to whoever the search returns first.
"""

from __future__ import annotations

from taloscluster_rancher.client import Client


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

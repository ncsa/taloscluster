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

import requests

from taloscluster_rancher.client import Client, _error_detail


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

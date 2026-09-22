"""Best-effort upstream version lookups."""

from __future__ import annotations

import pytest
import requests

from taloscluster_charts import upstream


class FakeResponse:
    def __init__(self, payload, error=False):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise requests.HTTPError("boom")

    def json(self):
        return self._payload


def test_gateway_latest_version_parses_tag(monkeypatch):
    monkeypatch.setattr(
        upstream.requests, "get", lambda *a, **k: FakeResponse({"tag_name": "v1.7.0"})
    )
    assert upstream.gateway_latest_version() == "v1.7.0"


def test_gateway_latest_version_tolerates_errors(monkeypatch):
    monkeypatch.setattr(
        upstream.requests, "get", lambda *a, **k: FakeResponse({}, error=True)
    )
    assert upstream.gateway_latest_version() is None


def test_gateway_latest_version_tolerates_missing_tag(monkeypatch):
    monkeypatch.setattr(
        upstream.requests, "get", lambda *a, **k: FakeResponse({"message": "rate limited"})
    )
    assert upstream.gateway_latest_version() is None


def test_gateway_latest_version_tolerates_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(upstream.requests, "get", boom)
    assert upstream.gateway_latest_version() is None


@pytest.mark.parametrize("tag", [None, "", 0])
def test_gateway_latest_version_ignores_empty_tags(monkeypatch, tag):
    monkeypatch.setattr(upstream.requests, "get", lambda *a, **k: FakeResponse({"tag_name": tag}))
    assert upstream.gateway_latest_version() is None

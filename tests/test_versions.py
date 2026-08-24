"""Tests for taloscluster.versions and the `check` report built on top of it.

No network: the factory/dl.k8s.io lookups are monkeypatched, so what is under
test is the version parsing/comparison and the up-to-date verdict.
"""

from __future__ import annotations

import pytest

from taloscluster import versions

# A realistic factory /versions payload: sorted, with the next minor present
# only as pre-releases (which must never be reported as "latest").
FACTORY = [
    "v1.12.9", "v1.12.10", "v1.12.11",
    "v1.13.0", "v1.13.8", "v1.13.9",
    "v1.14.0-alpha.0", "v1.14.0-beta.1", "v1.14.0-rc.1",
]


@pytest.mark.parametrize("version,expected", [
    ("v1.13.8", (1, 13, 8)),
    ("1.13.8", (1, 13, 8)),
    ("v1.14.0-beta.1", (1, 14, 0)),
    ("v1.36", (1, 36)),
    ("garbage", ()),
])
def test_parse(version, expected):
    assert versions.parse(version) == expected


def test_is_stable():
    assert versions.is_stable("v1.13.9")
    assert not versions.is_stable("v1.14.0-rc.1")


def test_minor():
    assert versions.minor("v1.36.1") == "1.36"
    with pytest.raises(ValueError):
        versions.minor("v1")


def test_is_older():
    assert versions.is_older("v1.13.8", "v1.13.9")
    assert not versions.is_older("v1.13.9", "v1.13.8")
    assert not versions.is_older("v1.13.9", "v1.13.9")
    # a missing/unparseable side is never "older" -- unknown must not read as
    # "an update is available"
    assert not versions.is_older("", "v1.13.9")
    assert not versions.is_older("v1.13.9", "")


def test_latest_talos_skips_prereleases():
    assert versions.latest_talos(FACTORY) == "v1.13.9"


def test_latest_talos_patch_stays_on_the_minor():
    assert versions.latest_talos_patch("1.12", FACTORY) == "v1.12.11"
    assert versions.latest_talos_patch("1.13", FACTORY) == "v1.13.9"
    # a minor that only exists as pre-releases has no stable patch
    assert versions.latest_talos_patch("1.14", FACTORY) == ""
    assert versions.latest_talos_patch("1.99", FACTORY) == ""


def test_talos_versions_sorts_numerically(monkeypatch):
    """Ordering must be numeric, not lexicographic (v1.12.10 > v1.12.9)."""
    class FakeResp:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return ["v1.12.10", "v1.13.9", "v1.12.9"]

    monkeypatch.setattr(versions.requests, "get", lambda *a, **k: FakeResp())
    assert versions.talos_versions() == ["v1.12.9", "v1.12.10", "v1.13.9"]

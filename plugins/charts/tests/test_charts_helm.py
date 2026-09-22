"""helm list record parsing."""

from __future__ import annotations

from taloscluster_charts.helm import chart_version


def test_strips_release_prefix():
    assert chart_version({"chart": "metallb-0.16.1"}) == "0.16.1"
    assert chart_version({"chart": "traefik-41.6.0"}) == "41.6.0"
    assert chart_version({"chart": "sealed-secrets-2.20.0"}) == "2.20.0"


def test_keeps_v_prefixed_versions():
    assert chart_version({"chart": "cert-manager-v1.21.2"}) == "v1.21.2"
    assert chart_version({"chart": "cert-manager-1.21.2"}) == "1.21.2"


def test_falls_back_to_chart_field():
    assert chart_version({"chart": "weird"}) == "weird"
    assert chart_version({}) == ""

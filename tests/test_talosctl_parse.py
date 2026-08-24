"""Tests for the output parsers in taloscluster.talos.talosctl.

``server_version`` and ``node_image`` shell out via ``_run``; we monkeypatch
``_run`` so no ``talosctl`` binary is needed and assert the parsing logic.
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.talos import talosctl

# Realistic `talosctl version` output: a Client block and a Server block, each
# with its own Tag. The parser must return the SERVER tag, not the client one.
VERSION_OUTPUT = """\
Client:
    Tag: v1.8.0
    SHA: abcdef
    Built: 2024-01-01
Server:
    Tag: v1.8.3
    SHA: 123456
    Built: 2024-02-01
"""


def test_server_version_returns_server_tag(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: VERSION_OUTPUT)
    tag = talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert tag == "v1.8.3"


def test_server_version_empty_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "")
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_garbage_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "nonsense\nno tags here")
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_client_only_no_server_returns_empty(monkeypatch):
    """If no Server: block is present, there is no server tag."""
    out = "Client:\n    Tag: v1.8.0\n"
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


# ---------------------------------------------------------------------------
# node_image
# ---------------------------------------------------------------------------

def test_node_image_returns_full_ref_with_tag(monkeypatch):
    out = (
        "metadata:\n"
        "  namespace: config\n"
        "spec:\n"
        "  machine:\n"
        "    install:\n"
        "      image: factory.talos.dev/openstack-installer/abc123:v1.8.3\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    ref = talosctl.node_image(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert ref == "factory.talos.dev/openstack-installer/abc123:v1.8.3"


def test_node_image_unrelated_image_line_ignored(monkeypatch):
    """An image: line without 'installer' is not the install image."""
    out = (
        "spec:\n"
        "  some:\n"
        "    image: registry.example.com/nginx:latest\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    assert talosctl.node_image(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_node_image_empty_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "")
    assert talosctl.node_image(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


# A realistic `get members -o json` stream: separate JSON objects, NOT an array.
# controlplane-01 carries the shared kube-api VIP among its private ips, and the
# members differ in talos version (a rollout in flight).
MEMBERS_JSON = """\
{
    "metadata": {"id": "quad-controlplane-01"},
    "spec": {
        "hostname": "quad-controlplane-01",
        "machineType": "controlplane",
        "operatingSystem": "Talos (v1.13.9)",
        "addresses": ["100.64.0.68", "192.168.1.47", "192.168.3.34"]
    }
}
{
    "metadata": {"id": "quad-worker-01"},
    "spec": {
        "hostname": "quad-worker-01",
        "machineType": "worker",
        "operatingSystem": "Talos (v1.13.8)",
        "addresses": ["192.168.0.42", "100.64.0.70"]
    }
}
"""


def test_members_parses_version_and_prefers_tailscale(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, MEMBERS_JSON, ""))
    got = talosctl.members(Path("talosconfig"), "quad-controlplane-01")
    assert got["quad-controlplane-01"].version == "v1.13.9"
    assert got["quad-worker-01"].version == "v1.13.8"
    # the tailscale address, not the VIP-carrying private one and not addrs[0]
    assert got["quad-controlplane-01"].address == "100.64.0.68"
    assert got["quad-worker-01"].address == "100.64.0.70"


def test_member_addresses_still_returns_plain_addresses(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, MEMBERS_JSON, ""))
    assert talosctl.member_addresses(Path("talosconfig"), "e") == {
        "quad-controlplane-01": "100.64.0.68",
        "quad-worker-01": "100.64.0.70",
    }


def test_members_empty_when_discovery_is_unreachable(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, "", "no route"))
    assert talosctl.members(Path("talosconfig"), "e") == {}


def test_member_version_tolerates_an_odd_os_string():
    assert talosctl._member_version("Talos (v1.13.9)") == "v1.13.9"
    assert talosctl._member_version("Talos") == ""
    assert talosctl._member_version("") == ""

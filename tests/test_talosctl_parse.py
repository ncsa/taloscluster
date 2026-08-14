"""Tests for the output parsers in clusterctl.talos.talosctl.

``server_version`` and ``node_image`` shell out via ``_run``; we monkeypatch
``_run`` so no ``talosctl`` binary is needed and assert the parsing logic.
"""

from __future__ import annotations

from pathlib import Path

from clusterctl.talos import talosctl

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

"""Pin the 2026-09-07 doc/docstring-drift fixes.

A mix of prose, docstrings and CLI messages drifted from the code over time.
These tests keep the surviving wording honest so the drift does not resurface:

- `sdn.exit_nodes` defaults to every cluster node, offline included.
- The managed-SDN docs example uses a placeholder, not the real host `phoenix`.
- The VIP move is not claimed to happen "without a reboot".
- The scaffold comment no longer says every node gets `ncsa/project`.
- The reachability-timeout message points at the troubleshooting guide, not the
  removed README section.
- Module docstrings no longer lean on the removed terraform / shell / yq tooling
  or an OpenStack-only connection.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TALOSCLUSTER = ROOT / "taloscluster"
DOCS = ROOT / "docs"


def test_proxmox_sdn_docstring_includes_offline_nodes():
    src = (TALOSCLUSTER / "config.py").read_text()
    assert "every cluster node, offline included" in src
    assert "every online cluster node" not in src
    assert "every online node" not in src


def test_exit_nodes_docs_default_includes_offline_nodes():
    text = (DOCS / "configuration" / "proxmox.md").read_text()
    assert "every cluster node, offline included" in text
    assert "every online node" not in text


def test_managed_sdn_example_uses_a_placeholder_not_phoenix():
    text = (DOCS / "providers" / "proxmox.md").read_text()
    assert "name: mycl" in text
    assert "name: phoenix" not in text


def test_vip_move_is_not_claimed_to_avoid_a_reboot():
    for path in (
        DOCS / "providers" / "proxmox.md",
        DOCS / "configuration" / "proxmox.md",
        DOCS / "usage.md",
    ):
        text = path.read_text()
        assert "without a reboot" not in text
        assert (
            "not guaranteed to avoid a restart" in text
            or "may or may not settle without a restart" in text
        )


def test_reachability_timeout_points_at_troubleshooting():
    src = (TALOSCLUSTER / "converge.py").read_text()
    assert "docs/troubleshooting.md" in src
    assert "README: headscale hygiene" not in src


def test_module_docstrings_drop_removed_tooling_and_openstack_only_claims():
    for name in (
        "config.py",
        "state.py",
        "cli.py",
        "naming.py",
        "errors.py",
        "output.py",
        "converge.py",
        "talos/talosctl.py",
        "talos/machineconfig.py",
        "talos/factory.py",
        "openstack/compute.py",
        "openstack/session.py",
    ):
        text = (TALOSCLUSTER / name).read_text()
        assert "terraform" not in text
        assert "README-python.md" not in text
        assert "bin/cluster.sh" not in text
        assert "cluster.sh" not in text
        # no lingering "python rewrite ... on OpenStack" / yq framing
        assert "pure-Python alternative to the terraform" not in text


def test_package_docstring_describes_both_providers():
    text = (TALOSCLUSTER / "__init__.py").read_text()
    assert "OpenStack or Proxmox" in text
    assert "See README-python.md" not in text

"""Keep the machine-access guide published and its verify commands correct.

The guide documents the two management access paths end to end. Its prose may
be reworded freely; what must not drift is that it stays published and that
every verify ``talosctl`` command it hands the operator points at the generated
``./talosconfig`` rather than the environment or home config.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "concepts" / "machines.md"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = ROOT / "mkdocs.yml"
    assert "concepts/machines.md" in nav.read_text()


def test_guide_verify_commands_use_the_generated_talosconfig():
    # Both paths' verify `talosctl ... version` commands must point at the
    # generated `./talosconfig`, otherwise talosctl falls back to the
    # environment or home config and may reach nothing. Pin each one.
    text = GUIDE.read_text()
    assert "talosctl --talosconfig talosconfig -n" in text
    assert "talosctl --talosconfig talosconfig -n mycluster-controlplane-01 version" in text
    assert "talosctl --talosconfig talosconfig -n 192.0.2.11 version" in text


def test_guide_verify_commands_have_no_bare_talosctl():
    # A `talosctl -n ... version` without `--talosconfig` defaults to the
    # environment or home config rather than `./talosconfig`, so the guide must
    # never hand the operator a bare talosctl verify command.
    for line in GUIDE.read_text().splitlines():
        if "version" in line and "talosctl -n" in line:
            assert "--talosconfig talosconfig" in line

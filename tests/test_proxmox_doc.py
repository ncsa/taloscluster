"""Keep the Proxmox 9 requirement's documentation honest about `destroy`.

The supported-release check runs inside the inventory load every provider
command performs, so it refuses `destroy` as much as converge: an operator
who upgraded taloscluster cannot tear down a Proxmox 8 cluster with it, and
only the error's advice to pin 0.7.x gets them out. The setup guide and the
``proxmox.url`` reference must both say so, matching the text the refusal
actually prints, so neither page can drift back to a converge-only story.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "docs" / "providers" / "proxmox.md"
REFERENCE = ROOT / "docs" / "configuration" / "proxmox.md"
BACKEND = ROOT / "taloscluster" / "proxmox" / "backend.py"

PIN = "pin taloscluster to 0.7.x"


def test_the_setup_guide_covers_the_destroy_refusal():
    text = PROVIDER.read_text()
    # the refusal is not converge's alone: destroy loads the same inventory
    assert "and so does `destroy`" in text
    # the pin is the escape hatch for a Proxmox 8 cluster, teardown included
    assert "Stay on taloscluster 0.7.x" in text
    assert "refuses `destroy` on it too" in text


def test_the_reference_covers_the_destroy_refusal():
    text = REFERENCE.read_text()
    assert "`destroy` is refused below Proxmox 9 the same way converge is" in text
    assert "Tear down a Proxmox 8 cluster with taloscluster 0.7.x" in text


def test_the_pin_guidance_matches_the_refusal_error():
    # the pages name the same escape hatch the error message prints
    assert PIN in BACKEND.read_text()

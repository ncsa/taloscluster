"""Keep the metal provider's docs published, cross-linked and quoting its code.

The metal provider is documented across the setup guide, the key reference and
the troubleshooting entry for BMCs that cannot boot remote media. The pages'
prose may be reworded freely; what must not drift is that they stay published
and cross-linked, and that the troubleshooting entry quotes the messages the
``metal`` commands actually print.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "docs" / "providers" / "metal.md"
REFERENCE = ROOT / "docs" / "configuration" / "metal.md"
GUIDE = ROOT / "docs" / "troubleshooting.md"
NAV = ROOT / "mkdocs.yml"
JOINED = ROOT / "taloscluster" / "metal" / "commands.py"


def test_provider_page_is_published_and_cross_linked():
    assert "providers/metal.md" in NAV.read_text()
    text = PROVIDER.read_text()
    # the guide hands the keys to the reference, the syntax to the command
    # reference, and the flow to the machines page
    assert "../configuration/metal.md" in text
    assert "../commands.md#metal" in text
    assert "../concepts/machines.md#metal" in text


def test_reference_points_back_at_the_setup_guide():
    text = REFERENCE.read_text()
    assert "../providers/metal.md" in text


def test_troubleshooting_entry_quotes_the_join_diagnostics():
    # The entry's symptom is the wait step timing out; quote the message the
    # wait command actually raises, and the redfish-disabled notice the join
    # prints for a machine the operator boots by hand.
    guide = GUIDE.read_text()
    code = JOINED.read_text()
    assert "did not answer the maintenance apid on" in guide
    assert "did not answer the maintenance apid on" in code
    assert "boot the machine into maintenance mode yourself" in guide
    assert "boot the machine into maintenance mode yourself" in code

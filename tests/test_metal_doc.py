"""Keep the metal provider's documentation in lockstep with the join flow.

The metal provider is documented across four kinds of pages: the setup guide
under ``docs/providers/``, the key reference under ``docs/configuration/``, the
join-flow coverage in the machines and lifecycle concepts pages, and the
troubleshooting entry for BMCs that cannot boot remote media. These tests pin
the pages to the behavior they describe: the published nav entry, the quoted
diagnostics against the strings the ``metal`` commands actually print, the
cross-links that carry a reader from one page to the next, and the two join
paths — an explicit ``metal join`` and converge's ``auto_join`` phase — named
together on every page that tells the operator how a machine joins.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "docs" / "providers" / "metal.md"
REFERENCE = ROOT / "docs" / "configuration" / "metal.md"
MACHINES = ROOT / "docs" / "concepts" / "machines.md"
LIFECYCLE = ROOT / "docs" / "concepts" / "lifecycle.md"
USAGE = ROOT / "docs" / "usage.md"
COMMANDS = ROOT / "docs" / "commands.md"
GUIDE = ROOT / "docs" / "troubleshooting.md"
NAV = ROOT / "mkdocs.yml"
JOINED = ROOT / "taloscluster" / "metal" / "commands.py"


def test_provider_page_is_published_and_cross_linked():
    assert "providers/metal.md" in NAV.read_text()
    text = PROVIDER.read_text()
    assert text.startswith("# Metal setup")
    # the guide hands the keys to the reference, the syntax to the command
    # reference, and the flow to the machines page
    assert "../configuration/metal.md" in text
    assert "../commands.md#metal" in text
    assert "../concepts/machines.md#metal" in text
    # the BMC is only ever asked for media, a one-time boot and power
    assert "one-time boot" in text
    assert "no internet egress" in text
    # hardware- and site-specific observations stay with the cluster
    assert "cluster's own notes" in text


def test_provider_page_documents_the_dev_build_reinstall():
    # Machines joined with an early 0.8.0 development build carry the guest
    # agent the metal images have since dropped; the accepted resolution of the
    # one-off reinstall on the first converge is documented, not coded around.
    text = PROVIDER.read_text()
    assert "0.8.0 development build" in text
    assert "reinstalled once" in text


def test_reference_points_back_at_the_setup_guide():
    text = REFERENCE.read_text()
    assert "../providers/metal.md" in text


def test_reference_lists_every_setting_a_server_replaces():
    # The `metal.<group>` prose splits the group settings into the plain ones a
    # server replaces wholesale and the ones that merge (`bmc` key by key,
    # `interfaces` per interface). The replaced list must carry every scalar
    # setting, `boot_timeout` among them, and none of the merging keys.
    listed = re.search(
        r"plain settings \(([^)]*)\) are replaced when the server sets one",
        REFERENCE.read_text(),
    ).group(1)
    names = {chunk.strip().strip("`") for chunk in listed.split(",")}
    assert names == {"role", "redfish", "disk", "network", "boot_timeout"}


def test_machines_page_describes_the_join_flow():
    text = MACHINES.read_text()
    assert "## Metal" in text
    # converge does not create bare metal; the join flow brings it in
    assert "Converge does not create bare-metal machines" in text
    assert "taloscluster metal join SERVER" in text
    assert "no BIOS boot-mode changes and no boot-order manipulation" in text
    # a redfish-off machine is booted by the operator; join waits/applies/verifies
    assert "wait, apply and verify" in text
    assert "../providers/metal.md" in text
    # the access paths cover bare metal too
    assert "static address of their cluster link" in text


def test_lifecycle_build_covers_the_join():
    text = LIFECYCLE.read_text()
    assert "are not created by converge" in text
    assert "taloscluster metal join SERVER" in text
    assert "redfish: false" in text
    assert "../providers/metal.md" in text


def test_join_pages_name_both_join_paths():
    # A machine joins either through an explicit `metal join` or through
    # converge's compute phase when its group opts in with `auto_join`; every
    # page that tells the operator how a machine joins names both, so none of
    # them drifts back into presenting `metal join` as the only path.
    for page in (MACHINES, USAGE, COMMANDS, PROVIDER):
        text = page.read_text()
        assert "metal join" in text
        assert "auto_join" in text, (
            f"{page.relative_to(ROOT)} presents `metal join` as the only join path"
        )


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
    # the recovery names the redfish switch, the PXE boot-from-disk duty and
    # where site-specific observations belong
    assert "redfish: false" in guide
    assert "PXE" in guide
    assert "boot from disk afterwards" in guide
    assert "cluster's own notes" in guide
    assert "providers/metal.md" in guide

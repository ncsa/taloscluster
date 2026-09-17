"""Keep the Day 2 operate guide's sections coherent after the docs reintegration.

The Day 2 text mixed two unrelated concerns into single paragraphs: worker-cleanup
scale-down was prepended to the control-plane quorum paragraph, and the
unknown-version rule was tacked onto the scale-up/upgrade paragraph. These tests
pin the four concerns (scale-down, control-plane rollouts, config applies,
versions) to their own subsections, each short and self-contained.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "concepts" / "lifecycle.md"


def test_day2_has_four_short_subsections():
    # Scale-down, control-plane rollouts, config applies and versions each get
    # their own `###` subsection under `## Day 2: operate`.
    text = GUIDE.read_text()
    day2 = text.split("## Day 2: operate", 1)[1]
    headings = re.findall(r"^### ([^\n]+)$", day2, re.M)
    assert headings == [
        "Scaling down",
        "Control-plane rollouts",
        "Config applies",
        "Versions",
    ]


def test_worker_cleanup_is_separate_from_control_plane_quorum():
    # Worker cleanup (owned-inventory reconciliation) is its own paragraph, so it
    # is no longer folded into the etcd-quorum control-plane scale-down text.
    text = GUIDE.read_text()
    assert "reconciled from the provider's owned inventory" in text
    assert "Scaling control planes down is quorum-safe" in text
    owned = text.index("reconciled from the provider's owned inventory")
    quorum = text.index("Scaling control planes down is quorum-safe")
    # The two sentences are now separated by a blank line (distinct paragraphs).
    assert quorum > owned
    assert "\n\n" in text[owned:quorum]


def test_unknown_version_rule_is_not_appended_to_scale_up():
    # The unknown-version refusal is a standalone paragraph in the Versions
    # section, not tacked onto the end of the scale-up/upgrade paragraph.
    text = GUIDE.read_text()
    assert "boots at the upgraded version" in text
    assert "refuses to generate or apply any machine configs" in text
    scale_up = text.index("boots at the upgraded version")
    unknown = text.index("refuses to generate or apply any machine configs")
    assert unknown > scale_up
    assert "\n\n" in text[scale_up:unknown]

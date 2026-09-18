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


def test_day2_covers_all_four_concerns_in_their_own_subsections():
    # Scale-down, control-plane rollouts, config applies and versions each get
    # their own `###` subsection under `## Day 2: operate`. Match on a keyword
    # per concern rather than the exact ordered heading list, so a reworded or
    # reordered heading does not spuriously fail while a concern folded back
    # into an unrelated subsection (or dropped) still does.
    text = GUIDE.read_text()
    day2 = text.split("## Day 2: operate", 1)[1]
    headings = re.findall(r"^### ([^\n]+)$", day2, re.M)
    assert len(headings) >= 4
    joined = "\n".join(headings).lower()
    for concern in ("scaling down", "control-plane rollouts", "config applies", "versions"):
        assert concern in joined


def test_worker_cleanup_is_separate_from_control_plane_quorum():
    # Worker cleanup (owned-inventory reconciliation) is its own paragraph, so it
    # is no longer folded into the etcd-quorum control-plane scale-down text.
    # Anchor on the concept words rather than the full sentences so a reword
    # does not fail the check while the two ideas being merged still does.
    text = GUIDE.read_text()
    assert "owned inventory" in text
    assert "quorum-safe" in text
    owned = text.index("owned inventory")
    quorum = text.index("quorum-safe")
    # The two topics are now separated by a blank line (distinct paragraphs).
    assert quorum > owned
    assert "\n\n" in text[owned:quorum]


def test_unknown_version_rule_is_not_appended_to_scale_up():
    # The unknown-version refusal is a standalone paragraph in the Versions
    # section, not tacked onto the end of the scale-up/upgrade paragraph. Anchor
    # on the section heading and the refusal phrase so the refusal must open a
    # paragraph of its own past the scale-up paragraph, while a reword of the
    # surrounding scale-up prose does not fail the check.
    text = GUIDE.read_text()
    refusal = "unknown version"
    assert refusal in text
    section = text.split("### Versions", 1)[1]
    paragraphs = [p for p in section.split("\n\n") if p.strip()]
    # the refusal is its own paragraph, not folded into the scale-up paragraph
    assert any(refusal in p for p in paragraphs[1:])
    assert not any(refusal in p for p in paragraphs[:1])

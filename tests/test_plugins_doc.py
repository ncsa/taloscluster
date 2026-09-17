"""Keep the plugins guide from restating the results-forwarding rule twice.

The rule that whatever a plugin's `converge`/`check`/`status` returns is stored
for the plugins that follow is documented in detail once, in the `ctx.results`
paragraph of "Writing a plugin". The "Plugins depend on each other" section must
point at that section rather than restate the whole rule.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "concepts" / "plugins.md"


def test_results_forwarding_is_stated_once_in_detail():
    # The `ctx.results[<name>]` storage detail appears exactly once -- the full
    # explanation lives in the "Writing a plugin" section.
    text = GUIDE.read_text()
    assert text.count("ctx.results[<name>]") == 1
    assert text.count("is stored for the plugins that follow") == 1


def test_dependency_section_points_at_writing_a_plugin():
    # "Plugins depend on each other" forwards to the authoritative section instead
    # of restating the mechanism.
    text = GUIDE.read_text()
    current = text.split("## Plugins depend on each other", 1)[1]
    current = current.split("\n## ", 1)[0]
    assert "Writing a plugin" in current
    assert "#writing-a-plugin" in text

"""Keep the plugins guide from restating the results-forwarding rule twice.

The rule that whatever a plugin's `converge`/`check`/`status` returns is stored
for the plugins that follow is documented in detail once, in the `ctx.results`
paragraph of "Writing a plugin". The "Plugins depend on each other" section must
point at that section rather than restate the whole rule. The coverage checks
keep every bundled plugin visible on the page and on the landing pages: the
charts plugin once shipped in the intro table without a section of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "concepts" / "plugins.md"
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.md"

#: the bundled plugins and the `##` section each gets on the concepts page
SECTIONS = {"rancher": "Rancher", "argocd": "ArgoCD", "charts": "Charts"}


def test_results_forwarding_is_documented_in_writing_a_plugin():
    # The full `ctx.results[<name>]` storage detail lives in the "Writing a
    # plugin" section, the authoritative one that `test_dependency_section_*`
    # forwards readers to. Check presence there rather than a page-wide count,
    # so a rewording of the rule does not spuriously fail.
    text = GUIDE.read_text()
    writing = text.split("## Writing a plugin", 1)[1]
    assert "ctx.results[<name>]" in writing


def test_dependency_section_points_at_writing_a_plugin():
    # "Plugins depend on each other" forwards to the authoritative section instead
    # of restating the mechanism.
    text = GUIDE.read_text()
    current = text.split("## Plugins depend on each other", 1)[1]
    current = current.split("\n## ", 1)[0]
    assert "Writing a plugin" in current
    assert "#writing-a-plugin" in text


def test_every_listed_plugin_has_its_own_section():
    # Regression: the charts plugin shipped in the intro table without a
    # `## Charts` section, so the page documented Rancher and ArgoCD in depth
    # and left the third bundled plugin to the configuration page alone. Every
    # plugin the table lists must have a matching section on the page.
    text = GUIDE.read_text()
    intro = text.split("\n## ", 1)[0]
    listed = re.findall(r"^\| `([a-z0-9-]+)` \|", intro, re.M)
    assert listed, "plugin table not found on the concepts page"
    for name in listed:
        section = SECTIONS.get(name, name.capitalize())
        assert f"\n## {section}\n" in text, f"plugin {name} has no ## {section} section"


def test_readme_and_index_advertise_every_bundled_plugin():
    # The landing pages name the plugins; a bundled plugin they skip is
    # invisible to a reader who never opens the concepts page.
    for path in (README, INDEX):
        text = path.read_text().lower()
        for name in SECTIONS:
            assert name in text, f"{path.relative_to(ROOT)} does not mention the {name} plugin"

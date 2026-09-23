"""Guards for the bundled plugins' packaging and READMEs.

The plugins ship their own pyproject and README beside the core package, and
both classes of file have drifted silently before: the pyprojects stayed at
``0.1.0`` from v0.7.0 while the plugins kept changing, and all three depended
on an unpinned ``taloscluster`` even though the charts plugin imports
``taloscluster.output.show_yaml``, an API 0.7.0 does not ship. The READMEs
meanwhile accumulated claims the code had outgrown -- an unresolvable Rancher
member "skipped with a warning" where converge now refuses, advice to edit
``client.py`` to turn TLS verification off when no such option exists, the
refused ``proxmox.network.external.ingress_pool`` key, a mangled sentence
around the ``charts.ceph`` secrets path -- and were hard-wrapped at ~80
columns against the repository's one-line-per-paragraph convention.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ("argocd", "charts", "rancher")

#: plugin name -> the configuration page that documents its section
CONFIGURATION_DOC = {
    "argocd": "docs/configuration/argocd.md",
    "charts": "docs/configuration/charts.md",
    "rancher": "docs/configuration/rancher.md",
}

#: the first taloscluster release shipping every API a plugin imports (charts
#: needs taloscluster.output.show_yaml, absent from 0.7.0), so an older core
#: cannot satisfy an installed plugin
MINIMUM_CORE = (0, 8, 0)

#: phrases whose claims the code no longer backs; each names a behaviour that
#: changed, not wording, so the README cannot quietly re-assert them
STALE_CLAIMS = {
    "rancher/README.md": (
        # members that cannot be resolved fail the run now, they are not skipped
        "skipped with a warning",
        # user docs must not send readers into package internals to flip TLS
        # verification; there is no option to turn it off
        "client.py",
    ),
    "charts/README.md": (
        # the old key path is refused since the network settings moved
        "proxmox.network.external.ingress_pool",
    ),
}


def _pyproject(name: str) -> str:
    return (ROOT / "plugins" / name / "pyproject.toml").read_text()


def _readme(name: str) -> str:
    return (ROOT / "plugins" / name / "README.md").read_text()


def _version(text: str) -> tuple[int, ...]:
    match = re.search(r'^version = "([\d.]+)"$', text, re.MULTILINE)
    assert match, "pyproject.toml has no version"
    return tuple(int(part) for part in match.group(1).split("."))


def _taloscluster_floor(text: str) -> tuple[int, ...]:
    """The minimum taloscluster the pyproject's dependency declares."""
    match = re.search(r'"taloscluster(?:>=([\d.]+))?"', text)
    assert match, "pyproject.toml does not depend on taloscluster"
    if match.group(1) is None:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


@pytest.mark.parametrize("name", PLUGINS)
def test_plugin_version_tracks_the_release_that_changed_it(name):
    # a plugin version is what a user pins against; leaving it at the value it
    # had at v0.7.0 hides substantial plugin changes from every diff since
    assert _version(_pyproject(name)) >= MINIMUM_CORE


@pytest.mark.parametrize("name", PLUGINS)
def test_plugin_requires_the_core_release_its_imports_need(name):
    # an unpinned dependency installs happily against a taloscluster too old
    # for it (charts imports output.show_yaml, added after 0.7.0) and fails at
    # run time instead of resolve time
    assert _taloscluster_floor(_pyproject(name)) >= MINIMUM_CORE


@pytest.mark.parametrize("name", PLUGINS)
def test_readme_points_at_the_configuration_page_that_documents_the_section(name):
    # the READMEs sketch the section; the per-key reference lives in the
    # published configuration docs, so the link must exist and resolve
    readme = _readme(name)
    doc = CONFIGURATION_DOC[name]
    assert f"({doc})" in readme or f"docs/configuration/{name}.md" in readme
    assert (ROOT / doc).is_file()


@pytest.mark.parametrize("name", PLUGINS)
def test_readme_is_one_line_per_paragraph(name):
    # prose is unwrapped (one paragraph, one line) like the rest of the
    # repository's docs; a hard-wrapped paragraph leaves lines mid-sentence
    in_fence = False
    offenders: list[str] = []
    for line in _readme(name).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        text = line.strip()
        if not text or text.startswith(("#", "-", "*", ">")) or re.match(r"^\d+\.", text):
            continue
        if not text.endswith((".", ":", "!", "?", ")", "]")):
            offenders.append(line)
    assert not offenders, f"hard-wrapped prose in plugins/{name}/README.md: {offenders}"


@pytest.mark.parametrize("path, claims", sorted(STALE_CLAIMS.items()))
def test_readme_does_not_reassert_claims_the_code_outgrew(path, claims):
    text = (ROOT / "plugins" / path).read_text()
    back = [claim for claim in claims if claim in text]
    assert not back, f"stale claim(s) back in plugins/{path}: {back}"


def test_charts_docstring_names_the_current_ingress_pool_key():
    # the metallb pool is computed from network.external.ingress_pool; the
    # proxmox-prefixed path is the refused pre-0.8 spelling
    text = (ROOT / "plugins" / "charts" / "taloscluster_charts" / "charts.py").read_text()
    assert "proxmox.network.external.ingress_pool" not in text
    assert "network.external.ingress_pool" in text


def test_rancher_readme_treats_the_section_as_merged_configuration():
    # the members and the credentials may live anywhere in the merged
    # configuration (cluster.yaml, secrets.yaml or an included file); a README
    # that ties each value to one file contradicts the loader
    text = _readme("rancher")
    assert "include" in text
    assert "included file" in text

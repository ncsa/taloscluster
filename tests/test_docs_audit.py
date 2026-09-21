"""Keep the docs' own audit checks maintained.

The 2026-09-07 documentation audit leaned on three ad-hoc checks that keep the
published site honest: a complete documented cluster example must actually load
with a matching secrets file, every internal link and header anchor must resolve
to a real target, and the command reference must cover every command the CLI
registers. These tests turn those one-off audit checks into permanent regression
tests so the drift they caught cannot come back silently:

- The complete ``cluster.yaml`` + ``secrets.yaml`` examples in
  ``docs/configuration.md`` must load through the real ``load_config`` /
  ``load_config`` once the ``CHANGE-ME`` scaffold placeholders are replaced with
  real credential strings.
- The ``metal:`` example on the metal configuration page must load the same
  way -- its cluster.yaml block and the matching ``secrets.yaml`` block for the
  group's BMC credentials, dropped into a provider-managed cluster -- and the
  metal pages plus the ``init --metal`` scaffold must name placeholder machines
  and networks, never the real site's hostnames and addresses.
- Every ``[text](path.md#anchor)`` / ``[text](path.md)`` / ``[text](#anchor)``
  link across ``docs/`` must point at an existing markdown file and, when an
  anchor is given, at a header whose MkDocs slug matches.
- Every subcommand the CLI registers (including its ``sync``/``apply`` aliases)
  must have a matching section in ``docs/commands.md``.
- Every absolute GitHub Pages link in ``README.md`` must resolve to a page the
  current ``mkdocs.yml`` nav publishes (and a fragment to that page's heading),
  and the established ``concepts/`` URLs must survive the documentation
  reorganization rather than silently moving and breaking old links.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from markdown.extensions.toc import slugify_unicode

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE, load_config

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
COMMANDS = DOCS / "commands.md"
CONFIGURATION = DOCS / "configuration.md"
METAL = DOCS / "configuration" / "metal.md"
SCAFFOLD = ROOT / "taloscluster" / "scaffold.py"
CLI = ROOT / "taloscluster" / "cli.py"
README = ROOT / "README.md"
MKDOCS = ROOT / "mkdocs.yml"
SITE_URL = "https://ncsa.github.io/taloscluster/"

SCAFFOLD_PLACEHOLDER = "CHANGE-ME"


def _yaml_blocks(text: str) -> list[str]:
    """Return the bodies of every fenced ```yaml ... ``` block in ``text``."""
    return re.findall(r"```yaml\n(.*?)```", text, re.S)


def _headers(path: Path) -> set[str]:
    """MkDocs slugs for every heading in ``path``."""
    slugs: set[str] = set()
    for line in path.read_text().splitlines():
        if re.match(r"^#{1,6} ", line):
            header = re.sub(r"^#{1,6}\s*", "", line).strip()
            slugs.add(slugify_unicode(header, "-"))
    return slugs


def _internal_link_targets(path: Path) -> list[tuple[str, str | None]]:
    """(target-path, anchor|None) for every internal markdown link in ``path``."""
    found: list[tuple[str, str | None]] = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:", "//")):
            continue
        target = target.split(' "', 1)[0].strip()
        if "&lt;" in target or "<" in target:
            continue
        if "#" in target:
            filepart, anchor = target.rsplit("#", 1)
        else:
            filepart, anchor = target, None
        filepart = filepart.strip()
        if not filepart:
            # same-file anchor
            found.append(("", anchor))
            continue
        if not filepart.endswith(".md"):
            continue
        found.append((filepart, anchor))
    return found


# --------------------------------------------------------------------------- #
# 1. The complete documented example must load with matching secrets.
# --------------------------------------------------------------------------- #


def _replace_scaffold(value):
    """Recursively swap ``CHANGE-ME`` scaffold placeholders for real strings."""
    if isinstance(value, dict):
        return {k: _replace_scaffold(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_scaffold(v) for v in value]
    if value == SCAFFOLD_PLACEHOLDER:
        return "a-real-placeholder-credential"
    return value


def test_complete_documented_example_loads(tmp_path):
    # The full worked example on the configuration overview page must stay a
    # complete, loadable cluster: one `cluster.yaml` block that carries every
    # required key and one `secrets.yaml` block for the same provider. The
    # examples use `CHANGE-ME` scaffold placeholders (which the loader
    # deliberately refuses), so substitute real credential strings first, then
    # run the real loader -- a doc example that drifts out of the schema would
    # fail here.
    blocks = _yaml_blocks(CONFIGURATION.read_text())
    cluster_blocks = [b for b in blocks if "controlplane:" in b]
    secrets_blocks = [b for b in blocks if "token_id:" in b or "credential_id:" in b]

    assert cluster_blocks, "configuration.md must keep a complete cluster.yaml example"
    assert secrets_blocks, "configuration.md must keep a matching secrets.yaml example"

    cluster = yaml.safe_load(cluster_blocks[0])
    secrets = yaml.safe_load(secrets_blocks[0])

    (tmp_path / CLUSTER_FILE).write_text(yaml.safe_dump(cluster))
    (tmp_path / SECRETS_FILE).write_text(yaml.safe_dump(_replace_scaffold(secrets)))

    # secrets.yaml is merged in, so one load covers both documented blocks
    cfg = load_config(tmp_path)
    assert cfg.name
    assert cfg.provider_name in ("openstack", "proxmox")
    assert all(cfg.provider.credentials())


def test_metal_documented_example_loads(tmp_path):
    # The `metal:` example on the metal configuration page must stay loadable:
    # a cluster.yaml block carrying the group, its cabling plan and its servers,
    # plus a matching secrets.yaml block carrying the group's BMC credentials
    # (the loader refuses a `redfish` group whose credentials are still the
    # `CHANGE-ME` scaffold placeholders, so substitute a real one first). The
    # example group sits on another L2, which loads only with the KubeSpan
    # opt-in, and cables an `external` link, which needs a `network.external`
    # block -- so drop both blocks into a minimal provider-managed cluster that
    # supplies those, then run the real loader.
    blocks = _yaml_blocks(METAL.read_text())
    cluster_blocks = [b for b in blocks if "servers:" in b]
    secrets_blocks = [b for b in blocks if "password:" in b]

    assert cluster_blocks, "configuration/metal.md must keep a metal cluster.yaml example"
    assert secrets_blocks, "configuration/metal.md must keep a matching secrets.yaml example"

    cluster = yaml.safe_load(cluster_blocks[0])
    secrets = _replace_scaffold(yaml.safe_load(secrets_blocks[0]))
    assert len(cluster["metal"]) == 1

    cluster.update({
        "name": "mycluster",
        "talos": {"version": "v1.13.8", "kubespan": True},
        "kubernetes": {"version": "v1.36.1"},
        "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
        "workers": {"worker": {"count": 3, "cores": 8, "memory": 16, "disk": 100}},
        "proxmox": {
            "url": "https://pve.example.edu:8006",
            "storage": "local-lvm",
            "iso_storage": "local",
            "network": {
                "cluster": {"bridge": "vmbr0"},
                "external": {"bridge": "vmbr0"},
            },
        },
        "network": {
            "cluster": {"cidr": "10.0.0.0/24", "kubeapi_vip": "10.0.0.10"},
            "external": {
                "cidr": "192.0.2.0/24",
                "gateway": "192.0.2.1",
                "anchor_cidr": "169.254.100.0/24",
            },
            "dns": ["192.0.2.53"],
            "ntp": ["ntp.example.edu"],
        },
    })

    (tmp_path / CLUSTER_FILE).write_text(yaml.safe_dump(cluster))
    (tmp_path / SECRETS_FILE).write_text(yaml.safe_dump(secrets))

    cfg = load_config(tmp_path)
    assert cfg.provider_name == "proxmox"
    # the secrets block's BMC credentials merged into the cluster block's server
    group_name, group_cfg = next(iter(cfg.metal.groups.items()))
    server_name, server = next(iter(group_cfg.servers.items()))
    assert server.bmc.username == "root"
    assert server.bmc.password == "a-real-placeholder-credential"
    assert server_name and group_name


def test_metal_examples_use_placeholder_names():
    # `rp001` and `phoenix` are real site hostnames, 172.29.21.0/24 the real
    # site's node network and 172.28.50.0/24 its BMC network; the metal examples
    # across the docs and the `init --metal` scaffold must name placeholder
    # machines and networks instead (the placeholder rule on the configuration
    # overview page), exactly as the managed-SDN example was policed before.
    real = ("rp001", "phoenix", "172.29.21.", "172.28.50.")
    offenders = [
        f"{path.relative_to(ROOT)}: {token}"
        for path in (
            COMMANDS,
            DOCS / "providers" / "metal.md",
            METAL,
            DOCS / "troubleshooting.md",
            SCAFFOLD,
        )
        for token in real
        if token in path.read_text()
    ]
    assert not offenders, "real site names in the metal examples: " + "; ".join(offenders)


# --------------------------------------------------------------------------- #
# 2. Internal links and anchors must resolve.
# --------------------------------------------------------------------------- #


def test_internal_links_point_at_existing_files():
    broken: list[str] = []
    for page in DOCS.rglob("*.md"):
        for filepart, _anchor in _internal_link_targets(page):
            if not filepart:
                continue
            resolved = (page.parent / filepart).resolve()
            if not resolved.is_file():
                broken.append(f"{page.relative_to(ROOT)} -> {filepart}")
    assert not broken, "broken internal markdown link(s): " + "; ".join(broken)


def test_internal_anchor_targets_resolve():
    broken: list[str] = []
    for page in DOCS.rglob("*.md"):
        slugs = _headers(page)
        for filepart, anchor in _internal_link_targets(page):
            if not anchor:
                continue
            if filepart:
                target = (page.parent / filepart).resolve()
                if not target.is_file():
                    continue  # already reported by the existence check
                target_slugs = _headers(target)
            else:
                target_slugs = slugs
            if anchor not in target_slugs:
                broken.append(f"{page.relative_to(ROOT)} -> {filepart}#{anchor}")
    assert not broken, "broken internal anchor link(s): " + "; ".join(broken)


# --------------------------------------------------------------------------- #
# 3. The command reference must cover the CLI.
# --------------------------------------------------------------------------- #


def test_command_reference_covers_every_cli_command():
    cli = CLI.read_text()
    registered = set(re.findall(r'sub\.add_parser\(\s*"([a-zA-Z]+)"', cli))
    aliases: set[str] = set()
    for m in re.finditer(r"aliases=\[([^\]]*)\]", cli):
        aliases.update(a.strip(' "') for a in m.group(1).split(",") if a.strip())
    assert registered, "failed to read the CLI subcommands from cli.py"

    text = COMMANDS.read_text()
    headings = set()
    for line in text.splitlines():
        if line.startswith("## "):
            headings.add(re.sub(r"^##\s*", "", line).strip().strip("`"))

    # every real command gets a dedicated `## command` reference section
    missing = sorted(c for c in registered if c not in headings)
    assert not missing, f"commands.md has no `##` section for CLI command(s): {', '.join(missing)}"
    # the converge aliases are documented under the converge section
    for alias in aliases:
        assert alias in text, f"commands.md does not mention the `{alias}` alias"


# --------------------------------------------------------------------------- #
# 4. README links must match the published (deployed) site, and the
#    reorganized docs must keep the established concepts/ URLs.
# --------------------------------------------------------------------------- #


def _nav_pages() -> dict[str, str]:
    """Map a deployed-site page URL to the markdown source that publishes it.

    MkDocs publishes ``docs/<path>.md`` at ``https://ncsa.github.io/taloscluster/<path>/``
    (``docs/index.md`` at the site root). Only pages reachable from ``mkdocs.yml``
    ``nav`` are built and deployed, so a link matching nothing here would 404.
    """
    nav = yaml.safe_load(MKDOCS.read_text())["nav"]
    pages: dict[str, str] = {}

    def walk(entries) -> None:
        for entry in entries:
            for _title, value in entry.items():
                if isinstance(value, str):
                    src = value.strip()
                    url = "/" if src == "index.md" else "/" + src[:-3] + "/"
                    pages[url] = src
                else:
                    walk(value)

    walk(nav)
    return pages


def test_readme_links_point_at_published_pages():
    # The README advertises the deployed docs by absolute GitHub Pages URLs; each
    # must resolve to a page the current mkdocs nav actually publishes (and, when
    # it carries a fragment, to a heading on that page), so a reorganization that
    # relocates or drops a page fails here instead of breaking the live site.
    pages = _nav_pages()
    broken: list[str] = []
    for m in re.finditer(r"\]\(" + re.escape(SITE_URL) + r"[^)\s]*\)", README.read_text()):
        url = m.group(0).lstrip("](").rstrip(")")
        remainder = url[len(SITE_URL):]
        pagepath, _, anchor = remainder.partition("#")
        page_url = "/" if not pagepath else (pagepath if pagepath.startswith("/") else f"/{pagepath}")  # noqa: E501
        if page_url not in pages:
            broken.append(f"{url} (not a published page)")
            continue
        if anchor:
            src = pages[page_url]
            if anchor not in _headers(DOCS / src):
                broken.append(f"{url} (missing anchor on {src})")
    assert not broken, "README links to non-published sites: " + "; ".join(broken)


def test_reorganized_docs_preserve_concepts_urls():
    # The concepts/ pages carried stable public URLs before the 2026-09-07
    # documentation reorganization and must keep them, so bookmarks, search
    # engines, and links in the wild do not break. If a page genuinely has to
    # move later, relocate it and add a redirect instead of removing the URL.
    pages = _nav_pages()
    stable = {
        "/concepts/talos/",
        "/concepts/machines/",
        "/concepts/lifecycle/",
        "/concepts/plugins/",
    }
    missing = sorted(stable - set(pages))
    assert not missing, (
        "reorganized docs dropped established concepts/ URL(s): "
        + ", ".join(missing)
        + "; preserve the URL or add a redirect"
    )

"""Keep the real site's data out of the repository.

The checkout doubles as an operator workspace: ``csfarm`` symlinks to a real
cluster directory and ``gen-infra.py`` is local tooling whose docstring once
carried the site's real domain, address and email. Both are git-ignored
("local cluster bootstrap manifests, not part of the tool") and must stay that
way -- a dropped ``.gitignore`` entry would let ``git add .`` commit them.

Everything that *is* committed -- the docs, the scaffold and the tests -- names
placeholder values only, the rule the configuration overview states: RFC 5737
documentation ranges for routed networks, RFC 1918 ranges for private node
networks, and ``example.`` hostnames. That rule is enforced as an allow-list,
not a ban list: every address and domain-shaped literal in the committed files
must fall inside the placeholder vocabulary (plus the never-site-specific
special ranges -- loopback, link-local, the tailnet's CGNAT block -- and the
tool's own public endpoints), so the next real hostname, campus subnet or
operator email cannot quietly join the fixtures. One exception cannot be
shaped at all: a bare hostname is indistinguishable from an ordinary word,
so the real site's own machine and subnet names are banned outright as a
backstop under the allow-list.

The file also keeps the committed package metadata honest: dependencies the
code imports directly stay declared in ``pyproject.toml``, and its
description stays provider-neutral.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Local operator files carrying real site data; each must stay covered by
# .gitignore so a plain `git add .` can never pick it up.
LOCAL_SITE_FILES = ("csfarm", "gen-infra.py")

# The networks a committed file may name: the RFC 5737 documentation ranges the
# configuration overview prescribes for routed addresses, the RFC 1918 ranges
# it prescribes for private node and BMC networks, and the special ranges that
# are never site-specific -- loopback, link-local (metadata services, the
# external anchor) and the CGNAT block tailscale hands out. The 172 private
# range is allowed only where the fixtures actually use it: the wider private
# block would admit the real site's own node and BMC subnets.
ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "10.0.0.0/8",
        "172.16.0.0/16",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
    )
)

# Address literals allowed outside those networks: the default-route/allow-all
# CIDR and the public resolvers the scaffold ships as `network.dns`.
ALLOWED_ADDRESS_LITERALS = ("0.0.0.0/0", "8.8.8.8", "8.8.4.4")

# Domain suffixes a committed file may name: the `example.` placeholder
# hostnames, and the public endpoints of the tool's own ecosystem (docs,
# registries, CRD groups) -- not site data.
ALLOWED_DOMAINS = (
    "example.com",
    "example.edu",
    "example.org",
    "example.net",
    "talos.dev",
    "siderolabs.com",
    "github.com",
    "github.io",
    "k8s.io",
    "kubernetes.io",
    "metallb.io",
    "ghcr.io",
    "velero.io",
    "tailscale.com",
    "headscale.net",
    "proxmox.com",
    "ntp.org",
    "cattle.io",
    "letsencrypt.org",
)

# Top-level endings that make a dotted token a domain name at all; anything
# else (attribute access such as `network.cluster`, versions such as `v1.13.9`)
# is not site-data-shaped and is skipped.
TLDS = frozenset({"com", "edu", "org", "net", "io", "dev", "cloud", "app", "sh", "gov"})

# File-name endings a dotted token may carry without being a domain
# (`cluster.yaml`, `test_config.py`, `install.sh`, ...).
FILE_SUFFIXES = frozenset(
    {"md", "py", "yaml", "yml", "toml", "json", "sh", "swp", "txt", "lock", "cfg", "ini"}
)

DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9][a-zA-Z0-9-]*)+\b")
IP_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?![\w.])")

# The real site's own machine names and network prefixes, banned outright: a
# bare hostname matches no shape the allow-list can judge (it is just a word)
# and a subnet written without its last octet is not an address literal, so
# no vocabulary check can refuse them. Assembled from fragments so this file
# itself stays inside the vocabulary the guard scans.
REAL_SITE_TOKENS = (
    "rp" + "001",
    "phoe" + "nix",
    "172.29." + "21.",
    "172.28." + "50.",
)


# Everything the rule applies to: the published docs, the scaffolded example
# cluster and the test suite's fixtures.
def _committed_files() -> list[Path]:
    files: list[Path] = sorted((ROOT / "docs").rglob("*.md"))
    files.append(ROOT / "taloscluster" / "scaffold.py")
    files += sorted((ROOT / "tests").glob("*.py"))
    return files


def _relative(path: Path) -> Path:
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def _placeholder_offenders(path: Path) -> list[str]:
    """Every address or domain literal in ``path`` outside the placeholder vocabulary."""
    rel = _relative(path)
    offenders: list[str] = []
    for match in IP_RE.finditer(path.read_text()):
        literal = match.group(1) + (match.group(2) or "")
        if literal in ALLOWED_ADDRESS_LITERALS:
            continue
        try:
            network = ipaddress.ip_network(literal, strict=False)
        except ValueError:
            continue  # not an address at all -- e.g. a deliberately invalid one
        if not any(network.subnet_of(allowed) for allowed in ALLOWED_NETWORKS):
            offenders.append(f"{rel}: address {literal}")
    for match in DOMAIN_RE.finditer(path.read_text()):
        domain = match.group(0).lower()
        *_, tld = domain.rsplit(".", 1)
        if tld not in TLDS or tld in FILE_SUFFIXES:
            continue
        if not any(domain == d or domain.endswith(f".{d}") for d in ALLOWED_DOMAINS):
            offenders.append(f"{rel}: domain {domain}")
    return offenders


def _site_token_offenders(path: Path) -> list[str]:
    """The real site's own tokens, which no shape check can refuse."""
    rel = _relative(path)
    text = path.read_text()
    return [f"{rel}: site token {token}" for token in REAL_SITE_TOKENS if token in text]


@pytest.mark.parametrize("name", LOCAL_SITE_FILES)
def test_local_site_files_are_git_ignored(name):
    # `git check-ignore` answers for the path name itself, so this also holds
    # on a fresh clone where the git-ignored files do not exist.
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", name],
        cwd=ROOT,
        capture_output=True,
    )
    assert ignored.returncode == 0, f"{name} is no longer covered by .gitignore"


def test_committed_files_name_placeholder_values_only():
    offenders = [
        offender
        for path in _committed_files()
        for offender in _placeholder_offenders(path) + _site_token_offenders(path)
    ]
    assert not offenders, "non-placeholder values in committed files: " + "; ".join(offenders)


def test_placeholder_check_catches_site_shaped_values(tmp_path):
    # The allow-list must actually refuse what it exists against: a routable
    # address, a real domain, a real operator email, the real site's RFC 1918
    # node and BMC subnets and its bare hostnames are flagged, while the
    # placeholder vocabulary and the tool's own public endpoints pass. The
    # site-shaped values are assembled from fragments so this file itself
    # stays inside the vocabulary the guard scans.
    site_tld = ".c" + "loud"
    campus_tld = ".e" + "du"
    routable = "141.142." + "36.1"
    node_net = "172.29." + "21.0/24"
    bmc_net = "172.28." + "50.0/24"
    rack = "rp" + "001"
    machine = "phoe" + "nix"
    probe = tmp_path / "probe.md"
    probe.write_text(
        "bmc 203.0.113.5, dns ntp.example.edu, admin@example.edu,\n"
        f"router {routable}, host cyclops.ncsa{site_tld}, nodes {node_net},"
        f" ipmi {bmc_net}, servers {rack} and {machine},\n"
        f"mail ops@illinois{campus_tld}, registry factory.talos.dev,"
        " gateway 8.8.8.8, private nodes 172.16.0.1/16\n"
    )
    offenders = _placeholder_offenders(probe) + _site_token_offenders(probe)
    assert any(routable in o for o in offenders), offenders
    assert any("ncsa" + site_tld in o for o in offenders), offenders
    assert any("illinois" + campus_tld in o for o in offenders), offenders
    assert any(node_net in o for o in offenders), offenders
    assert any(bmc_net in o for o in offenders), offenders
    assert any(rack in o for o in offenders), offenders
    assert any(machine in o for o in offenders), offenders
    assert not any(
        "203.0.113.5" in o
        or "example.edu" in o
        or "talos.dev" in o
        or "8.8.8.8" in o
        or "172.16.0.1" in o
        for o in offenders
    ), offenders


# --------------------------------------------------------------------------- #
# The committed package metadata stays honest.
# --------------------------------------------------------------------------- #

PYPROJECT = ROOT / "pyproject.toml"


def test_keystoneauth1_is_a_declared_dependency():
    # openstack/backend.py imports keystoneauth1.exceptions directly; it must be
    # a declared dependency, not a transitive one, so the import cannot silently
    # break when a middle package stops shipping it.
    assert "keystoneauth1" in PYPROJECT.read_text()


def test_urllib3_is_a_declared_dependency():
    # metal/redfish.py imports urllib3 directly to silence insecure-request
    # warnings; it must be a declared dependency, not a transitive one, so the
    # import cannot silently break when a middle package stops shipping it.
    assert "urllib3" in PYPROJECT.read_text()


def test_pyproject_description_is_provider_neutral():
    # The package description (what PyPI shows) names both providers and no
    # longer references the removed terraform/cluster.sh workflow.
    text = PYPROJECT.read_text()
    assert "terraform" not in text
    assert "OpenStack or Proxmox" in text

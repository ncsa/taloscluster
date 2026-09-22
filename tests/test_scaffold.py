"""Tests for taloscluster.scaffold: `taloscluster init` file creation, permissions,
never-overwrite behaviour, and .gitignore append logic, using tmp_path.
"""

from __future__ import annotations

import ipaddress
import os
import stat
from types import SimpleNamespace

import pytest
import yaml

from taloscluster import naming, plugins
from taloscluster.config import ConfigError, load_config
from taloscluster.output import Die
from taloscluster.scaffold import CLUSTER_TEMPLATE, GITIGNORE_ENTRIES, init


@pytest.fixture(autouse=True)
def no_installed_plugins(monkeypatch):
    monkeypatch.setattr(plugins, "discover", lambda: [])


def test_init_creates_all_three_files(tmp_path):
    init(tmp_path, name="demo")
    assert (tmp_path / "cluster.yaml").is_file()
    assert (tmp_path / "secrets.yaml").is_file()
    assert (tmp_path / ".gitignore").is_file()


def test_init_creates_missing_directory(tmp_path):
    root = tmp_path / "new" / "cluster"
    init(root, name="demo")
    assert (root / "cluster.yaml").is_file()


def test_init_calls_installed_plugin_initializers(monkeypatch, tmp_path):
    seen = []

    def initialize(root):
        assert (root / "cluster.yaml").is_file()
        assert (root / "secrets.yaml").is_file()
        seen.append(root)

    monkeypatch.setattr(plugins, "initialize", initialize)
    init(tmp_path, name="demo")
    assert seen == [tmp_path]


def test_cluster_yaml_is_valid_and_uses_name(tmp_path):
    init(tmp_path, name="demo")
    d = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    assert d["name"] == "demo"
    # the scaffold lists secrets.yaml, so the credentials merge in
    assert d["include"] == ["secrets.yaml"]
    # every key load_config requires must be present in the template
    assert d["talos"]["version"]
    assert d["kubernetes"]["version"]
    assert {"count", "flavor", "disk"} <= d["controlplane"].keys()
    assert d["openstack"].keys() >= {"url", "availability_zone", "external_net"}
    assert d["network"].keys() >= {"cluster", "dns", "ntp"}
    assert d["network"]["cluster"]["cidr"]
    assert load_config(tmp_path).provider_name == "openstack"


def test_secrets_yaml_is_valid_and_mode_0600(tmp_path):
    init(tmp_path, name="demo")
    path = tmp_path / "secrets.yaml"
    d = yaml.safe_load(path.read_text())
    assert d["openstack"].keys() >= {"credential_id", "credential_secret"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_proxmox_templates_are_valid_and_provider_specific(tmp_path):
    init(tmp_path, name="demo", provider="proxmox")

    cluster = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    assert "openstack" not in cluster
    assert cluster["controlplane"].keys() >= {"count", "cores", "memory", "disk"}
    assert cluster["workers"]["worker"].keys() >= {
        "count", "cores", "memory", "disk",
    }
    assert cluster["proxmox"].keys() >= {
        "url", "storage", "iso_storage", "cidata_storage", "placement_strategy",
        "network",
    }
    # the Proxmox section carries the plumbing; the L2 lives under `network`
    assert cluster["proxmox"]["network"]["cluster"].keys() == {"bridge"}
    assert cluster["network"]["cluster"].keys() >= {"cidr", "kubeapi_vip"}
    # the VIP carries its operator note into the written file, not just the source
    written = (tmp_path / "cluster.yaml").read_text()
    assert "must sit OUTSIDE any DHCP range" in written
    assert "outside the static layout" in written

    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert "openstack" not in secrets
    assert secrets["proxmox"].keys() >= {"token_id", "token_secret"}
    cfg = load_config(tmp_path)
    assert cfg.provider_name == "proxmox"
    # the scaffolded placeholder must be replaced before the credentials are used
    assert secrets["proxmox"]["token_secret"] == "CHANGE-ME"
    with pytest.raises(ConfigError, match="CHANGE-ME"):
        cfg.provider.credentials()


def test_proxmox_scaffold_kubeapi_vip_sits_outside_the_sdn_layout(tmp_path):
    """The scaffolded default VIP must still be valid if the user follows the
    inline `sdn:` hint -- so it may not sit inside the SDN static layout
    (controlplane block / worker pool blocks), which would then be rejected."""
    init(tmp_path, name="demo", provider="proxmox")
    cluster = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    vip = cluster["network"]["cluster"]["kubeapi_vip"]
    cidr = cluster["network"]["cluster"]["cidr"]
    assert ipaddress.ip_address(vip) not in naming.sdn_reserved(
        cidr, tuple(cluster["workers"])
    )
    # the value also passes the full (Sdn-less) config load, as it always did
    assert load_config(tmp_path).provider_name == "proxmox"


def test_openstack_templates_remain_the_default(tmp_path):
    init(tmp_path, name="demo")
    cluster = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert "openstack" in cluster and "proxmox" not in cluster
    assert "openstack" in secrets and "proxmox" not in secrets
    cfg = load_config(tmp_path)
    assert cfg.provider_name == "openstack"
    # the scaffolded placeholder must be replaced before the credentials are used
    assert secrets["openstack"]["credential_id"] == "CHANGE-ME"
    with pytest.raises(ConfigError, match="CHANGE-ME"):
        assert cfg.openstack_credentials


def test_plain_init_has_no_metal_section(tmp_path):
    init(tmp_path, name="demo")
    cluster = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert "metal" not in cluster
    assert "metal" not in secrets
    assert load_config(tmp_path).metal is None


@pytest.mark.parametrize("provider", ["openstack", "proxmox"])
def test_metal_scaffold_produces_a_loadable_pair(tmp_path, provider):
    init(tmp_path, name="demo", provider=provider, metal=True)

    cluster = yaml.safe_load((tmp_path / "cluster.yaml").read_text())
    secrets = yaml.safe_load((tmp_path / "secrets.yaml").read_text())

    # one example group carrying the settings every group needs
    group = cluster["metal"]["rack1"]
    assert group["role"] == "worker"
    # the example group sits on another L2, which only loads with the KubeSpan
    # opt-in the scaffold writes into the talos section
    assert cluster["talos"]["kubespan"] is True
    # redfish starts false so the placeholder BMC credentials still load
    assert group["redfish"] is False
    assert group["disk"]
    assert group["network"]["cidr"]
    assert group["interfaces"]
    assert group["servers"]
    # the BMC credentials are scaffolded into secrets.yaml, not cluster.yaml
    assert secrets["metal"]["rack1"]["bmc"] == {
        "username": "CHANGE-ME",
        "password": "CHANGE-ME",
    }
    assert "bmc" not in group

    cfg = load_config(tmp_path)
    assert cfg.provider_name == provider
    assert cfg.metal.groups["rack1"].servers["srv01"].bmc.username == "CHANGE-ME"


def test_metal_init_without_a_provider_is_refused(tmp_path):
    """Bare metal joins a cluster a provider manages: `--metal` alone has no
    backend to plan, converge or destroy, so the scaffold refuses it."""
    with pytest.raises(Die, match="--metal requires a VM provider"):
        init(tmp_path, name="demo", provider=None, metal=True)
    assert not (tmp_path / "cluster.yaml").exists()
    assert not (tmp_path / "secrets.yaml").exists()


def test_metal_init_mentions_include_in_the_next_steps(tmp_path, capsys):
    init(tmp_path, name="demo", metal=True)
    assert "include:" in capsys.readouterr().out


def test_metal_init_never_duplicates_the_section(tmp_path):
    init(tmp_path, name="demo", metal=True)
    init(tmp_path, name="demo", metal=True)
    text = (tmp_path / "cluster.yaml").read_text()
    assert text.count("metal:") == 1


def test_init_never_overwrites_existing_files(tmp_path):
    (tmp_path / "cluster.yaml").write_text("name: keepme\n")
    (tmp_path / "secrets.yaml").write_text("openstack: {}\n")
    init(tmp_path, name="demo")
    assert (tmp_path / "cluster.yaml").read_text() == "name: keepme\n"
    assert (tmp_path / "secrets.yaml").read_text() == "openstack: {}\n"


def test_gitignore_covers_secret_and_derived_files(tmp_path):
    init(tmp_path, name="demo")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    for entry in ("secrets.yaml", "talossecrets.yaml", "talosconfig",
                  "kubeconfig", ".metal/"):
        assert entry in lines


def test_gitignore_appends_only_missing_entries(tmp_path):
    (tmp_path / ".gitignore").write_text("secrets.yaml\n*.pyc\n")
    init(tmp_path, name="demo")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count("secrets.yaml") == 1
    assert "*.pyc" in lines
    for entry in GITIGNORE_ENTRIES:
        assert entry in lines


def test_gitignore_untouched_when_complete(tmp_path):
    content = "".join(f"{e}\n" for e in GITIGNORE_ENTRIES)
    (tmp_path / ".gitignore").write_text(content)
    init(tmp_path, name="demo")
    assert (tmp_path / ".gitignore").read_text() == content


def test_scaffold_comment_no_longer_claims_every_node_gets_ncsa_project():
    """Only OpenStack adds the `ncsa/project` label; the scaffold comment must
    say so instead of promising it on every node (Proxmox supplies none)."""
    text = CLUSTER_TEMPLATE
    assert "ncsa/role" in text and "ncsa/pool" in text
    assert "OpenStack adds ncsa/project" in text
    assert "always added as ncsa/project" not in text


@pytest.mark.parametrize("provider", ["openstack", "proxmox"])
def test_scaffold_loads_with_comment_only_plugin_sections(tmp_path, provider, monkeypatch):
    """A plugin's scaffolded secrets section is comments only, so it parses as
    null; merging it must not collide with the plugin's cluster.yaml section."""
    init(tmp_path, name="demo", provider=provider)
    monkeypatch.setattr(
        plugins, "discover",
        lambda: [SimpleNamespace(module=SimpleNamespace(CONFIG_SECTIONS=("rancher",)))],
    )
    for path, section in (
        ("cluster.yaml", "\nrancher:\n  admins: []\n  users: []\n"),
        ("secrets.yaml", "\nrancher:\n  # url: https://rancher.example.edu\n  # token: x\n"),
    ):
        with (tmp_path / path).open("a") as f:
            f.write(section)

    assert load_config(tmp_path).provider_name == provider

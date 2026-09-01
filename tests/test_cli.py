"""Command aliases dispatch through the canonical CLI handlers."""

from __future__ import annotations

import pytest

from taloscluster import cli


@pytest.mark.parametrize(
    ("provider_option", "expected"),
    [([], "openstack"), (["--openstack"], "openstack"), (["--proxmox"], "proxmox")],
)
def test_init_selects_provider(monkeypatch, tmp_path, provider_option, expected):
    seen = {}

    def init(root, name, provider):
        seen.update(root=root, name=name, provider=provider)

    monkeypatch.setattr(cli._scaffold, "init", init)

    argv = ["init", "demo", "-C", str(tmp_path), *provider_option]
    assert cli.main(argv) == 0
    assert seen == {"root": tmp_path, "name": "demo", "provider": expected}


def test_init_provider_flags_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit, match="2"):
        cli.main([
            "init", "demo", "-C", str(tmp_path), "--openstack", "--proxmox",
        ])


@pytest.mark.parametrize("command", ["converge", "sync", "apply"])
def test_converge_aliases_use_the_same_handler(monkeypatch, tmp_path, command):
    seen = {}

    def converge(root, assume_yes=False):
        seen["root"] = root
        seen["assume_yes"] = assume_yes

    monkeypatch.setattr(cli._converge, "converge", converge)
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: seen.update(dry_run=enabled))

    assert cli.main([command, "-C", str(tmp_path), "--dry-run", "--yes"]) == 0
    assert seen == {"root": tmp_path, "assume_yes": True, "dry_run": True}

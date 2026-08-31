"""Command aliases dispatch through the canonical CLI handlers."""

from __future__ import annotations

import pytest

from taloscluster import cli


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

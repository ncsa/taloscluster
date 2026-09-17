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

    def converge(root, assume_yes=False, reboot=False):
        seen["root"] = root
        seen["assume_yes"] = assume_yes
        seen["reboot"] = reboot

    monkeypatch.setattr(cli._converge, "converge", converge)
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: seen.update(dry_run=enabled))

    assert cli.main([command, "-C", str(tmp_path), "--dry-run", "--yes"]) == 0
    assert seen == {"root": tmp_path, "assume_yes": True, "dry_run": True, "reboot": False}


@pytest.mark.parametrize("command", ["converge", "plan"])
def test_reboot_flag_reaches_converge(monkeypatch, tmp_path, command):
    seen = {}
    monkeypatch.setattr(
        cli._converge, "converge",
        lambda root, assume_yes=False, reboot=False: seen.update(reboot=reboot),
    )
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: None)
    assert cli.main([command, "-C", str(tmp_path), "--reboot"]) == 0
    assert seen == {"reboot": True}


def test_openstack_sdk_error_exits_cleanly_through_cli_main(
    make_config, tmp_path, monkeypatch, capsys
):
    """An escaping OpenStack SDKException is wrapped into a ReconcileError at the
    backend boundary, so cli.main prints one ``ERROR:`` line and exits 1 instead
    of leaking a traceback (the SDKException subclasses only Exception and cli.main
    does not handle it directly)."""
    from openstack import exceptions as os_exceptions
    from taloscluster.openstack import backend as os_backend
    from taloscluster.openstack.session import Inventory
    from taloscluster.output import set_dry_run

    make_config()  # a valid cluster.yaml in tmp_path
    (tmp_path / "secrets.yaml").write_text(
        "openstack:\n"
        "  credential_id: cred-id\n"
        "  credential_secret: cred-secret\n"
    )
    # reach backend.load_inventory without real auth, then make the first SDK
    # call fail like a Neutron 409. Don't POST to the talos image factory for a
    # schematic id (pure unit test: no cloud access).
    monkeypatch.setattr("taloscluster.converge.preflight_tools", lambda: None)
    monkeypatch.setattr(cli._converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(os_backend, "connect", lambda cfg, secrets: object())

    def _failing_load(self):
        raise os_exceptions.ConflictException(message="Neutron 409 (resource already exists)")

    monkeypatch.setattr(Inventory, "load", _failing_load)

    try:
        rc = cli.main(["converge", "-C", str(tmp_path), "--dry-run"])
    finally:
        set_dry_run(False)  # converge --dry-run sets the global dry-run flag

    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR: OpenStack API error: Neutron 409 (resource already exists)" in err
    assert "Traceback" not in err


def test_openstack_transport_error_exits_cleanly_through_cli_main(
    make_config, tmp_path, monkeypatch, capsys
):
    """A keystoneauth1 transport error (unreachable or timing-out cloud) is not an
    ``openstack.exceptions.SDKException``, but is wrapped into a ReconcileError at
    the backend boundary too, so cli.main still prints one ``ERROR:`` line and exits
    1 instead of leaking a traceback."""
    from keystoneauth1.exceptions.connection import ConnectTimeout

    from taloscluster.openstack import backend as os_backend
    from taloscluster.openstack.session import Inventory
    from taloscluster.output import set_dry_run

    make_config()  # a valid cluster.yaml in tmp_path
    (tmp_path / "secrets.yaml").write_text(
        "openstack:\n"
        "  credential_id: cred-id\n"
        "  credential_secret: cred-secret\n"
    )
    monkeypatch.setattr("taloscluster.converge.preflight_tools", lambda: None)
    monkeypatch.setattr(cli._converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(os_backend, "connect", lambda cfg, secrets: object())

    def _failing_load(self):
        raise ConnectTimeout("Timed out connecting to the cloud endpoint")

    monkeypatch.setattr(Inventory, "load", _failing_load)

    try:
        rc = cli.main(["converge", "-C", str(tmp_path), "--dry-run"])
    finally:
        set_dry_run(False)  # converge --dry-run sets the global dry-run flag

    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR: OpenStack API error: Timed out connecting to the cloud endpoint" in err
    assert "Traceback" not in err

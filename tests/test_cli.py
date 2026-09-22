"""Command aliases dispatch through the canonical CLI handlers."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from taloscluster import cli
from taloscluster import plugins as _plugins


def _stub_plugin(name, **hooks):
    """A real :class:`Plugin` with a stub module exposing the given callables."""

    class _Module:
        pass

    module = _Module()
    for k, v in hooks.items():
        setattr(module, k, v)
    return _plugins.Plugin(name=name, module=module, after=())


@pytest.mark.parametrize(
    ("provider_option", "expected"),
    [([], None), (["--openstack"], "openstack"), (["--proxmox"], "proxmox")],
)
def test_init_selects_provider(monkeypatch, tmp_path, provider_option, expected):
    """The provider flag is forwarded verbatim; with none given the scaffold
    applies the OpenStack default."""
    seen = {}

    def init(root, name, provider, metal):
        seen.update(root=root, name=name, provider=provider, metal=metal)

    monkeypatch.setattr(cli._scaffold, "init", init)

    argv = ["init", "demo", "-C", str(tmp_path), *provider_option]
    assert cli.main(argv) == 0
    assert seen == {
        "root": tmp_path, "name": "demo", "provider": expected, "metal": False,
    }


@pytest.mark.parametrize(
    ("provider_option", "expected"),
    [(["--openstack"], "openstack"), (["--proxmox"], "proxmox")],
)
def test_init_forwards_metal(monkeypatch, tmp_path, provider_option, expected):
    """`--metal` is additive to either provider and is forwarded verbatim."""
    seen = {}

    def init(root, name, provider, metal):
        seen.update(root=root, name=name, provider=provider, metal=metal)

    monkeypatch.setattr(cli._scaffold, "init", init)

    argv = ["init", "demo", "-C", str(tmp_path), "--metal", *provider_option]
    assert cli.main(argv) == 0
    assert seen == {
        "root": tmp_path, "name": "demo", "provider": expected, "metal": True,
    }


def test_init_metal_without_a_provider_is_refused(tmp_path, capsys):
    """`--metal` alone is refused: bare-metal machines join a cluster a
    provider manages, so the scaffold never writes a metal-only pair."""
    assert cli.main(["init", "demo", "-C", str(tmp_path), "--metal"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: --metal requires a VM provider")
    assert not (tmp_path / "cluster.yaml").exists()
    assert not (tmp_path / "secrets.yaml").exists()


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

    (tmp_path / "secrets.yaml").write_text(
        "openstack:\n"
        "  credential_id: cred-id\n"
        "  credential_secret: cred-secret\n"
    )
    make_config()  # a valid cluster.yaml in tmp_path, including secrets.yaml
    # reach backend.load_inventory without real auth, then make the first SDK
    # call fail like a Neutron 409. Don't POST to the talos image factory for a
    # schematic id (pure unit test: no cloud access).
    monkeypatch.setattr("taloscluster.converge.preflight_tools", lambda: None)
    monkeypatch.setattr(cli._converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(os_backend, "connect", lambda cfg: object())

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

    (tmp_path / "secrets.yaml").write_text(
        "openstack:\n"
        "  credential_id: cred-id\n"
        "  credential_secret: cred-secret\n"
    )
    make_config()  # a valid cluster.yaml in tmp_path, including secrets.yaml
    monkeypatch.setattr("taloscluster.converge.preflight_tools", lambda: None)
    monkeypatch.setattr(cli._converge.factory, "schematic_id", lambda _s: "scheme-a-01")
    monkeypatch.setattr(os_backend, "connect", lambda cfg: object())

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


# -- image -----------------------------------------------------------------

def test_image_download_dispatches(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: seen.update(dry_run=enabled))
    monkeypatch.setattr(cli._converge, "image_download", lambda root: seen.update(root=root))
    assert cli.main(["image", "download", "-C", str(tmp_path), "--dry-run"]) == 0
    assert seen == {"root": tmp_path, "dry_run": True}


def test_image_remove_dispatches_with_yes(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: seen.update(dry_run=enabled))
    monkeypatch.setattr(
        cli._converge, "image_remove",
        lambda root, assume_yes=False: seen.update(root=root, assume_yes=assume_yes),
    )
    assert cli.main(["image", "remove", "-C", str(tmp_path), "--yes"]) == 0
    assert seen == {"root": tmp_path, "assume_yes": True, "dry_run": False}


# -- destroy ---------------------------------------------------------------

def test_destroy_dispatches_and_forwards_rc(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "set_dry_run", lambda enabled: seen.update(dry_run=enabled))
    monkeypatch.setattr(
        cli._converge, "destroy",
        lambda root, assume_yes=False: seen.update(root=root, assume_yes=assume_yes) or 7,
    )
    assert cli.main(["destroy", "-C", str(tmp_path), "--yes", "--dry-run"]) == 7
    assert seen == {"root": tmp_path, "assume_yes": True, "dry_run": True}


# -- plugin -----------------------------------------------------------------

def test_plugin_list_is_reserved_name(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli._plugins, "discover", lambda: [])
    assert cli.main(["plugin", "list", "-C", str(tmp_path)]) == 0
    assert "no plugins installed" in capsys.readouterr().out


def test_plugin_named_list_cannot_be_run(monkeypatch, tmp_path, capsys):
    listed = _stub_plugin("list", configured=lambda ctx: True)
    monkeypatch.setattr(cli._plugins, "discover", lambda: [listed])
    assert cli.main(["plugin", "list", "-C", str(tmp_path)]) == 0
    # the reserved name routes to the listing, never to `_plugins.run`
    out = capsys.readouterr().out
    assert "list" in out


def test_plugin_validate_runs_before_configured_check(make_config, monkeypatch, tmp_path):
    make_config()  # a valid cluster.yaml so Context.load succeeds before the configured check
    plugin = _stub_plugin("demo", configured=lambda ctx: False,
                          converge=lambda ctx, assume_yes=False: {})
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    seen = {}
    monkeypatch.setattr(
        cli._plugins, "validate",
        lambda ctx, only=None: seen.update(validated=only),
    )
    assert cli.main(["plugin", "demo", "-C", str(tmp_path)]) == 0
    assert seen == {"validated": [plugin]}
    assert not plugin.configured(object())  # sanity: it really is unconfigured


def test_plugin_validate_refuses_unconfigured_plugin(make_config, monkeypatch, tmp_path):
    make_config()
    plugin = _stub_plugin("demo", configured=lambda ctx: False)

    def reject(ctx, only=None):
        raise cli.ConfigError("broken section")

    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "validate", reject)
    assert cli.main(["plugin", "demo", "-C", str(tmp_path)]) == 1


def test_plugin_destroy_confirmation_wrong_name_aborts(make_config, monkeypatch, tmp_path):
    make_config()  # a valid cluster.yaml so ctx.cfg.name is "testcluster"
    plugin = _stub_plugin("demo", configured=lambda ctx: True,
                          destroy=lambda ctx, assume_yes=False: None)
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "validate", lambda ctx, only=None: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrongcluster")
    with pytest.raises(SystemExit) as exc:
        cli.main(["plugin", "demo", "destroy", "-C", str(tmp_path)])
    assert exc.value.code == "aborted"


def test_plugin_destroy_confirmation_matching_name_runs(make_config, monkeypatch, tmp_path):
    make_config()
    plugin = _stub_plugin("demo", configured=lambda ctx: True,
                          destroy=lambda ctx, assume_yes=False: None)
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "validate", lambda ctx, only=None: None)
    seen = {}
    monkeypatch.setattr(
        cli._plugins, "run",
        lambda plugins, hook, ctx, **kw: seen.update(hook=hook, assume_yes=kw["assume_yes"]) or 0,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "testcluster")
    assert cli.main(["plugin", "demo", "destroy", "-C", str(tmp_path)]) == 0
    assert seen == {"hook": "destroy", "assume_yes": False}


def test_plugin_destroy_skip_confirmation_with_yes(make_config, monkeypatch, tmp_path):
    make_config()
    plugin = _stub_plugin("demo", configured=lambda ctx: True,
                          destroy=lambda ctx, assume_yes=False: None)
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "validate", lambda ctx, only=None: None)
    seen = {}
    monkeypatch.setattr(
        cli._plugins, "run",
        lambda plugins, hook, ctx, **kw: seen.update(hook=hook, assume_yes=kw["assume_yes"]) or 0,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("should not prompt"))
    assert cli.main(["plugin", "demo", "destroy", "-C", str(tmp_path), "--yes"]) == 0
    assert seen == {"hook": "destroy", "assume_yes": True}


@pytest.mark.parametrize("ok,expected", [(True, 0), (False, 1)])
def test_plugin_check_exit_reflects_ok(make_config, monkeypatch, tmp_path, ok, expected):
    make_config()
    plugin = _stub_plugin("demo", configured=lambda ctx: True,
                          check=lambda ctx: {"ok": ok})
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "collect",
                        lambda plugins, hook, ctx: {"demo": {"ok": ok}})
    assert cli.main(["plugin", "demo", "check", "-C", str(tmp_path)]) == expected


def test_plugin_status_exits_zero(make_config, monkeypatch, tmp_path):
    make_config()
    plugin = _stub_plugin("demo", configured=lambda ctx: True,
                          status=lambda ctx: {"state": "ok"})
    monkeypatch.setattr(cli._plugins, "discover", lambda: [plugin])
    monkeypatch.setattr(cli._plugins, "collect",
                        lambda plugins, hook, ctx: {"demo": {"state": "ok"}})
    assert cli.main(["plugin", "demo", "status", "-C", str(tmp_path)]) == 0


# -- metal -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("action", "expected_kwargs"),
    [
        ("inspect", {}),
        ("boot", {"serve": False}),
        ("wait", {}),
        ("apply", {}),
        ("eject", {}),
        ("join", {"serve": False}),
    ],
)
def test_metal_dispatches(monkeypatch, tmp_path, action, expected_kwargs):
    seen = {}
    monkeypatch.setattr(
        cli._metal, action,
        lambda root, name, **kw: seen.update(root=root, name=name, **kw),
    )
    assert cli.main(["metal", action, "rp001", "-C", str(tmp_path)]) == 0
    assert seen == {"root": tmp_path, "name": "rp001", **expected_kwargs}


def test_metal_boot_forwards_serve(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        cli._metal, "boot",
        lambda root, name, **kw: seen.update(root=root, name=name, **kw),
    )
    assert cli.main(["metal", "boot", "rp001", "--serve", "-C", str(tmp_path)]) == 0
    assert seen == {"root": tmp_path, "name": "rp001", "serve": True}


def test_metal_serve_is_rejected_for_the_bmc_free_actions(tmp_path, capsys):
    assert cli.main(["metal", "wait", "rp001", "--serve", "-C", str(tmp_path)]) == 1
    assert "--serve" in capsys.readouterr().err


# -- metal-only configs are refused by every command -------------------------

def _metal_only_dir(tmp_path, monkeypatch):
    """A cluster directory whose machines are all bare metal: the scaffolded
    provider+metal pair with the provider section stripped out, i.e. the pair
    the metal-only `init --metal` scaffold used to write."""
    monkeypatch.setattr(_plugins, "discover", lambda: [])
    cli._scaffold.init(tmp_path, name="demo", provider="proxmox", metal=True)
    for name in ("cluster.yaml", "secrets.yaml"):
        path = tmp_path / name
        d = yaml.safe_load(path.read_text())
        d.pop("proxmox", None)
        path.write_text(yaml.safe_dump(d))


METAL_ONLY_COMMANDS = [
    ["plan"],
    ["converge"],
    ["status"],
    ["check"],
    ["env"],
    ["image", "download"],
    ["destroy"],
]


@pytest.mark.parametrize("argv", METAL_ONLY_COMMANDS, ids=lambda a: " ".join(a))
def test_metal_only_config_is_refused_by_every_command(tmp_path, monkeypatch, capsys, argv):
    """A config with a metal section but no VM provider is refused at load
    time, so every command exits 1 with one clean ERROR line instead of the
    TypeError traceback `backend_for` used to raise."""
    _metal_only_dir(tmp_path, monkeypatch)
    assert cli.main([*argv, "-C", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert err.count("ERROR:") == 1
    assert "metal section requires a VM provider" in err
    assert "Traceback" not in err


# -- exception-to-exit-code mapping in main ---------------------------------

def _bomb(exc):
    def boom(root, assume_yes=False, reboot=False):
        raise exc
    return boom


def test_main_maps_die_to_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli._converge, "converge", _bomb(cli.Die("boom")))
    assert cli.main(["converge", "-C", str(tmp_path)]) == 1
    assert "ERROR: boom" in capsys.readouterr().err


def test_main_maps_config_error_to_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli._converge, "converge", _bomb(cli.ConfigError("bad config")))
    assert cli.main(["converge", "-C", str(tmp_path)]) == 1
    assert "ERROR: bad config" in capsys.readouterr().err


def test_main_maps_called_process_error_to_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli._converge, "converge",
        _bomb(subprocess.CalledProcessError(1, ["talosctl", "get", "nodes"])),
    )
    assert cli.main(["converge", "-C", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "ERROR: command failed" in err
    assert "talosctl get nodes" in err


def test_main_maps_timeout_expired_to_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli._converge, "converge",
        _bomb(subprocess.TimeoutExpired(["kubectl", "get", "nodes"], 30)),
    )
    assert cli.main(["converge", "-C", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "timed out" in err
    assert "kubectl get nodes" in err


def test_main_maps_keyboard_interrupt_to_130(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli._converge, "converge", _bomb(KeyboardInterrupt()))
    assert cli.main(["converge", "-C", str(tmp_path)]) == 130
    assert "interrupted" in capsys.readouterr().err

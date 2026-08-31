"""Plugin discovery, ordering and fan-out.

No real plugin is installed here -- `discover()` is fed fake entry points, so
these tests describe the contract core promises a plugin, independent of
whichever plugins happen to be installed in the environment.
"""

from __future__ import annotations

import types

import pytest

from taloscluster import plugins
from taloscluster.context import Context


class FakeEntryPoint:
    def __init__(self, name, module=None, error=None):
        self.name = name
        self._module = module
        self._error = error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._module


def make_module(name, after=(), **hooks):
    mod = types.ModuleType(name)
    mod.AFTER = after
    mod.configured = hooks.pop("configured", lambda ctx: True)
    for hook, fn in hooks.items():
        setattr(mod, hook, fn)
    return mod


@pytest.fixture
def ctx(tmp_path):
    """A Context with the status payload pre-filled, so nothing hits OpenStack."""
    return Context(root=tmp_path, cfg=None, status={})


def install(monkeypatch, *eps):
    monkeypatch.setattr(plugins, "entry_points", lambda group: list(eps))


# ---- discovery -------------------------------------------------------------

def test_discover_returns_installed_plugins(monkeypatch):
    install(monkeypatch, FakeEntryPoint("a", make_module("a")))
    found = plugins.discover()
    assert [p.name for p in found] == ["a"]


def test_broken_plugin_is_dropped_not_fatal(monkeypatch, capsys):
    install(
        monkeypatch,
        FakeEntryPoint("broken", error=ImportError("no such thing")),
        FakeEntryPoint("fine", make_module("fine")),
    )
    assert [p.name for p in plugins.discover()] == ["fine"]
    assert "broken" in capsys.readouterr().err


# ---- ordering --------------------------------------------------------------

def test_after_orders_plugins(monkeypatch):
    install(
        monkeypatch,
        FakeEntryPoint("argocd", make_module("argocd", after=("rancher",))),
        FakeEntryPoint("rancher", make_module("rancher")),
    )
    assert [p.name for p in plugins.discover()] == ["rancher", "argocd"]


def test_after_naming_an_uninstalled_plugin_is_ignored(monkeypatch):
    """AFTER is a wish, not a dependency: argocd alone must still work."""
    install(monkeypatch, FakeEntryPoint("argocd", make_module("argocd", after=("rancher",))))
    assert [p.name for p in plugins.discover()] == ["argocd"]


def test_ties_break_alphabetically(monkeypatch):
    install(
        monkeypatch,
        FakeEntryPoint("zulu", make_module("zulu")),
        FakeEntryPoint("alpha", make_module("alpha")),
    )
    assert [p.name for p in plugins.discover()] == ["alpha", "zulu"]


def test_cycle_warns_and_falls_back(monkeypatch, capsys):
    install(
        monkeypatch,
        FakeEntryPoint("a", make_module("a", after=("b",))),
        FakeEntryPoint("b", make_module("b", after=("a",))),
    )
    assert [p.name for p in plugins.discover()] == ["a", "b"]
    assert "cycle" in capsys.readouterr().err


# ---- active ----------------------------------------------------------------

def test_active_skips_unconfigured(monkeypatch, ctx):
    install(
        monkeypatch,
        FakeEntryPoint("on", make_module("on", configured=lambda ctx: True)),
        FakeEntryPoint("off", make_module("off", configured=lambda ctx: False)),
    )
    assert [p.name for p in plugins.active(ctx)] == ["on"]


# ---- fan-out ---------------------------------------------------------------

def test_run_calls_the_hook(monkeypatch, ctx):
    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", converge=lambda ctx, assume_yes=False: seen.append("a"))))
    assert plugins.run(plugins.discover(), "converge", ctx, assume_yes=False) == 0
    assert seen == ["a"]


def test_run_skips_a_hook_the_plugin_does_not_implement(monkeypatch, ctx):
    install(monkeypatch, FakeEntryPoint("a", make_module("a")))
    assert plugins.run(plugins.discover(), "destroy", ctx) == 0


def test_one_failing_plugin_does_not_stop_the_others(monkeypatch, ctx, capsys):
    seen = []

    def boom(ctx, assume_yes=False):
        raise RuntimeError("nope")

    install(
        monkeypatch,
        FakeEntryPoint("a_bad", make_module("a_bad", converge=boom)),
        FakeEntryPoint("b_good", make_module(
            "b_good", converge=lambda ctx, assume_yes=False: seen.append("b"))),
    )
    rc = plugins.run(plugins.discover(), "converge", ctx, assume_yes=False)
    assert rc == 1                 # the command reports the failure...
    assert seen == ["b"]           # ...but the healthy plugin still ran
    assert "a_bad" in capsys.readouterr().err


def test_initialize_calls_installed_plugin_init_hooks(monkeypatch, tmp_path):
    seen = []
    install(
        monkeypatch,
        FakeEntryPoint("with_init", make_module("with_init", init=seen.append)),
        FakeEntryPoint("without_init", make_module("without_init")),
    )
    plugins.initialize(tmp_path)
    assert seen == [tmp_path]


def test_initialize_contains_plugin_failures(monkeypatch, tmp_path, capsys):
    seen = []

    def boom(root):
        raise RuntimeError("nope")

    install(
        monkeypatch,
        FakeEntryPoint("a_bad", make_module("a_bad", init=boom)),
        FakeEntryPoint("b_good", make_module("b_good", init=seen.append)),
    )
    plugins.initialize(tmp_path)
    assert seen == [tmp_path]
    assert "a_bad" in capsys.readouterr().err


def test_converge_results_reach_the_next_plugin(monkeypatch, ctx):
    """The channel between plugins: rancher publishes, argocd consumes."""
    seen = {}

    def first(ctx, assume_yes=False):
        return {"cluster_id": "c-12345"}

    def second(ctx, assume_yes=False):
        seen.update(ctx.results.get("first", {}))

    install(
        monkeypatch,
        FakeEntryPoint("first", make_module("first", converge=first)),
        FakeEntryPoint("second", make_module("second", after=("first",), converge=second)),
    )
    plugins.run(plugins.discover(), "converge", ctx, assume_yes=False)
    assert ctx.results["first"] == {"cluster_id": "c-12345"}
    assert seen == {"cluster_id": "c-12345"}


# ---- collect ---------------------------------------------------------------

def test_collect_gathers_reports(monkeypatch, ctx):
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", check=lambda ctx: {"ok": True, "detail": 1})))
    assert plugins.collect(plugins.discover(), "check", ctx) == {
        "a": {"ok": True, "detail": 1}
    }


def test_collect_records_a_failure_rather_than_dropping_it(monkeypatch, ctx):
    def boom(ctx):
        raise RuntimeError("unreachable")

    install(monkeypatch, FakeEntryPoint("a", make_module("a", check=boom)))
    report = plugins.collect(plugins.discover(), "check", ctx)
    assert report["a"]["ok"] is False
    assert "unreachable" in report["a"]["error"]


# ---- dry-run ---------------------------------------------------------------

def test_dry_run_reaches_plugin_code(monkeypatch, ctx):
    """`plan` sets one process-wide flag and plugin code sees it.

    This is what running in-process buys: the plugins used to be separate
    processes with their own _DRY_RUN global, so a --dry-run on the parent could
    not reach them at all.
    """
    from taloscluster import output

    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", converge=lambda ctx, assume_yes=False: seen.append(
            output.dry_run()))))

    output.set_dry_run(True)
    try:
        plugins.run(plugins.discover(), "converge", ctx, assume_yes=False)
    finally:
        output.set_dry_run(False)

    assert seen == [True]


# ---- `taloscluster plugin list` --------------------------------------------

def test_plugin_list_works_without_a_cluster_yaml(monkeypatch, tmp_path, capsys):
    """"What is installed" is not a question about a cluster, so listing must not
    require a cluster directory."""
    from taloscluster import cli

    install(monkeypatch, FakeEntryPoint("a", make_module("a")))
    assert cli.main(["plugin", "-C", str(tmp_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "a" in out
    assert "cluster.yaml" in out          # says why the column is missing


def test_plugin_list_shows_configured_state_with_a_cluster_yaml(
    monkeypatch, tmp_path, make_config, capsys
):
    from taloscluster import cli

    make_config()  # writes cluster.yaml into tmp_path
    install(
        monkeypatch,
        FakeEntryPoint("on", make_module("on", configured=lambda ctx: True)),
        FakeEntryPoint("off", make_module("off", configured=lambda ctx: False)),
    )
    assert cli.main(["plugin", "-C", str(tmp_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "on           configured" in out
    assert "off          not configured" in out

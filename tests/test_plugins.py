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
from taloscluster.errors import ConfigError


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


# ---- validate --------------------------------------------------------------

def test_validate_skips_plugins_without_the_hook(monkeypatch, ctx):
    install(monkeypatch, FakeEntryPoint("noop", make_module("noop")))
    plugins.validate(ctx)  # no configured plugin calls validate -> no error


def test_validate_consults_every_plugin_with_the_hook(monkeypatch, ctx):
    """Activation can silently discard a supplied-but-invalid config section, so
    validate runs the hook whether or not a plugin is active; the hook itself
    treats an absent section as a no-op."""
    seen = []
    install(
        monkeypatch,
        FakeEntryPoint("on", make_module(
            "on", configured=lambda ctx: True,
            validate=lambda root, ctx: seen.append(root))),
        FakeEntryPoint("off", make_module(
            "off", configured=lambda ctx: False,
            validate=lambda root, ctx: seen.append(root))),
    )
    plugins.validate(ctx)
    assert seen == [ctx.root, ctx.root]


def test_validate_raises_configerror_naming_the_plugin(monkeypatch, ctx):
    def reject(root, ctx):
        raise ConfigError("argocd.git.url missing")

    install(
        monkeypatch,
        FakeEntryPoint("argocd", make_module(
            "argocd", configured=lambda ctx: True, validate=reject)),
    )
    with pytest.raises(ConfigError, match="plugin 'argocd'|plugin \"argocd\""):
        plugins.validate(ctx)


def test_validate_contains_an_unexpected_exception(monkeypatch, ctx):
    def boom(root, ctx):
        raise RuntimeError("exploded")

    install(
        monkeypatch,
        FakeEntryPoint("a", make_module("a", configured=lambda ctx: True, validate=boom)),
    )
    with pytest.raises(ConfigError, match="plugin 'a'|plugin \"a\"") as exc:
        plugins.validate(ctx)
    assert "exploded" in str(exc.value)


def test_validate_rejects_an_inactive_plugin_with_bad_supplied_config(monkeypatch, ctx):
    """A malformed or unsupported config section can make a plugin inactive and
    so escape activation's gating; validation must still refuse it (as the
    ArgoCD plugin does for a non-mapping `argocd:` section or a url/token apply
    target) instead of silently discarding it."""
    def reject(root, ctx):
        raise ConfigError("url/token is not a supported apply target")

    install(
        monkeypatch,
        FakeEntryPoint("argocd", make_module(
            "argocd", configured=lambda ctx: False, validate=reject)),
    )
    with pytest.raises(ConfigError, match="plugin 'argocd'|plugin \"argocd\""):
        plugins.validate(ctx)


# ---- collect ---------------------------------------------------------------

def test_collect_gathers_reports(monkeypatch, ctx):
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", check=lambda ctx: {"ok": True, "detail": 1})))
    assert plugins.collect(plugins.discover(), "check", ctx) == {
        "a": {"ok": True, "detail": 1}
    }


def test_collect_results_reach_the_next_plugin(monkeypatch, ctx):
    """The same cluster identity converge hands downstream plugins must also
    reach them through check/status reports: rancher publishes its cluster_id
    in check/status, argocd consumes it to render manifests."""
    seen = {}

    def first(ctx):
        return {"cluster_id": "c-12345", "ok": True}

    def second(ctx):
        seen.update(ctx.results.get("first", {}))

    install(
        monkeypatch,
        FakeEntryPoint("first", make_module("first", after=(), check=first)),
        FakeEntryPoint("second", make_module("second", after=("first",), check=second)),
    )
    plugins.collect(plugins.discover(), "check", ctx)
    assert ctx.results["first"] == {"cluster_id": "c-12345", "ok": True}
    assert seen == {"cluster_id": "c-12345", "ok": True}


def test_converge_then_check_delivers_the_same_identity(monkeypatch, tmp_path):
    """The todo's regression: a converge that stamps the Rancher id on ArgoCD's
    manifests is followed by a separate `check` that renders the SAME id, so it
    reports the just-applied manifests as current rather than drifted.

    Rancher and argocd are both enabled. Each leg uses a fresh context, as a
    separate `taloscluster` invocation would, so nothing leaks between them:
    the converge leg publishes `cluster_id` into ctx.results and the check leg's
    collect must republish rancher's check report into the new context before
    argocd renders."""
    seen = {}

    def rancher_converge(ctx, assume_yes=False):
        return {"cluster_id": "c-abc12", "members": []}

    def rancher_check(ctx):
        return {"cluster_id": "c-abc12", "ok": True}

    def argocd_converge(ctx, assume_yes=False):
        seen["converge"] = (ctx.results.get("rancher") or {}).get("cluster_id")
        return {"applied": ["secret"], "server": "unused"}

    def argocd_check(ctx):
        seen["check"] = (ctx.results.get("rancher") or {}).get("cluster_id")
        return {"ok": True, "drifted": []}

    install(
        monkeypatch,
        FakeEntryPoint("rancher", make_module(
            "rancher", converge=rancher_converge, check=rancher_check)),
        FakeEntryPoint("argocd", make_module(
            "argocd", after=("rancher",), converge=argocd_converge, check=argocd_check)),
    )

    plugins.run(plugins.discover(), "converge", Context(root=tmp_path, cfg=None))
    plugins.collect(plugins.discover(), "check", Context(root=tmp_path, cfg=None))

    assert seen["converge"] == "c-abc12"
    assert seen["check"] == "c-abc12"


def test_real_rancher_and_argocd_check_hand_off_the_downstream_id_on_a_mismatch(
    monkeypatch, tmp_path
):
    """The real plugins through collect: when the Rancher cluster does not match
    the downstream agent's id, rancher's check publishes the DOWNSTREAM id and
    argocd's check stamps that same id onto its cluster Secret.

    A mismatch (downstream `c-abc12` vs a different Rancher cluster `c-OTHER`)
    is the hard case: rancher must refuse the registration as ours yet still hand
    argocd the id converge would refuse to attach to, so the Secret annotation
    never drifts against the cluster that actually exists downstream."""
    import taloscluster_argocd as _argocd_pkg
    import taloscluster_rancher as _rancher_pkg
    from taloscluster_argocd import kube as argocd_kube
    from taloscluster_rancher import reconcile as rancher_reconcile
    from taloscluster_rancher.client import RancherCluster

    (tmp_path / "cluster.yaml").write_text(
        "name: testcluster\n"
        "include: [secrets.yaml]\n"
        "rancher:\n  admins: []\n  users: []\n"
        "argocd:\n  admins: []\n  users: []\n"
    )
    (tmp_path / "secrets.yaml").write_text(
        "rancher:\n  url: https://200.1.2.3/\n  token: token-x:y\n"
        "argocd:\n  kubeconfig: ./kubeconfig\n"
    )
    (tmp_path / "kubeconfig").write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: testcluster\n"
        "  cluster:\n"
        "    server: https://192.0.2.10:6443\n"
        "    certificate-authority-data: ca==\n"
        "users:\n"
        "- name: admin\n"
        "  user:\n"
        "    client-certificate-data: cert==\n"
        "    client-key-data: key==\n"
        "contexts: []\n"
    )

    class _FakeClient:
        def find_cluster(self, name):
            return RancherCluster(id="c-OTHER", name=name, state="active")

        def list_member_bindings(self, cluster_id):
            return []

        def resolve_principal(self, netid):
            return None

    # the downstream cluster's own agent id, which mismatches Rancher's c-OTHER
    monkeypatch.setattr(rancher_reconcile, "downstream_rancher_id", lambda root: "c-abc12")
    monkeypatch.setattr(rancher_reconcile, "_client", lambda secrets: _FakeClient())

    rendered = {}

    def _matches(target, root, manifest):
        rendered.setdefault("secret", []).append(manifest)
        return True

    monkeypatch.setattr(argocd_kube, "matches", _matches)

    install(
        monkeypatch,
        FakeEntryPoint("rancher", _rancher_pkg),
        FakeEntryPoint("argocd", _argocd_pkg),
    )

    ctx = Context(root=tmp_path, cfg=None, results={})
    report = plugins.collect(plugins.discover(), "check", ctx)

    # rancher refuses the registration but publishes the downstream id it would
    # attach to, so argocd renders that (not the unrelated c-OTHER)
    assert report["rancher"]["ok"] is False
    assert report["rancher"]["id_match"] is False
    assert report["rancher"]["cluster_id"] == "c-abc12"
    assert report["rancher"]["downstream_id"] == "c-abc12"
    # the hand-off survives into argocd's report and its manifests
    assert ctx.results["rancher"]["cluster_id"] == "c-abc12"
    assert report["argocd"]["ok"] is True
    assert "rancher.cattle.io/cluster-id: c-abc12" in rendered["secret"][0]


def test_collect_records_a_failure_rather_than_dropping_it(monkeypatch, ctx):
    def boom(ctx):
        raise RuntimeError("unreachable")

    install(monkeypatch, FakeEntryPoint("a", make_module("a", check=boom)))
    report = plugins.collect(plugins.discover(), "check", ctx)
    assert report["a"]["ok"] is False
    assert "unreachable" in report["a"]["error"]


def test_run_contains_a_Die_raised_by_a_plugin(monkeypatch, ctx, capsys):
    """`Die` is a SystemExit (BaseException), so the usual `except Exception`
    would not contain it; a plugin that dies must not abort the whole run."""
    from taloscluster.output import Die

    seen = []

    def die(ctx, assume_yes=False):
        raise Die("fatal plugin failure")

    install(
        monkeypatch,
        FakeEntryPoint("a_dies", make_module("a_dies", converge=die)),
        FakeEntryPoint("b_good", make_module(
            "b_good", converge=lambda ctx, assume_yes=False: seen.append("b"))),
    )
    rc = plugins.run(plugins.discover(), "converge", ctx, assume_yes=False)
    assert rc == 1                  # the Die is recorded as a failure...
    assert seen == ["b"]            # ...and the healthy plugin still ran
    assert "a_dies" in capsys.readouterr().err


def test_collect_contains_a_Die_raised_by_a_plugin(monkeypatch, ctx, capsys):
    from taloscluster.output import Die

    def die(ctx):
        raise Die("unreachable")

    install(monkeypatch, FakeEntryPoint("a", make_module("a", check=die)))
    report = plugins.collect(plugins.discover(), "check", ctx)
    assert report["a"]["ok"] is False
    assert "unreachable" in report["a"]["error"]


def test_initialize_contains_a_Die_raised_by_a_plugin(monkeypatch, tmp_path, capsys):
    from taloscluster.output import Die

    def die(root):
        raise Die("fatal init")

    install(monkeypatch, FakeEntryPoint("a", make_module("a", init=die)))
    plugins.initialize(tmp_path)    # warns, does not raise
    assert "a" in capsys.readouterr().err


def test_duplicate_entry_point_names_warn_and_keep_the_first(monkeypatch, capsys):
    """_order keys plugins by name, so a duplicate silently collapses to one."""
    install(
        monkeypatch,
        FakeEntryPoint("dup", make_module("dup")),
        FakeEntryPoint("dup", make_module("dup2")),
        FakeEntryPoint("other", make_module("other")),
    )
    assert [p.name for p in plugins.discover()] == ["dup", "other"]
    assert "duplicate" in capsys.readouterr().err


def test_duplicate_entry_point_name_that_fails_to_load_is_not_reported(monkeypatch, capsys):
    """Only a name that actually survives counts: a load-failing duplicate is
    dropped with its own load warning, not a duplicate warning."""
    install(
        monkeypatch,
        FakeEntryPoint("broken", error=ImportError("no such thing")),
        FakeEntryPoint("broken", make_module("broken")),
        FakeEntryPoint("other", make_module("other")),
    )
    assert [p.name for p in plugins.discover()] == ["broken", "other"]
    err = capsys.readouterr().err
    assert "duplicate" not in err
    assert "broken" in err


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


# ---- direct `plugin NAME destroy` confirmation -----------------------------

def test_direct_destroy_declines_without_yes(monkeypatch, tmp_path, make_config):
    """A direct plugin destroy is a deletion, so it needs the same explicit
    approval as `destroy` and `image remove`: typing the cluster name, or --yes."""
    from taloscluster import cli

    make_config()
    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", destroy=lambda ctx, assume_yes=False: seen.append("destroy"))))
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-name")

    with pytest.raises(SystemExit, match="aborted"):
        cli.main(["plugin", "-C", str(tmp_path), "a", "destroy"])
    assert seen == []


def test_direct_destroy_confirms_by_typing_the_cluster_name(monkeypatch, tmp_path, make_config):
    from taloscluster import cli

    make_config()
    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", destroy=lambda ctx, assume_yes=False: seen.append("destroy"))))
    monkeypatch.setattr("builtins.input", lambda _prompt: "testcluster")

    assert cli.main(["plugin", "-C", str(tmp_path), "a", "destroy"]) == 0
    assert seen == ["destroy"]


def test_direct_destroy_dry_run_skips_prompt_but_runs_hook(
    monkeypatch, tmp_path, make_config
):
    from taloscluster import cli, output

    make_config()
    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", destroy=lambda ctx, assume_yes=False: seen.append("destroy"))))
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--dry-run must not prompt"))

    output.set_dry_run(True)
    try:
        assert cli.main(["plugin", "-C", str(tmp_path), "a", "destroy", "--dry-run"]) == 0
    finally:
        output.set_dry_run(False)

    assert seen == ["destroy"]


def test_direct_destroy_yes_skips_the_prompt(monkeypatch, tmp_path, make_config):
    from taloscluster import cli

    make_config()
    seen = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", destroy=lambda ctx, assume_yes=False: seen.append("destroy"))))
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt"))

    assert cli.main(["plugin", "-C", str(tmp_path), "a", "destroy", "--yes"]) == 0
    assert seen == ["destroy"]


# ---- standalone converge/plan run validation first -------------------------

def test_validate_can_narrow_to_a_single_plugin(monkeypatch, ctx):
    """The standalone plugin commands validate only the plugin being run, so a
    malformed section on some other installed plugin does not block it."""
    seen = []
    install(
        monkeypatch,
        FakeEntryPoint("good", make_module(
            "good", configured=lambda ctx: True,
            validate=lambda root, ctx: seen.append(root))),
        FakeEntryPoint("bad", make_module(
            "bad", configured=lambda ctx: True,
            validate=lambda root, ctx: pytest.fail("must not run"))),
    )
    plugins.validate(ctx, [p for p in plugins.discover() if p.name == "good"])
    assert seen == [ctx.root]


def test_direct_converge_rejects_bad_config_before_running(monkeypatch, tmp_path, make_config):
    """`taloscluster plugin NAME converge` applies the same early validation
    converge does: a plugin section that the `validate` hook rejects stops the
    run with a non-zero exit before the mutating hook runs."""
    from taloscluster import cli

    make_config()
    ran = []

    def reject(root, ctx):
        raise ConfigError("argocd.infra.url must be set together with git.url")

    install(monkeypatch, FakeEntryPoint(
        "argocd", make_module(
            "argocd",
            configured=lambda ctx: True,
            validate=reject,
            converge=lambda ctx, assume_yes=False: ran.append("converge"))))

    assert cli.main(["plugin", "-C", str(tmp_path), "argocd", "converge"]) == 1
    assert ran == []


def test_direct_plan_rejects_bad_config_before_running(monkeypatch, tmp_path, make_config):
    """`plugin NAME plan` (converge --dry-run) also validates first, so a lone
    repository URL or unsupported override is refused while planning."""
    from taloscluster import cli, output

    make_config()
    ran = []

    def reject(root, ctx):
        raise ConfigError("unsupported option")

    install(monkeypatch, FakeEntryPoint(
        "argocd", make_module(
            "argocd",
            configured=lambda ctx: True,
            validate=reject,
            converge=lambda ctx, assume_yes=False: ran.append("converge"))))

    output.set_dry_run(True)
    try:
        assert cli.main(["plugin", "-C", str(tmp_path), "argocd", "plan"]) == 1
    finally:
        output.set_dry_run(False)
    assert ran == []


def test_direct_converge_runs_when_config_is_valid(monkeypatch, tmp_path, make_config):
    """Passing validation is a precondition, not a block: a valid config still
    reaches the mutating hook."""
    from taloscluster import cli

    make_config()
    ran = []
    install(monkeypatch, FakeEntryPoint(
        "a", make_module(
            "a",
            configured=lambda ctx: True,
            validate=lambda root, ctx: None,
            converge=lambda ctx, assume_yes=False: ran.append("converge"))))

    assert cli.main(["plugin", "-C", str(tmp_path), "a", "converge"]) == 0
    assert ran == ["converge"]


def test_direct_destroy_validates_before_teardown(monkeypatch, tmp_path, make_config):
    """A standalone mutating destroy also validates before removing resources."""
    from taloscluster import cli

    make_config()
    ran = []

    def reject(root, ctx):
        raise ConfigError("bad plugin config")

    install(monkeypatch, FakeEntryPoint(
        "a", make_module(
            "a",
            configured=lambda ctx: True,
            validate=reject,
            destroy=lambda ctx, assume_yes=False: ran.append("destroy"))))

    assert cli.main(["plugin", "-C", str(tmp_path), "a", "destroy", "--yes"]) == 1
    assert ran == []


# ---- warning-only status/check errors --------------------------------------

def test_direct_status_plugin_error_is_warning_only(monkeypatch, tmp_path, make_config, capsys):
    """A plugin whose status hook raises stays informational: the error appears in
    the report and the command still exits 0, mirroring core status."""
    from taloscluster import cli

    make_config()

    def boom(ctx):
        raise RuntimeError("unreachable")

    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", status=boom)))
    assert cli.main(["plugin", "-C", str(tmp_path), "a", "status"]) == 0
    err = capsys.readouterr().err
    assert "a" in err
    assert "unreachable" in err


def test_direct_check_plugin_error_exits_nonzero(monkeypatch, tmp_path, make_config):
    """A plugin whose check hook errors is a report that needs attention, so the
    direct `plugin NAME check` exits 1, unlike status."""
    from taloscluster import cli

    make_config()

    def boom(ctx):
        raise RuntimeError("unreachable")

    install(monkeypatch, FakeEntryPoint(
        "a", make_module("a", check=boom)))
    assert cli.main(["plugin", "-C", str(tmp_path), "a", "check"]) == 1

"""Keep the troubleshooting guide in lockstep with the code's failure diagnostics.

The guide's whole point is telling the operator what each failure *looks like*
and how to clear it. If the code rewords a diagnostic (the missing-secrets hard
fail, the scale-down drain abort, the incomplete-check reasons, the plugin
failure warnings, the stale-cordon message), the guide must quote the same
string. These tests pin the guide to the actual messages raised or printed by
``taloscluster.converge`` and ``taloscluster.plugins``.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "troubleshooting.md"
USAGE = Path(__file__).resolve().parent.parent / "docs" / "usage.md"
QUICKSTART = Path(__file__).resolve().parent.parent / "docs" / "quickstart.md"
CONVERGE = Path(__file__).resolve().parent.parent / "taloscluster" / "converge.py"
PLUGINS = Path(__file__).resolve().parent.parent / "taloscluster" / "plugins.py"
KUBECTL = Path(__file__).resolve().parent.parent / "taloscluster" / "k8s" / "kubectl.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "troubleshooting.md" in nav.read_text()


def test_guide_covers_all_five_topics():
    text = GUIDE.read_text().lower()
    assert "missing talos secrets" in text
    assert "drain fails during scale-down" in text
    assert "check` is incomplete" in text
    assert "plugin fails" in text
    assert "interrupted upgrade" in text


def test_missing_secrets_quotes_the_converge_refusal():
    # Converge hard-fails when talossecrets.yaml is missing but machines exist;
    # the guide must quote the same "restore it from backup" language so the
    # operator recognizes the message rather than retrying it. The full message
    # is built across two source-line literals, so pin the shared fragment
    # that lands on a single line in both the guide and the code.
    message = "regenerated for an existing cluster -- restore it from backup"
    assert message in GUIDE.read_text()
    assert message in CONVERGE.read_text()


def test_missing_secrets_points_at_backup_recovery():
    text = GUIDE.read_text()
    assert "backup.md#talos-identity" in text
    assert "mode 0600" in text


def test_drain_abort_matches_converge():
    # A blocked scale-down drain while the node is Ready aborts to protect a
    # live node; the guide quotes the exact message.
    message = (
        "drain of mycluster-worker-02 failed and node is Ready; aborting to "
        "protect a potentially live node"
    )
    assert message in GUIDE.read_text()
    assert "aborting to protect a potentially live node" in CONVERGE.read_text()


def test_drain_recovery_cross_references_maintenance():
    text = GUIDE.read_text()
    assert "maintenance.md#blocked-drain-what-you-see-and-the-resolution" in text
    assert "PodDisruptionBudget" in text
    assert "bare" in text


def test_incomplete_check_matches_the_reasons():
    # An incomplete check warns `check incomplete: <reason>` and reports the
    # two-sided gaps (unreachable upstream, unknown node version, unreachable
    # cluster). Pin the guide to the code's reason phrasing.
    text = GUIDE.read_text()
    assert "check incomplete:" in text
    assert "incomplete_reasons" in text
    assert "cluster unreachable; no node versions known" in text
    assert "cluster unreachable; no node versions known" in CONVERGE.read_text()


def test_plugin_failure_matches_plugins():
    # Plugin failures are contained (reported, others still run, nonzero exit);
    # the guide quotes core's warning strings so the operator recognizes them.
    text = GUIDE.read_text()
    assert "failed during <hook>" in text
    assert "could not be loaded" in text
    assert "failed during" in PLUGINS.read_text()
    assert "could not be loaded" in PLUGINS.read_text()


def test_interrupted_upgrade_matches_stale_cordon_behavior():
    # An interrupted upgrade leaves a node SchedulingDisabled; the guide says
    # converge lifts stale cordons and hands kubectl uncordon as the fallback.
    text = GUIDE.read_text()
    assert "SchedulingDisabled" in text
    assert "kubectl uncordon" in text
    stale = "leftover from the upgrade" in CONVERGE.read_text()
    watch = "client-side watch dies" in KUBECTL.read_text()
    assert stale and watch


def test_no_stale_unavailable_data_can_pass_claims():
    # An incomplete check (unreachable upstream, unknown node version, missing
    # or unreachable nodes) is not a clean bill of health: it exits 1 rather
    # than passing a CI gate with unverified data. The guide and usage must not
    # drift back to the old claim that unavailable data can still exit 0, and
    # the superseded "check succeeds while version data is unavailable" section
    # must stay gone.
    guide = GUIDE.read_text()
    usage = USAGE.read_text()
    assert "check` succeeds while version data is unavailable" not in guide
    for doc in (guide, usage):
        assert "Unavailable upstream or node-version data can still yield exit status 0" not in doc
    # the aligned contract: nothing unverifiable passes as current
    assert "An incomplete check exits `1`" in guide
    assert "incomplete" in usage and "exits 1" in usage


def test_quickstart_has_no_prequisite_plugin_workaround():
    # Plugins are configured from the start; their planning hooks run in dry-run
    # and report work that needs the cluster/kubeconfig as deferred until
    # converge bootstraps the cluster. The old "leave plugins inactive until the
    # cluster exists" workaround is obsolete now that pre-bootstrap plugin
    # planning work is deferred.
    text = QUICKSTART.read_text().lower()
    assert "leave plugins inactive" not in text
    assert "deferred until converge bootstraps the cluster" in text

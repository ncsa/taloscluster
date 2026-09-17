"""Keep the troubleshooting guide in lockstep with the code's failure diagnostics.

The guide's whole point is telling the operator what each failure *looks like*
and how to clear it. If the code rewords a diagnostic (the missing-secrets hard
fail, the scale-down drain abort, the incomplete-check reasons, the plugin
failure warnings, the stale-cordon message), the guide must quote the same
string. These tests pin the guide to the actual messages raised or printed by
``taloscluster.converge`` and ``taloscluster.plugins``.
"""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "troubleshooting.md"
MAINTENANCE = Path(__file__).resolve().parent.parent / "docs" / "maintenance.md"
USAGE = Path(__file__).resolve().parent.parent / "docs" / "usage.md"
QUICKSTART = Path(__file__).resolve().parent.parent / "docs" / "quickstart.md"
CONVERGE = Path(__file__).resolve().parent.parent / "taloscluster" / "converge.py"
PLUGINS = Path(__file__).resolve().parent.parent / "taloscluster" / "plugins.py"
KUBECTL = Path(__file__).resolve().parent.parent / "taloscluster" / "k8s" / "kubectl.py"
PROXMOX_BACKEND = Path(__file__).resolve().parent.parent / "taloscluster" / "proxmox" / "backend.py"
RANCHER_RECONCILE = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "rancher"
    / "taloscluster_rancher"
    / "reconcile.py"
)
LIFECYCLE = Path(__file__).resolve().parent.parent / "docs" / "concepts" / "lifecycle.md"


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


def test_guide_covers_the_new_fail_closed_topics():
    text = GUIDE.read_text().lower()
    assert "still an etcd member" in text
    assert "kubernetes version it cannot read" in text
    assert "duplicate proxmox vm names" in text
    assert "cannot resolve a configured member" in text
    assert "cluster id no longer matches the downstream agent" in text
    assert "shared sdn controller changes are pending" in text


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


def test_tight_pdb_resolution_loosens_eviction():
    # A PDB that refuses to go below `minAvailable` is unblocked by making
    # eviction easier, never harder: lower `minAvailable` or raise
    # `maxUnavailable` so one node can leave, or add replicas above the PDB
    # floor. The guide must not recommend raising `minAvailable`, which only
    # tightens the budget and blocks the drain further. The same resolution is
    # given in maintenance.md and matches the Kubernetes PDB definition.
    text = GUIDE.read_text()
    assert "lower `minAvailable`" in text
    assert "raise `maxUnavailable`" in text
    assert "add replicas above the PDB floor" in text
    # raising minAvailable makes eviction harder - it must never be advised
    assert "Raise `minAvailable`" not in text
    assert not re.search(r"raise `?minAvailable", text, re.IGNORECASE)
    # maintenance.md documents the same loosening resolution
    maint = MAINTENANCE.read_text()
    assert "raise `replicas` above the PDB floor" in maint
    assert "lower `minAvailable`/raise `maxUnavailable`" in maint


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
    assert "lifts stale cordons on its own" in text
    stale = "leftover from the upgrade" in CONVERGE.read_text()
    watch = "client-side watch dies" in KUBECTL.read_text()
    assert stale and watch


def test_guide_kubectl_commands_use_the_generated_kubeconfig():
    # The drain diagnostics and the uncordon fallback run kubectl; each must
    # point at `./kubeconfig`, not a bare kubectl reading the environment or
    # home cluster config.
    text = GUIDE.read_text()
    assert "kubectl --kubeconfig kubeconfig describe pod" in text
    assert "kubectl --kubeconfig kubeconfig get pdb" in text
    assert "kubectl --kubeconfig kubeconfig uncordon NODE" in text


def test_guide_talosctl_health_uses_the_generated_talosconfig():
    # The half-upgraded-control-plane diagnostic runs `talosctl ... health` and
    # must point at `./talosconfig`, not a bare talosctl reaching the
    # environment or home config.
    text = GUIDE.read_text()
    assert "talosctl --talosconfig talosconfig -n NODE health" in text


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


def test_etcd_member_control_plane_refusal_quotes_converge():
    # A control-plane removal whose reset failed (known address, dead/wiped
    # node) or whose address is gone refuses to delete a member that could cost
    # quorum; the guide must quote the same language as the converge refusal.
    assert "still an etcd member" in GUIDE.read_text()
    assert "could cost quorum" in GUIDE.read_text()
    assert "delete a member that could cost quorum" in CONVERGE.read_text()


def test_unknown_k8s_version_refusal_matches_converge():
    # A reachable kube-api that will not answer a version query aborts config
    # mutation; the guide quotes the exact refusal.
    message = "could not determine the running cluster's kubernetes version"
    assert message in GUIDE.read_text()
    assert message in CONVERGE.read_text()
    assert "refusing to generate machine configs against an unknown version" in GUIDE.read_text()


def test_duplicate_proxmox_vm_names_matches_backend():
    # Two VMIDs sharing a name that involves a cluster-managed machine are
    # refused before any mutation; the guide quotes the inventory refusal.
    # The backend composes the message across source lines; pin the shared
    # fragment that lands on a single line in both the guide and the code.
    assert "duplicate Proxmox VM names among" in GUIDE.read_text()
    assert "duplicate Proxmox VM names among" in PROXMOX_BACKEND.read_text()


def test_sdn_shared_controller_pending_matches_backend():
    # SDN teardown refuses a pending `deleted`/`changed` on the shared controller
    # before any VM or pool is deleted; the guide quotes the refusal.
    text = GUIDE.read_text()
    backend = PROXMOX_BACKEND.read_text()
    assert "refusing to commit pending SDN state on the shared controller" in text
    assert "refusing to commit pending SDN state on the shared" in backend
    assert "apply or revert them first" in text
    assert "apply or revert them first" in backend


def test_rancher_unresolved_member_matches_reconcile():
    # A configured member that cannot be resolved refuses the reconciliation
    # rather than removing an existing binding as stale; the guide quotes it.
    text = GUIDE.read_text()
    reconcile = RANCHER_RECONCILE.read_text()
    assert "could not resolve Rancher principals for configured member(s)" in text
    assert "could not resolve Rancher principals for configured member(s)" in reconcile
    assert "not removed as stale" in text
    assert "removed as stale" in reconcile


def test_rancher_id_mismatch_matches_reconcile():
    # A downstream cattle-cluster-agent whose id matches no Rancher cluster is
    # refused by converge and reported with id_match/id_mismatch_reason by
    # check/status; the guide names id_mismatch_reason and quotes the reason.
    text = GUIDE.read_text()
    assert "id_mismatch_reason" in text
    assert "id_match: false" in text
    assert "does not match the downstream cluster" in text
    assert "does not match the downstream cluster" in RANCHER_RECONCILE.read_text()


def test_scale_down_removes_unjoined_or_failed_delete_machines():
    # Scale-down reconciles from the owned provider inventory, so owned machines
    # that never joined Kubernetes or whose VM delete failed on an earlier run
    # are removed on the next converge. Usage must say so, matching lifecycle.
    usage = USAGE.read_text()
    lifecycle = LIFECYCLE.read_text()
    assert "never joined kubernetes" in usage.lower()
    assert "vm delete failed" in usage.lower()
    assert "never registered" in lifecycle and "delete failed" in lifecycle

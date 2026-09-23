"""Keep the troubleshooting guide quoting the code's failure diagnostics.

The guide's whole point is telling the operator what each failure *looks like*.
When the code rewords a diagnostic -- the missing-secrets hard fail, the
scale-down drain abort, the incomplete-check reasons, the plugin failure
warnings, the etcd-member and upgrade refusals, the Proxmox inventory and SDN
refusals, the Rancher plugin refusals -- the guide must quote the same string.
These tests pin the guide to the actual messages raised or printed by
``taloscluster.converge``, ``taloscluster.plugins``, the Proxmox backend and the
Rancher plugin, and keep its kubectl/talosctl commands pointed at the generated
configs.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "troubleshooting.md"
CONVERGE = Path(__file__).resolve().parent.parent / "taloscluster" / "converge.py"
TALOSCTL = Path(__file__).resolve().parent.parent / "taloscluster" / "talos" / "talosctl.py"
PLUGINS = Path(__file__).resolve().parent.parent / "taloscluster" / "plugins.py"
PROXMOX_BACKEND = Path(__file__).resolve().parent.parent / "taloscluster" / "proxmox" / "backend.py"
RANCHER_RECONCILE = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "rancher"
    / "taloscluster_rancher"
    / "reconcile.py"
)
RANCHER_CLIENT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "rancher"
    / "taloscluster_rancher"
    / "client.py"
)


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "troubleshooting.md" in nav.read_text()


def test_missing_secrets_quotes_the_converge_refusal():
    # Converge hard-fails when talossecrets.yaml is missing but machines exist;
    # the guide must quote the same "restore it from backup" language so the
    # operator recognizes the message rather than retrying it. The full message
    # is built across two source-line literals, so pin the shared fragment
    # that lands on a single line in both the guide and the code.
    message = "regenerated for an existing cluster -- restore it from backup"
    assert message in GUIDE.read_text()
    assert message in CONVERGE.read_text()


def test_drain_abort_matches_converge():
    # A blocked scale-down drain while the node is Ready aborts to protect a
    # live node; the guide quotes the exact message.
    message = (
        "drain of mycluster-worker-02 failed and node is Ready; aborting to "
        "protect a potentially live node"
    )
    assert message in GUIDE.read_text()
    assert "aborting to protect a potentially live node" in CONVERGE.read_text()


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


def test_etcd_member_control_plane_refusal_quotes_converge():
    # A control-plane removal whose reset failed (known address, dead/wiped
    # node) or whose address is gone refuses to delete a member that could cost
    # quorum; the guide must quote the same language as the converge refusal.
    # A reset that fails while the Kubernetes Node still exists aborts with
    # talosctl reset's own message, so the guide must present that abort (and
    # its quoted refusal) rather than claiming the etcd fallthrough always
    # applies.
    guide = GUIDE.read_text()
    talosctl = TALOSCTL.read_text()
    assert "still an etcd member" in guide
    assert "could cost quorum" in guide
    assert "delete a member that could cost quorum" in CONVERGE.read_text()
    assert "refusing to delete it -- a half-reset " in talosctl
    assert "control plane is a dead etcd member" in talosctl
    assert "refusing to delete it -- a half-reset control plane is a dead etcd member" in guide


def test_upgrade_abort_diagnostics_match_converge():
    # A Kubernetes upgrade that cannot read the running version aborts instead
    # of silently skipping. The guide quotes the converge abort it is describing.
    guide = GUIDE.read_text()
    converge = CONVERGE.read_text()
    assert "resolved; cannot perform a kubernetes upgrade" in guide
    assert "resolved; cannot perform a kubernetes upgrade" in converge


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
    # before any VM or pool is deleted; the guide quotes the refusal verbatim,
    # including the pending state the code must keep on the descriptor.
    text = GUIDE.read_text()
    backend = PROXMOX_BACKEND.read_text()
    assert (
        "refusing to commit pending SDN state on the shared controller "
        "controller-01 (changed); teardown never deletes the controller and "
        "its staged edits are cluster-wide, so apply or revert them first"
    ) in text
    # the teardown message embeds the shared-controller descriptor verbatim, so
    # it keeps the `(<state>)` the guide quotes; dropping the state must fail.
    assert "refusing to commit pending SDN state on the shared" in backend
    assert '"{shared}; teardown never deletes the controller' in backend
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


def test_rancher_orphan_refusal_matches_its_own_source():
    # The guide's orphaned-agent section quotes the refusal converge actually
    # prints -- the ensure_cluster refusal in client.py -- and presents the
    # `orphan_reason`, which is a *different* string, as what check/status
    # report. Each fragment is pinned to its own source so the guide cannot
    # attribute one code path's message to the other again.
    guide = GUIDE.read_text()
    client = RANCHER_CLIENT.read_text()
    reconcile = RANCHER_RECONCILE.read_text()
    assert "uninstall the orphaned agent, then re-run" in guide
    assert "uninstall the orphaned agent, then re-run" in client
    assert "re-register the cluster under a fresh id" in guide
    assert "re-register the cluster under a fresh id" in reconcile
    assert "orphan_reason:" in guide


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

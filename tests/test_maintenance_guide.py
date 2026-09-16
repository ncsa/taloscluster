"""Keep the maintenance walkthrough in lockstep with the code's drain and sizing behaviour.

The guide's whole point is the contract between ``converge``'s scale-down drain
and the workloads on the cluster: capacities, PDBs, scheduling and storage must
let a node's pods be evicted, and a drain that cannot proceed while the node is
Ready aborts instead of deleting a live node. These tests pin the guide to the
actual ``kubectl drain`` flags, to the abort message ``converge`` raises, and to
the fact that "two eligible workers" is a recommendation, not an enforced
configuration requirement.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "maintenance.md"
CONVERGE = Path(__file__).resolve().parent.parent / "taloscluster" / "converge.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "maintenance.md" in nav.read_text()


def test_guide_recommends_two_eligible_workers():
    text = GUIDE.read_text()
    assert "at least two eligible workers" in text
    assert "spare worker capacity" in text.lower() or "worker capacity" in text


def test_two_workers_stays_a_recommendation_not_a_requirement():
    # The guide must present two eligible workers as guidance. Make sure it does
    # not claim converge enforces a minimum of two, and that the code really does
    # accept a single-worker pool so the recommendation never hard-fails.
    text = GUIDE.read_text()
    assert "recommendation" in text.lower()
    assert "not an enforcement" in text.lower()
    assert "a worker pool may have one node" in text


def test_guide_matches_the_drain_abort_message():
    # A blocked drain is the centrepiece; the guide must show the same message
    # converge raises to mark the node intact.
    message = (
        "drain of mycluster-worker-02 failed and node is Ready; aborting to "
        "protect a potentially live node"
    )
    assert message in GUIDE.read_text()
    assert "aborting to protect a potentially live node" in CONVERGE.read_text()


def test_guide_matches_the_drain_flags():
    text = GUIDE.read_text()
    assert "--ignore-daemonsets" in text
    assert "--delete-emptydir-data" in text


def test_guide_does_not_claim_emptydir_pods_are_skipped():
    # With --delete-emptydir-data, emptyDir pods are EVICTED (their data wiped),
    # not left on the node. DaemonSet and static/mirror pods, by contrast, are
    # SKIPPED by the drain, not evicted. The guide must say each correctly.
    text = GUIDE.read_text()
    assert "--delete-emptydir-data" in text
    assert "pods using `emptyDir` volumes are evicted" in text
    assert "their data deleted with them" in text
    assert "pods managed by DaemonSets and mirror or other static pods are skipped" in text


def test_guide_names_bare_pods_as_a_drain_error():
    # A pod with no controller is the other real drain failure: kubectl refuses
    # without --force instead of leaving it stranded.
    text = GUIDE.read_text()
    assert "no controller" in text
    assert "--force" in text


def test_guide_says_too_few_replicas_does_not_block_the_drain():
    # A single replica (or no eligible node) does not block eviction - the pod is
    # evicted and the replacement lands Pending. Only a too-tight PDB and bare
    # pods actually block a drain while the node is Ready.
    text = GUIDE.read_text()
    assert "does not block its own drain" in text
    assert "do not block the drain" in text
    assert "goes `Pending`" in text
    assert "Only two things really block a drain" in text
    assert "PDB too tight" in text
    assert "Bare pod" in text


def test_guide_distinguishes_reattachable_from_pinned_storage():
    # RWO network storage does not block the drain; eviction does not check
    # volume attachability, the drain completes, and the relocated pod must
    # reattach. Local-path/topology-pinned volumes do not block eviction either
    # - the pod is evicted but its replacement goes Pending with the data
    # stranded on the departed node.
    text = GUIDE.read_text().lower()
    assert "does not check volume attachability" in text
    assert "relocated pod" in text
    assert "local-path or topology-pinned" in text
    assert "does not stop a drain" in text
    assert "goes `pending`" in text


def test_guide_covers_the_four_blocker_categories():
    text = GUIDE.read_text().lower()
    assert "poddisruptionbudget" in text
    assert "replicas" in text
    assert "nodeSelector" in text.lower() or "scheduling" in text
    assert "storage" in text
    assert "blocked drain" in text


def test_guide_kubectl_commands_use_the_generated_kubeconfig():
    # Every kubectl command in the guide must point at `./kubeconfig`, otherwise
    # bare kubectl reads the cluster kubeconfig from the environment or home. The
    # drain-abort how-to and the maintenance checklist both run kubectl.
    text = GUIDE.read_text()
    assert "kubectl --kubeconfig kubeconfig describe pod" in text
    assert "kubectl --kubeconfig kubeconfig get pdb" in text
    assert "kubectl --kubeconfig kubeconfig uncordon NODE" in text
    assert "kubectl --kubeconfig kubeconfig get pods -A -o wide" in text
    assert "kubectl --kubeconfig kubeconfig get pdb --all-namespaces" in text


def test_guide_has_no_bare_kubectl_commands():
    # A kubectl invocation without `--kubeconfig` establishes no KUBECONFIG and
    # hits the environment or home cluster by default, so the guide must not
    # hand the operator a bare one. Only prose references to `kubectl` (no flag
    # and no verb) are allowed.
    text = GUIDE.read_text()
    for word in ("get ", "describe ", "uncordon ", "drain "):
        assert f"`kubectl {word}" not in text, f"bare kubectl found: `kubectl {word}`"

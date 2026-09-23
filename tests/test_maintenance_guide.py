"""Keep the maintenance guide quoting the code's real drain diagnostics.

The guide's drain walkthrough must quote the message ``converge`` actually
raises when a blocked drain aborts, and every kubectl command it hands the
operator must point at the generated ``./kubeconfig``. Its prose may otherwise
be reworded freely.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "maintenance.md"
CONVERGE = Path(__file__).resolve().parent.parent / "taloscluster" / "converge.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "maintenance.md" in nav.read_text()


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

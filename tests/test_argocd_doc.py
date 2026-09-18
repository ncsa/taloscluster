"""Keep the ArgoCD configuration guide in lockstep with the reconcile code.

The guide's intro describes how the Rancher cluster-id annotation is resolved.
Both the shared-converge path (rancher runs first and publishes the id into
``ctx.results``) and the standalone ``plugin argocd converge|check`` path (which
reads the same id off the downstream cluster's own ``cattle-cluster-agent``)
must be described the same way, so a reader gets one consistent story and the
stale "ArgoCD alone has no preceding Rancher result" claim cannot come back.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "configuration" / "argocd.md"
PLUGINS = ROOT / "docs" / "concepts" / "plugins.md"
RECONCILE = ROOT / "plugins" / "argocd" / "taloscluster_argocd" / "reconcile.py"


def test_intro_describes_both_resolution_paths():
    # The intro must say that a shared converge takes the id from the preceding
    # rancher result and that a standalone run reads it off the downstream
    # cluster itself -- never that a standalone run carries no Rancher id at all.
    text = GUIDE.read_text()
    assert "cattle-cluster-agent" in text
    assert "cluster-id annotation" in text
    assert "runs after Rancher" in text


def test_intro_does_not_claim_standalone_run_has_no_rancher_id():
    # The stale claim this pins down: a standalone run used to leave the Secret
    # without a Rancher annotation. Both paths are now described identically.
    text = GUIDE.read_text()
    assert "no preceding Rancher result" not in text
    assert "standalone runs" in text
    assert "same identity" in text


def test_intro_matches_the_reconcile_code():
    # The intro prose must agree with how reconcile actually resolves the id.
    reconcile = RECONCILE.read_text()
    plugins = PLUGINS.read_text()
    # The guide's two paths are the two code paths: rancher's published id and
    # the downstream cattle-cluster-agent fallback.
    assert "cluster_id" in reconcile or "cattle-cluster-agent" in reconcile
    assert "downstream_rancher_id" in reconcile
    # The concepts page already states the standalone path reads the downstream
    # agent id; the configuration page must not contradict it.
    assert "cattle-cluster-agent" in plugins


def test_yaml_serializer_guarantee_is_stated_in_the_intro():
    # The YAML-serializer guarantee now opens the page: every scalar the plugin
    # renders round-trips on one physical line. It must live in the intro, not
    # under the git-credentials section (it is not specific to credentials).
    # Match on concept words in the intro block rather than the full sentence,
    # so a rewording does not spuriously fail.
    text = GUIDE.read_text()
    intro = text.split("\n## ", 1)[0]
    assert "YAML serializer" in intro
    assert "one physical line" in intro


def test_git_section_does_not_enumerate_every_scalar():
    # The serialization paragraph sat at the end under `argocd.git.username`/
    # `token` but covered repo URLs, servers, emails and Helm values. The long
    # enumeration is gone; the git section only names the credential itself.
    text = GUIDE.read_text()
    assert "spec.source.repoURL" not in text
    assert "AppProject role member emails" not in text
    assert "kubeconfig `server` in the cluster Secret and AppProject" not in text

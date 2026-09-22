"""Keep the live-workflow test and its guide in lockstep with the CLI and schema.

The live workflow test is the only path that runs the real ``taloscluster``
against a provider (it takes live credentials, so it never runs in CI), but the
guide and the script can still drift: a subcommand that stops being registered,
a ``kubectl`` call that stops pointing at the generated ``kubeconfig``, or a
``cluster.yaml`` key the harness edits that is no longer part of the schema.
These tests keep the harness and its documentation pinned to the actual CLI and
to the config schema that ``scaffold.init`` writes, the same way the docs-audit
and maintenance-guide tests pin the other operational guides.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "testing.md"
HARNESS = ROOT / "scripts" / "live-workflow-test.sh"
MK_DOCS = ROOT / "mkdocs.yml"
SCAFFOLD = ROOT / "taloscluster" / "scaffold.py"
CLI = ROOT / "taloscluster" / "cli.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    assert "testing.md" in MK_DOCS.read_text()


def test_harness_exists_is_executable_and_referenced():
    assert HARNESS.is_file()
    assert HARNESS.stat().st_mode & 0o111, "live-workflow-test.sh must be executable"
    assert "live-workflow-test.sh" in GUIDE.read_text()


def test_harness_shell_syntax_is_valid():
    result = subprocess.run(["bash", "-n", str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_harness_help_exits_cleanly():
    result = subprocess.run([str(HARNESS), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--provider" in result.stdout
    assert "--dir" in result.stdout


def test_guide_taloscluster_verbs_are_registered():
    cli = CLI.read_text()
    registered = set(re.findall(r'sub\.add_parser\(\s*"([a-zA-Z]+)"', cli))
    for m in re.finditer(r"aliases=\[([^\]]*)\]", cli):
        registered.update(a.strip(' "') for a in m.group(1).split(",") if a.strip())
    # init's provider flags are written inline in the guide, not as a subcommand.
    assert registered

    text = GUIDE.read_text()
    # a fenced command like `taloscluster converge`, possibly wrapped.
    patterned = (
        r"(?:`|^|\s)taloscluster\s+"
        r"(init|plan|converge|status|check|destroy|env|image|dashboard|plugin)\b"
    )
    verbs = set(re.findall(patterned, text))
    missing = sorted(v for v in verbs if v not in registered)
    assert not missing, f"guide calls unregistered taloscluster subcommand(s): {missing}"
    # the documented flags the guide relies on are all spelled as the CLI takes them.
    assert "converge --yes" in text or "converge` with `--yes" in text
    assert "destroy --dry-run" in text
    assert "destroy --yes" in text or "destroy` with `--yes" in text


def test_guide_has_no_bare_kubectl_commands():
    # Any kubectl invocation must point at the generated kubeconfig, matching the
    # operational-guide convention: bare kubectl reads the environment or home
    # cluster. The guide holds no command substitution, so only prose mentions
    # are allowed; the harness is checked separately below.
    text = GUIDE.read_text()
    for word in ("get ", "describe ", "apply ", "drain "):
        assert f"`kubectl {word}" not in text, f"bare kubectl found in guide: `kubectl {word}`"


def test_harness_kubectl_targets_the_generated_kubeconfig():
    # Every kubectl invocation the harness runs must use the overridable
    # `KUBECTL` binary and carry the generated kubeconfig (the `-C DIR` cluster
    # directory), never a bare one or one that ignores the configured binary.
    text = HARNESS.read_text()
    assert '"$DIR/kubeconfig"' in text
    for line in text.splitlines():
        if '"$KUBECTL"' in line and "--kubeconfig" not in line:
            # the only "$KUBECTL" use without the kubeconfig flag is the
            # preflight binary lookup
            assert "command -v \"$KUBECTL\"" in line, line
    assert 'command -v "$KUBECTL"' in text
    # every node-reading call resolves the binary and the kubeconfig together.
    assert '"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes' in text


def test_harness_edits_keys_that_exist_in_the_real_schema():
    # The harness mutates cluster.yaml with these dotted keys; each must exist in
    # the config scaffold that `init` writes, for BOTH providers, so a run never
    # edits a key the schema had dropped or renamed.
    template = SCAFFOLD.read_text()
    assert re.search(r"^talos:\n  version:", template, re.M)
    assert re.search(r"^kubernetes:\n  version:", template, re.M)
    assert re.search(r"^workers:\n  worker:\n    count:", template, re.M)


def test_harness_runs_for_each_provider():
    text = HARNESS.read_text()
    assert "--provider openstack" in text and "--provider proxmox" in text
    # preflight and teardown branches are provider-agnostic, but the drain and
    # version helpers must be reached from a single path for either provider.
    assert 'case "$PROVIDER" in openstack|proxmox' in text


def test_guide_maps_the_four_workflows_to_existing_doc_pages():
    text = GUIDE.read_text()
    # each phase must cite the page that documents it, so a renamed page fails here.
    assert "quickstart.md" in text
    assert "usage.md#upgrade-talos-or-kubernetes" in text
    assert "maintenance.md" in text
    assert "usage.md#tear-down" in text


def test_guide_documents_multiworld_taloscluster_value():
    # The documented release-gate form -- `TALOSCLUSTER="uv tool run taloscluster"` --
    # is a command with arguments, so the harness must keep splitting the value
    # into words. If the guide ever documents a plain binary path instead, this
    # pin fails and the harness could revert to a single-argument array.
    text = GUIDE.read_text()
    assert "TALOSCLUSTER=" in text
    assert "uv tool run taloscluster" in text


def test_harness_splits_the_documented_taloscluster_value():
    # Regression: `TALOSCLUSTER` must be treated as a command line, not a single
    # executable name, or the guide's `uv tool run taloscluster` fails preflight
    # with "command not found". Execute the harness's real TALOSCLUSTER branch
    # with the documented value and assert it yields the four argv words.
    src = HARNESS.read_text()
    m = re.search(r'if \[\[ -n "\$\{TALOSCLUSTER:-\}" \]\].*?\n *fi\n', src, re.S)
    assert m, "harness must keep a TALOSCLUSTER branch to word-split"
    script = "\n".join(
        [
            "set -euo pipefail",
            'TALOSCLUSTER="uv tool run taloscluster"',
            m.group(0),
            'printf "%s\\n" "${#TC[@]}"',
            'printf "%s\\n" "${TC[*]}"',
        ]
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert int(lines[0]) == 4
    assert lines[1].split() == ["uv", "tool", "run", "taloscluster"]


def test_guide_and_harness_drain_the_largest_worker_pool():
    # The harness drains the configured pool with the HIGHEST count (so a
    # zero-node test pool is never chosen). The guide must describe the same
    # selection, not "the first pool" (which the harness does not pick). Both files
    # must stay in lockstep on the wording and on the selection logic.
    assert "max(pools, key=count)" in HARNESS.read_text()
    for text in (HARNESS.read_text(), GUIDE.read_text()):
        assert "first worker pool" not in text, "drifted to selecting the first pool"
        assert "largest worker pool" in text, "worker-pool wording drifted from the harness"


def test_guide_upgrade_claim_matches_harness_assertions():
    # Phase 2 must describe what the harness and converge actually check: converge
    # refuses to finish a node until it reports the target Talos version (so its
    # clean exit after the bump proves the Talos rollout), and the harness then
    # reads the kubelet minor for every node from the cluster kubeconfig. If the
    # wording claims a per-node Talos read the harness performs nowhere, this fails.
    text = GUIDE.read_text()
    assert "largest worker pool" in text  # phase 3 confirmed configured before this
    upgrade = text.split("**Upgrade**", 1)[1].split("**Drain**", 1)[0]
    assert "target Talos version" in upgrade
    assert "reports the pinned Kubernetes minor" in upgrade
    assert "kubeconfig" in upgrade


def test_harness_refuses_a_noop_upgrade_target():
    # The upgrade phase must exercise a real version bump, not silently converge
    # to a no-op (the node count stays, so a pass would prove nothing). The
    # harness refuses a target that equals its pin and tells the user to pass a
    # newer version; the guide documents that refusal.
    text = HARNESS.read_text()
    assert '[[ "$TALOS_VERSION" != "$old_talos" ]]' in text
    assert '[[ "$KUBERNETES_VERSION" != "$old_k8s" ]]' in text
    assert "already matches the pin" in text
    assert "pass --talos-version to force an upgrade" in text
    assert "pass --kubernetes-version to force an upgrade" in text
    assert "already equals its pin is refused" in GUIDE.read_text()


def test_guide_does_not_say_the_provider_section_stays_scaffolded():
    # Regression: the scaffolded provider section is all placeholders (endpoint,
    # availability zone, external net / storage) that must be replaced with the
    # tenant's values. The guide must not tell the reader it stays as-is, or a
    # literal run fails the preflight status check against example.edu.
    text = GUIDE.read_text()
    assert "provider section" in text
    assert "placeholders" in text
    assert "availability_zone" in text or "external_net" in text or "iso_storage" in text
    assert "example.edu" in text
    # the guide must point at the provider setup guides for the actual keys.
    assert "providers/openstack.md" in text
    assert "providers/proxmox.md" in text


def test_guide_documents_tailscale_handling():
    # Regression: the scaffold adds a `tailscale:` block to both files (login_server
    # in cluster.yaml, an auth_key "CHANGE-ME" placeholder in secrets.yaml). Neither
    # the placeholder refusal nor the tailscale_enabled hang is discoverable, so the
    # guide must tell the reader to remove the section from both files (or supply
    # real values) and to keep the 100.64.0.0/10 allowlist when reaching nodes over
    # the tailnet.
    text = GUIDE.read_text()
    assert "Tailscale" in text
    assert "CHANGE-ME" in text and "CHANGE-ME" in SCAFFOLD.read_text()
    assert "remove the `tailscale:` section from both `cluster.yaml` and `secrets.yaml`" in text
    assert "100.64.0.0/10" in text


def test_harness_usage_when_a_value_flag_is_last():
    # Regression: under `set -u`, `--provider` etc. as the last argv hit an unbound
    # `$2` instead of printing usage. The harness must guard each value flag.
    text = HARNESS.read_text()
    assert "[[ $# -ge 2 ]] || script_usage 1" in text
    result = subprocess.run([str(HARNESS), "--provider"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in (result.stdout + result.stderr).lower() or "--provider" in result.stderr


def test_harness_teardown_line_does_not_overclaim_provider_deletion():
    # The teardown info line must match what is actually asserted (the three local
    # client files); provider-resource deletion is trusted to destroy's exit code.
    assert "managed resources" not in HARNESS.read_text()

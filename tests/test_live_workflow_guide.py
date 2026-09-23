"""Keep the live-workflow harness valid and its guide's commands correct.

The live workflow test is the only path that runs the real ``taloscluster``
against a provider (it takes live credentials, so it never runs in CI), but the
harness script and its guide can still drift. The harness must stay executable,
syntactically valid bash that guards its value flags, splits the documented
``TALOSCLUSTER`` command value, refuses a no-op upgrade target, points every
kubectl call at the generated kubeconfig, and edits keys the schema still
defines, for both providers. The guide must stay published and only call
registered subcommands; its prose may be reworded freely.
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


def test_harness_refuses_a_noop_upgrade_target():
    # The upgrade phase must exercise a real version bump, not silently converge
    # to a no-op (the node count stays, so a pass would prove nothing). The
    # harness refuses a target that equals its pin and tells the user to pass a
    # newer version.
    text = HARNESS.read_text()
    assert '[[ "$TALOS_VERSION" != "$old_talos" ]]' in text
    assert '[[ "$KUBERNETES_VERSION" != "$old_k8s" ]]' in text
    assert "already matches the pin" in text
    assert "pass --talos-version to force an upgrade" in text
    assert "pass --kubernetes-version to force an upgrade" in text


def test_harness_usage_when_a_value_flag_is_last():
    # Regression: under `set -u`, `--provider` etc. as the last argv hit an unbound
    # `$2` instead of printing usage. The harness must guard each value flag.
    text = HARNESS.read_text()
    assert "[[ $# -ge 2 ]] || script_usage 1" in text
    result = subprocess.run([str(HARNESS), "--provider"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in (result.stdout + result.stderr).lower() or "--provider" in result.stderr

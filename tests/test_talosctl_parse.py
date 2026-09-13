"""Tests for the output parsers in taloscluster.talos.talosctl.

``server_version`` and ``running_schematic`` shell out via ``_run``; we
monkeypatch ``_run`` so no ``talosctl`` binary is needed and assert the parsing
logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from taloscluster.errors import ReconcileError
from taloscluster.talos import talosctl

# Realistic `talosctl version` output: a Client block and a Server block, each
# with its own Tag. The parser must return the SERVER tag, not the client one.
VERSION_OUTPUT = """\
Client:
    Tag: v1.8.0
    SHA: abcdef
    Built: 2024-01-01
Server:
    Tag: v1.8.3
    SHA: 123456
    Built: 2024-02-01
"""


def test_server_version_returns_server_tag(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: VERSION_OUTPUT)
    tag = talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert tag == "v1.8.3"


def test_server_version_empty_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "")
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_garbage_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "nonsense\nno tags here")
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_client_only_no_server_returns_empty(monkeypatch):
    """If no Server: block is present, there is no server tag."""
    out = "Client:\n    Tag: v1.8.0\n"
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


# ---------------------------------------------------------------------------
# running_schematic
# ---------------------------------------------------------------------------

SCHEMATIC = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"

# A realistic `get extensions -o yaml` stream: `node:` header lines interleaved
# with `---`-separated resource documents, one per extension. The Image Factory
# bakes a `schematic` extension whose manifest version is the running schematic.
EXTENSIONS_OUTPUT = (
    f"node: 192.0.2.10\n"
    "metadata:\n"
    "    namespace: runtime\n"
    "    type: ExtensionStatuses.runtime.talos.dev\n"
    "    id: schematic\n"
    "    version: 3\n"
    "    owner: runtime.ExtensionStatusController\n"
    "    phase: running\n"
    "spec:\n"
    "    image: ghcr.io/siderolabs/schematic:v1.0.0\n"
    "    metadata:\n"
    "        name: schematic\n"
    f"        version: {SCHEMATIC}\n"
    "        author: siderolabs\n"
    "---\n"
    f"node: 192.0.2.10\n"
    "metadata:\n"
    "    namespace: runtime\n"
    "    type: ExtensionStatuses.runtime.talos.dev\n"
    "    id: qemu-guest-agent\n"
    "    version: 2\n"
    "spec:\n"
    "    image: ghcr.io/siderolabs/qemu-guest-agent:1.0.0\n"
    "    metadata:\n"
    "        name: qemu-guest-agent\n"
    f"        version: {SCHEMATIC}\n"
)


def test_running_schematic_reads_the_factory_schematic_extension(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: EXTENSIONS_OUTPUT)
    got = talosctl.running_schematic(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == SCHEMATIC


def test_running_schematic_empty_when_no_factory_schematic(monkeypatch):
    out = (
        "node: 192.0.2.10\n"
        "metadata:\n"
        "    namespace: runtime\n"
        "    type: ExtensionStatuses.runtime.talos.dev\n"
        "    id: qemu-guest-agent\n"
        "spec:\n"
        "    metadata:\n"
        "        name: qemu-guest-agent\n"
        "        version: 1.2"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    assert talosctl.running_schematic(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_running_schematic_empty_on_empty_output(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "")
    assert talosctl.running_schematic(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


# A realistic `get members -o json` stream: separate JSON objects, NOT an array.
# controlplane-01 carries the shared kube-api VIP among its private ips, and the
# members differ in talos version (a rollout in flight).
MEMBERS_JSON = """\
{
    "metadata": {"id": "quad-controlplane-01"},
    "spec": {
        "hostname": "quad-controlplane-01",
        "machineType": "controlplane",
        "operatingSystem": "Talos (v1.13.9)",
        "addresses": ["100.64.0.68", "192.168.1.47", "192.168.3.34"]
    }
}
{
    "metadata": {"id": "quad-worker-01"},
    "spec": {
        "hostname": "quad-worker-01",
        "machineType": "worker",
        "operatingSystem": "Talos (v1.13.8)",
        "addresses": ["192.168.0.42", "100.64.0.70"]
    }
}
"""


def test_members_parses_version_and_prefers_tailscale(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, MEMBERS_JSON, ""))
    got = talosctl.members(Path("talosconfig"), "quad-controlplane-01")
    assert got["quad-controlplane-01"].version == "v1.13.9"
    assert got["quad-worker-01"].version == "v1.13.8"
    # the tailscale address, not the VIP-carrying private one and not addrs[0]
    assert got["quad-controlplane-01"].address == "100.64.0.68"
    assert got["quad-worker-01"].address == "100.64.0.70"


def test_member_addresses_still_returns_plain_addresses(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, MEMBERS_JSON, ""))
    assert talosctl.member_addresses(Path("talosconfig"), "e") == {
        "quad-controlplane-01": "100.64.0.68",
        "quad-worker-01": "100.64.0.70",
    }


def test_members_empty_when_discovery_is_unreachable(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, "", "no route"))
    assert talosctl.members(Path("talosconfig"), "e") == {}


def test_member_version_tolerates_an_odd_os_string():
    assert talosctl._member_version("Talos (v1.13.9)") == "v1.13.9"
    assert talosctl._member_version("Talos") == ""
    assert talosctl._member_version("") == ""


def test_upgrade_accepts_successful_post_check_with_nonzero_exit(monkeypatch):
    out = "upgrade completed\npost check passed\n"
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, out, ""))

    talosctl.upgrade(Path("talosconfig"), "endpoint", "node", "installer:v1.13.9")


def test_upgrade_still_raises_on_unclassified_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        talosctl,
        "_run_nocheck",
        lambda *a, **k: (1, "", "failed to pull installer image"),
    )

    with pytest.raises(RuntimeError, match="failed to pull installer image"):
        talosctl.upgrade(Path("talosconfig"), "endpoint", "node", "installer:v1.13.9")


def test_members_skips_the_shared_vip_when_excluded(monkeypatch, tmp_path):
    stream = (
        '{"metadata": {"id": "cp-01"}, "spec": {"addresses": '
        '["203.0.113.79", "10.0.0.236"], "operatingSystem": "Talos (v1.13.9)"}}'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda _cmd: (0, stream, ""))
    plain = talosctl.members(tmp_path / "talosconfig", "ep")
    excluded = talosctl.members(tmp_path / "talosconfig", "ep", exclude_vip="203.0.113.79")
    assert plain["cp-01"].address == "203.0.113.79"
    assert excluded["cp-01"].address == "10.0.0.236"


def test_members_prefers_tailscale_anywhere_in_100_64_slash_10(monkeypatch):
    """Tailscale CGNAT is the full 100.64.0.0/10: an address in 100.65-100.127
    is still a tailscale one and must be preferred over the private/VIP ips."""
    stream = (
        '{"metadata": {"id": "cp-01"}, "spec": {"addresses": '
        '["192.168.1.9", "100.127.0.17"], "operatingSystem": "Talos (v1.13.9)"}}'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda _cmd: (0, stream, ""))
    got = talosctl.members(Path("talosconfig"), "ep")
    assert got["cp-01"].address == "100.127.0.17"


def test_is_tailscale_covers_the_full_cgnat_range():
    assert talosctl._is_tailscale("100.64.0.1")
    assert talosctl._is_tailscale("100.127.255.254")
    # the boundary masked prefix is what the old `100.64.` startswith missed
    assert talosctl._is_tailscale("100.65.0.5")
    # outside the CGNAT range -- a public or normal private address
    assert not talosctl._is_tailscale("100.128.0.1")
    assert not talosctl._is_tailscale("192.168.1.9")
    assert not talosctl._is_tailscale("203.0.113.4")
    # malformed addresses are not tailscale
    assert not talosctl._is_tailscale("not-an-ip")


# ---- apply-config under plan ------------------------------------------------

def test_plan_apply_config_runs_talosctl_dry_run_and_prints_the_diff(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    seen = []

    def fake_run(args, timeout=None):
        seen.append(args)
        # talosctl prints the summary on stderr
        return 0, "", (
            "Dry run summary:\n"
            "Applied configuration without a reboot (skipped in dry-run).\n"
            "Config diff:\n\n"
            "--- a\n+++ b\n@@ -1,2 +1,2 @@\n-  ntp: [a]\n+  ntp: [a, b]\n"
        )

    monkeypatch.setattr(talosctl, "_run_nocheck", fake_run)
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)

    assert seen and "--dry-run" in seen[0]
    out = capsys.readouterr().out
    assert "[dry-run] talosctl apply-config 10.0.0.5" in out
    assert "+  ntp: [a, b]" in out
    assert "Dry run summary" not in out


def test_plan_apply_config_reports_no_changes(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    monkeypatch.setattr(
        talosctl, "_run_nocheck",
        lambda args, timeout=None: (0, "", "Dry run summary:\nConfig diff:\n\nNo changes.\n"),
    )
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)

    assert "no changes" in capsys.readouterr().out


def test_converge_apply_config_does_not_pass_dry_run(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        talosctl, "_run_nocheck",
        lambda args, timeout=None: (seen.append(args), "", "")[1:] and (0, "", ""),
    )
    talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    assert seen and "--dry-run" not in seen[0]


def test_converge_apply_config_reports_live_apply(monkeypatch, tmp_path):
    """A live/no-op apply (`--mode=auto` -> apid reports "without a reboot")
    makes apply_config return False: no restart is pending, so a settle path
    has nothing to wait out."""
    monkeypatch.setattr(
        talosctl, "_run_nocheck",
        lambda args, timeout=None: (0, "", "Applied configuration without a reboot.\n"),
    )
    assert talosctl.apply_config(
        tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}"
    ) is False


def test_converge_apply_config_reports_pending_reboot(monkeypatch, tmp_path):
    """A restart-requiring apply (`--mode=auto` -> apid reports "with a reboot")
    makes apply_config return True: the node is going down, so the settle path
    must wait it out, back in, and health-check before the next one."""
    monkeypatch.setattr(
        talosctl, "_run_nocheck",
        lambda args, timeout=None: (0, "", "Applied configuration with a reboot (2.5s).\n"),
    )
    assert talosctl.apply_config(
        tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}"
    ) is True


def test_plan_apply_config_failure_is_a_warning(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    monkeypatch.setattr(
        talosctl, "_run_nocheck", lambda args, timeout=None: (1, "", "connection refused")
    )
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    assert "could not diff machine config on 10.0.0.5" in capsys.readouterr().err


def test_plan_apply_config_redacts_secret_values(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "         key: LS0tLS1CRUdJTiBFRDI1NTE5\n"
        "     secret: 1kOcNXNRho\n"
        "-    token: abc.def\n"
        "+    token: ghi.jkl\n"
        "     secretboxEncryptionSecret: xyz\n"
        "         crt: LS0tLS1CRUdJTiBDRVJU\n"
        "-        endpoint: https://1.2.3.4:6443\n"
        "+    - TS_AUTHKEY=hskey-auth-JwDFrXEz\n"
        "+    - TS_HOSTNAME=quad-worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("LS0tLS1CRUdJTiBFRDI1NTE5", "1kOcNXNRho", "abc.def", "ghi.jkl", "xyz",
                   "hskey-auth-JwDFrXEz"):
        assert leaked not in out
    assert "key: <redacted>" in out
    assert "-    token: <redacted>" in out
    assert "crt: LS0tLS1CRUdJTiBDRVJU" in out  # certificates are public
    assert "-        endpoint: https://1.2.3.4:6443" in out
    assert "+    - TS_AUTHKEY=<redacted>" in out
    assert "+    - TS_HOSTNAME=quad-worker-01" in out


def test_plan_apply_config_redacts_files_and_inline_manifests(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        password: verysecretregistrypw\n"
        "+        username: registry-user\n"
        "+            files:\n"
        "+                - content: LS0tLS1CRUdJTiBQUklWQVRFIEtFWQotLS0tLQo=\n"
        "+                  op: create\n"
        "+                  path: /etc/secret/config\n"
        "+            inlineManifests:\n"
        "+                - name: credentials\n"
        "+                  contents: |\n"
        "+                      apiVersion: v1\n"
        "+                      kind: Secret\n"
        "+                      stringData:\n"
        "+                          password: manifestsecret\n"
        "+        environment:\n"
        "+            - REGISTRY_PASSWORD=envsecret\n"
        "+            - KUBELET_HOSTNAME=worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("verysecretregistrypw", "LS0tLS1CRUdJTiBQUklWQVRFIEtFWQotLS0tLQo=",
                   "manifestsecret", "envsecret", "apiVersion: v1", "stringData"):
        assert leaked not in out
    assert "password: <redacted>" in out
    assert "+            - REGISTRY_PASSWORD=<redacted>" in out
    assert "username: registry-user" in out
    assert "KUBELET_HOSTNAME=worker-01" in out
    assert "path: /etc/secret/config" in out


def test_plan_apply_config_redacts_blank_lines_in_block_body(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # Blank lines (bare `+`/`-`) are common between `---` documents/sections of a
    # Kubernetes inline manifest; they are part of the open block, not its end.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        inlineManifests:\n"
        "+            - contents: |\n"
        "+\n"
        "+                  apiVersion: v1\n"
        "+\n"
        "+                  kind: Secret\n"
        "+                  stringData:\n"
        "+                      password: blankline-secret\n"
        "+        environment:\n"
        "+            - KUBELET_HOSTNAME=worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("apiVersion: v1", "kind: Secret", "blankline-secret"):
        assert leaked not in out
    assert "environment:" in out
    assert "KUBELET_HOSTNAME=worker-01" in out


def test_plan_apply_config_redacts_folded_content(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # `contents: >` (folded) scalars must be tracked just like `|` literals.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        inlineManifests:\n"
        "+            - contents: >\n"
        "+                  apiVersion: v1\n"
        "+                  kind: Secret\n"
        "+                  stringData:\n"
        "+                      password: folded-secret\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("apiVersion: v1", "kind: Secret", "folded-secret"):
        assert leaked not in out


def test_plan_apply_config_tracks_block_with_trailing_comment(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A block scalar with a comment after the `|` indicator is still valid YAML;
    # the scalar's body lines must be redacted as a region regardless.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        inlineManifests:\n"
        "+            - contents: | # apiVersion v1\n"
        "+                  kind: Secret\n"
        "+                  stringData:\n"
        "+                      password: comment-secret\n"
        "+        environment:\n"
        "+            - KUBELET_HOSTNAME=worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("kind: Secret", "stringData", "comment-secret"):
        assert leaked not in out
    assert "environment:" in out
    assert "KUBELET_HOSTNAME=worker-01" in out


def test_plan_redacts_sequence_content_keeps_siblings(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # machine.files uses `- content: |` sequence items; the value body is redacted
    # but the sibling keys at the same column (`op:`, `path:`) stay visible.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+            files:\n"
        "+                - content: |\n"
        "+                      LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=\n"
        "+                  op: create\n"
        "+                  path: /etc/secret/config\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    assert "LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=" not in out
    assert "path: /etc/secret/config" in out
    assert "op: create" in out


def test_plan_apply_config_redacts_block_split_across_hunks(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A long block scalar body split across two hunks must keep redacting after
    # the second `@@` instead of treating the hunk header as the block's end.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        inlineManifests:\n"
        "+            - contents: |\n"
        "+                  apiVersion: v1\n"
        "@@ -5,6 +5,7 @@\n"
        "+                  kind: Secret\n"
        "+                  stringData:\n"
        "+                      password: crosshunk-secret\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("apiVersion: v1", "kind: Secret", "crosshunk-secret"):
        assert leaked not in out


def test_bootstrap_retries_until_etcd_accepts_it(tmp_path, monkeypatch):
    not_ready = "rpc error: code = FailedPrecondition desc = bootstrap is not available yet"
    results = iter([(1, "", not_ready), (1, "", not_ready), (0, "", "")])
    calls = []
    monkeypatch.setattr(talosctl, "_run_nocheck",
                        lambda args, timeout=None: (calls.append(args), next(results))[1])
    monkeypatch.setattr(talosctl.time, "sleep", lambda s: None)

    talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1")

    assert len(calls) == 3


def test_bootstrap_gives_up_when_etcd_never_becomes_available(tmp_path, monkeypatch):
    not_ready = "rpc error: code = FailedPrecondition desc = bootstrap is not available yet"
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (1, "", not_ready))
    monkeypatch.setattr(talosctl.time, "sleep", lambda s: None)
    clock = iter([0.0, 1.0, 400.0])
    monkeypatch.setattr(talosctl.time, "monotonic", lambda: next(clock))

    with pytest.raises(RuntimeError, match="not available yet"):
        talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1", timeout_s=300)


def test_bootstrap_treats_already_bootstrapped_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck",
                        lambda args, timeout=None: (1, "", "etcd data directory is not empty"))
    talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1")


# ---------------------------------------------------------------------------
# reset: a failed / timed-out graceful reset is fatal for a control plane
# ---------------------------------------------------------------------------

def test_reset_worker_failure_is_only_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (1, "", "boom"))
    talosctl.reset(tmp_path / "talosconfig", "endpoint", "worker-01")  # must not raise


def test_reset_control_plane_failure_raises(tmp_path, monkeypatch):
    """A failed graceful reset leaves a dead etcd member; refuse the removal."""
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (1, "", "boom"))
    with pytest.raises(ReconcileError, match="control plane cp-03 failed"):
        talosctl.reset(tmp_path / "talosconfig", "endpoint", "cp-03", control_plane=True)


def test_reset_control_plane_timeout_raises(tmp_path, monkeypatch):
    def boom(args, timeout=None):
        raise subprocess.TimeoutExpired("talosctl reset", timeout)
    monkeypatch.setattr(talosctl, "_run_nocheck", boom)
    with pytest.raises(ReconcileError, match="control plane cp-03 timed out"):
        talosctl.reset(tmp_path / "talosconfig", "endpoint", "cp-03", control_plane=True)

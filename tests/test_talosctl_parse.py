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
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: VERSION_OUTPUT)
    tag = talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert tag == "v1.8.3"


def test_server_version_empty_output_returns_empty(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: "")
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_garbage_output_returns_empty(monkeypatch):
    monkeypatch.setattr(
        talosctl, "_run", lambda args, capture=False, timeout=None: "nonsense\nno tags here"
    )
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_client_only_no_server_returns_empty(monkeypatch):
    """If no Server: block is present, there is no server tag."""
    out = "Client:\n    Tag: v1.8.0\n"
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: out)
    assert talosctl.server_version(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_server_version_passes_a_subprocess_timeout(monkeypatch):
    """The rollout wait polls this read against a node that may be rebooting:
    the probe is bounded so a hung apid costs the timeout, not the OS connect
    timeout, and the caller treats the expiry as still down."""
    seen = {}

    def fake_run(args, capture=False, timeout=None):
        seen["timeout"] = timeout
        return VERSION_OUTPUT

    monkeypatch.setattr(talosctl, "_run", fake_run)
    assert talosctl.server_version(Path("talosconfig"), "ep", "node-01") == "v1.8.3"
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


# ---------------------------------------------------------------------------
# running_schematic
# ---------------------------------------------------------------------------

SCHEMATIC = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"

# A realistic `get extensions -o yaml` stream: `node:` header lines interleaved
# with `---`-separated resource documents, one per extension. The Image Factory
# bakes a `schematic` extension whose manifest version is the running schematic,
# and a tailscale-using node also reports the `siderolabs/tailscale` extension.
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
    "---\n"
    f"node: 192.0.2.10\n"
    "metadata:\n"
    "    namespace: runtime\n"
    "    type: ExtensionStatuses.runtime.talos.dev\n"
    "    id: siderolabs-tailscale-v1.86.0\n"
    "    version: 1\n"
    "spec:\n"
    "    image: ghcr.io/siderolabs/tailscale:1.0.0\n"
    "    metadata:\n"
    "        name: siderolabs/tailscale\n"
    f"        version: {SCHEMATIC}\n"
)


def test_running_schematic_reads_the_factory_schematic_extension(monkeypatch):
    monkeypatch.setattr(
        talosctl, "_run", lambda args, capture=False, timeout=None: EXTENSIONS_OUTPUT
    )
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
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: out)
    assert talosctl.running_schematic(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_running_schematic_empty_on_empty_output(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: "")
    assert talosctl.running_schematic(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_running_schematic_passes_a_subprocess_timeout(monkeypatch):
    """The second half of the rollout wait's probe pair, bounded like the
    version read so a hung apid cannot outlive the wait's own deadline."""
    seen = {}

    def fake_run(args, capture=False, timeout=None):
        seen["timeout"] = timeout
        return EXTENSIONS_OUTPUT

    monkeypatch.setattr(talosctl, "_run", fake_run)
    talosctl.running_schematic(Path("talosconfig"), "ep", "node-01")
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


# ---------------------------------------------------------------------------
# running_extensions
# ---------------------------------------------------------------------------


def test_running_extensions_lists_every_extension_name(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: EXTENSIONS_OUTPUT)
    got = talosctl.running_extensions(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == ["schematic", "qemu-guest-agent", "siderolabs/tailscale"]


def test_running_extensions_falls_back_to_the_resource_id(monkeypatch):
    out = (
        "node: 192.0.2.10\n"
        "metadata:\n"
        "    namespace: runtime\n"
        "    type: ExtensionStatuses.runtime.talos.dev\n"
        "    id: siderolabs-tailscale-v1.86.0\n"
        "    version: 1\n"
        "spec:\n"
        "    image: ghcr.io/siderolabs/tailscale:1.0.0\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: out)
    got = talosctl.running_extensions(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == ["siderolabs-tailscale-v1.86.0"]


def test_running_extensions_empty_on_empty_output(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False: "")
    assert talosctl.running_extensions(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == []


# ---------------------------------------------------------------------------
# running_install_disk
# ---------------------------------------------------------------------------

# A realistic `get machineconfig v1alpha1 -o yaml` stream: one resource whose
# spec is the machine configuration itself, carried as a YAML string (talosctl
# marshals the resource's spec with the `talos.dev/yaml-spec` annotation). The
# string holds a multi-document config, so its own `---` separators are indented
# INSIDE the block scalar; the machine section names the install disk.
MACHINECONFIG_OUTPUT = """\
node: 192.0.2.61
---
metadata:
    namespace: config
    type: MachineConfigs.config.talos.dev
    id: v1alpha1
    version: 5
    owner: config.V1Alpha1Controller
    phase: running
spec: |
    machine:
        type: worker
        install:
            disk: /dev/sda
            image: factory.talos.dev/metal-installer/abc:v1.13.9
    ---
    apiVersion: v1alpha1
    kind: LinkConfig
    name: eno1
"""


def test_running_install_disk_reads_the_running_configurations_disk(monkeypatch):
    monkeypatch.setattr(
        talosctl, "_run", lambda args, capture=False, timeout=None: MACHINECONFIG_OUTPUT
    )
    got = talosctl.running_install_disk(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == "/dev/sda"


def test_running_install_disk_also_reads_an_inline_spec(monkeypatch):
    """A talosctl that marshals the spec as the parsed config mapping instead of
    a YAML string is read the same way."""
    out = (
        "node: 192.0.2.61\n"
        "---\n"
        "metadata:\n"
        "    id: v1alpha1\n"
        "spec:\n"
        "    machine:\n"
        "        install:\n"
        "            disk: /dev/nvme0n1\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: out)
    got = talosctl.running_install_disk(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == "/dev/nvme0n1"


def test_running_install_disk_empty_without_a_disk(monkeypatch):
    out = (
        "node: 192.0.2.61\n"
        "---\n"
        "metadata:\n"
        "    id: v1alpha1\n"
        "spec: |\n"
        "    machine:\n"
        "        type: worker\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: out)
    assert talosctl.running_install_disk(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01") == ""


def test_running_install_disk_empty_on_garbage_or_empty_output(monkeypatch):
    for out in ("nonsense", ""):
        monkeypatch.setattr(
            talosctl, "_run", lambda args, capture=False, timeout=None, out=out: out
        )
        assert talosctl.running_install_disk(
            Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
        ) == ""


def test_running_install_disk_targets_the_machineconfig_resource(monkeypatch):
    seen = {}

    def fake_run(args, capture=False, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(talosctl, "_run", fake_run)
    talosctl.running_install_disk(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert "get" in seen["args"] and "machineconfig" in seen["args"]


def test_running_install_disk_passes_a_subprocess_timeout(monkeypatch):
    """The validate-phase disk probe dials machines that may be powered off:
    bounded by the subprocess timeout, not the OS TCP connect timeout."""
    seen = {}

    def fake_run(args, capture=False, timeout=None):
        seen["timeout"] = timeout
        return MACHINECONFIG_OUTPUT

    monkeypatch.setattr(talosctl, "_run", fake_run)
    assert talosctl.running_install_disk(Path("talosconfig"), "ep", "node-01") == "/dev/sda"
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


# A 1.14 machine config: the install left the v1alpha1 document for the typed
# `UnattendedInstallConfig` one, whose diskSelector is generated from
# `--install-disk` as `disk.dev_path == "<disk>"`.
UNATTENDED_OUTPUT = """\
node: 192.0.2.61
---
metadata:
    namespace: config
    type: MachineConfigs.config.talos.dev
    id: v1alpha1
    version: 2
    phase: running
spec: |
    version: v1alpha1
    machine:
        type: worker
    ---
    apiVersion: v1alpha1
    kind: UnattendedInstallConfig
    installer:
        image: factory.talos.dev/metal-installer/abc:v1.14.0
    provisioning:
        diskSelector:
            match: disk.dev_path == "/dev/sda"
        wipe: true
"""


def test_running_install_disk_reads_the_unattended_install_document(monkeypatch):
    """On Talos 1.14 the v1alpha1 document carries no machine.install any more;
    the disk is read from the UnattendedInstallConfig document's selector, so
    the metal disk-move guard keeps working against a 1.14 node."""
    monkeypatch.setattr(
        talosctl, "_run", lambda args, capture=False, timeout=None: UNATTENDED_OUTPUT
    )
    got = talosctl.running_install_disk(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert got == "/dev/sda"


def test_running_install_disk_empty_when_the_selector_does_not_name_a_disk(monkeypatch):
    out = (
        "node: 192.0.2.61\n"
        "---\n"
        "metadata:\n"
        "    id: v1alpha1\n"
        "spec: |\n"
        "    version: v1alpha1\n"
        "    machine:\n"
        "        type: worker\n"
        "    ---\n"
        "    apiVersion: v1alpha1\n"
        "    kind: UnattendedInstallConfig\n"
        "    provisioning:\n"
        "        wipe: true\n"
    )
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, timeout=None: out)
    assert talosctl.running_install_disk(
        Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
    ) == ""


# ---------------------------------------------------------------------------
# secureboot_enforced
# ---------------------------------------------------------------------------

# A realistic `get securitystate -o yaml` stream: one SecurityState document
# whose spec reports the firmware state the running system booted under.
SECURITYSTATE_OUTPUT = """\
node: 192.0.2.10
metadata:
    namespace: runtime
    type: SecurityStates.talos.dev
    id: securitystate
    version: 2
    owner: runtime.SecurityStateController
    phase: running
spec:
    secureBoot: true
    ukiSigningKeyFingerprint: ""
    bootedWithUKI: true
"""


def test_secureboot_enforced_reads_the_security_state_resource(monkeypatch):
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, quiet_stderr=False:
                        SECURITYSTATE_OUTPUT)
    assert talosctl.secureboot_enforced(
        Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
    ) is True


def test_secureboot_enforced_reports_an_unenforced_node(monkeypatch):
    """A node created before Secure Boot support booted the plain ISO, so its
    spec says secureBoot: false -- the caller must keep the plain installer."""
    out = SECURITYSTATE_OUTPUT.replace("secureBoot: true", "secureBoot: false")
    monkeypatch.setattr(talosctl, "_run", lambda args, capture=False, quiet_stderr=False: out)
    assert talosctl.secureboot_enforced(
        Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
    ) is False


def test_secureboot_enforced_none_on_a_failed_read(monkeypatch):
    """A node that does not answer the read is an unknown, not a verdict."""
    def fail(args, capture=False, quiet_stderr=False):
        raise subprocess.CalledProcessError(1, "talosctl")

    monkeypatch.setattr(talosctl, "_run", fail)
    assert talosctl.secureboot_enforced(
        Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
    ) is None


def test_secureboot_enforced_none_on_garbage_output(monkeypatch):
    monkeypatch.setattr(
        talosctl, "_run", lambda args, capture=False, quiet_stderr=False: "nonsense"
    )
    assert talosctl.secureboot_enforced(
        Path("/dev/null/talosconfig"), "1.2.3.4", "node-01"
    ) is None


def test_secureboot_enforced_targets_the_securitystate_resource(monkeypatch):
    args_seen: list[list[str]] = []
    monkeypatch.setattr(talosctl, "_run",
                        lambda args, capture=False, quiet_stderr=False:
                        args_seen.append(args) or "")
    talosctl.secureboot_enforced(Path("/dev/null/talosconfig"), "1.2.3.4", "node-01")
    assert args_seen and args_seen[0][-4:] == ["get", "securitystate", "-o", "yaml"]


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


def test_tailnet_member_addresses_keeps_only_tailnet_members(monkeypatch):
    """A member still registered on the tailnet reports a 100.64/10 address;
    one that never registered reports only real addresses and is filtered out
    -- the difference a tailscale-removal refusal decides on."""
    stream = (
        '{"metadata": {"id": "quad-controlplane-01"}, "spec": {"addresses": '
        '["10.0.0.236", "100.64.0.68"], "operatingSystem": "Talos (v1.13.9)"}}\n'
        '{"metadata": {"id": "quad-worker-01"}, "spec": {"addresses": '
        '["192.168.0.42"], "operatingSystem": "Talos (v1.13.8)"}}\n'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, stream, ""))
    assert talosctl.tailnet_member_addresses(Path("talosconfig"), "e") == {
        "quad-controlplane-01": "100.64.0.68",
    }


def test_tailnet_member_addresses_empty_when_discovery_is_unreachable(monkeypatch):
    """Discovery that answers nothing reads as never registered -- a rollout in
    that state falls back to real addresses too."""
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, "", "no route"))
    assert talosctl.tailnet_member_addresses(Path("talosconfig"), "e") == {}


def test_members_empty_when_discovery_is_unreachable(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, "", "no route"))
    assert talosctl.members(Path("talosconfig"), "e") == {}


def test_members_treats_a_timed_out_discovery_as_unreachable(monkeypatch):
    """Discovery against an address that accepts TCP but never answers costs the
    subprocess timeout, not the OS connect timeout, and reads as unreachable --
    the callers fall back to the provider inventory either way."""

    def hangs(args, timeout=None):
        raise subprocess.TimeoutExpired(talosctl.BIN, timeout)

    monkeypatch.setattr(talosctl, "_run_nocheck", hangs)
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

    with pytest.raises(ReconcileError, match="failed to pull installer image"):
        talosctl.upgrade(Path("talosconfig"), "endpoint", "node", "installer:v1.13.9")


def test_members_skips_the_shared_vip_when_excluded(monkeypatch, tmp_path):
    stream = (
        '{"metadata": {"id": "cp-01"}, "spec": {"addresses": '
        '["203.0.113.79", "10.0.0.236"], "operatingSystem": "Talos (v1.13.9)"}}'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda _cmd, **_k: (0, stream, ""))
    plain = talosctl.members(tmp_path / "talosconfig", "ep")
    excluded = talosctl.members(tmp_path / "talosconfig", "ep", exclude_vip="203.0.113.79")
    assert plain["cp-01"].address == "203.0.113.79"
    assert excluded["cp-01"].address == "10.0.0.236"


def test_members_returns_unknown_when_every_address_is_the_excluded_vip(monkeypatch, tmp_path):
    """A member reporting only excluded VIPs must report "" (unknown) rather
    than fall back to the excluded VIP, which would name whatever node owns it."""
    stream = (
        '{"metadata": {"id": "cp-01"}, "spec": {"addresses": '
        '["203.0.113.79", "203.0.113.80"], "operatingSystem": "Talos (v1.13.9)"}}'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda _cmd, **_k: (0, stream, ""))
    got = talosctl.members(
        tmp_path / "talosconfig", "ep", exclude_vip=["203.0.113.79", "203.0.113.80"]
    )
    assert got["cp-01"].address == ""
    # the address-only view keeps the member but with an unknown address, so
    # resolve_node_address falls through to the provider inventory / network
    assert talosctl.member_addresses(
        tmp_path / "talosconfig",
        "ep",
        exclude_vip=["203.0.113.79", "203.0.113.80"],
    )["cp-01"] == ""


def test_members_prefers_tailscale_anywhere_in_100_64_slash_10(monkeypatch):
    """Tailscale CGNAT is the full 100.64.0.0/10: an address in 100.65-100.127
    is still a tailscale one and must be preferred over the private/VIP ips."""
    stream = (
        '{"metadata": {"id": "cp-01"}, "spec": {"addresses": '
        '["192.168.1.9", "100.127.0.17"], "operatingSystem": "Talos (v1.13.9)"}}'
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda _cmd, **_k: (0, stream, ""))
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


# A realistic `talosctl etcd members` tabwriter table: `-o`/`--output` does not
# exist on the subcommand, so this is how the authoritative live etcd member
# list is actually served by a surviving control plane. tabwriter space-pads
# each column to the header's width (padding=3); cells never contain whitespace.
ETCD_MEMBERS_OUTPUT = """\
NODE     ID        HOSTNAME          PEER URLS             CLIENT URLS           LEARNER
10.0.0.1 9eb1f01d  controlplane-01   https://192.0.2.1:2380 https://192.0.2.1:2379 false
10.0.0.1 8eb052c9  controlplane-03   https://192.0.2.3:2380 https://192.0.2.3:2379 false
"""

# The 5-column layout (no leading NODE column) used by earlier Talos releases;
# the header still tells the parser where ID and HOSTNAME sit.
ETCD_MEMBERS_OUTPUT_NO_NODE = """\
ID        HOSTNAME          PEER URLS             CLIENT URLS           LEARNER
9eb1f01d  controlplane-01   https://192.0.2.1:2380 https://192.0.2.1:2379 false
"""


def test_etcd_members_parses_live_member_hostnames(monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, ETCD_MEMBERS_OUTPUT, ""))
    assert talosctl.etcd_members(Path("talosconfig"), "cp-01") == {
        "controlplane-01": "9eb1f01d",
        "controlplane-03": "8eb052c9",
    }


def test_etcd_members_parses_without_a_node_column(monkeypatch):
    """The `ID`/`HOSTNAME` columns are located from the header, so a version
    that omits the leading `NODE` column is parsed the same way."""
    monkeypatch.setattr(
        talosctl, "_run_nocheck",
        lambda *a, **k: (0, ETCD_MEMBERS_OUTPUT_NO_NODE, ""),
    )
    assert talosctl.etcd_members(Path("talosconfig"), "cp-01") == {
        "controlplane-01": "9eb1f01d",
    }


def test_etcd_members_raises_on_empty_membership(monkeypatch):
    """A surviving control plane always lists itself, so an empty member list is
    missing/ambiguous evidence -- the helper must fail closed, not delete."""
    header_only = "NODE  ID  HOSTNAME  PEER URLS  CLIENT URLS  LEARNER\n"
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, header_only, ""))
    with pytest.raises(ReconcileError, match="returned no members"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


def test_etcd_members_raises_when_query_fails(monkeypatch):
    """A failed `etcd members` query means there is no authoritative evidence a
    node left etcd, so the helper must fail closed rather than return {}."""
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (1, "", "no route"))
    with pytest.raises(ReconcileError, match="could not read etcd membership"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


def test_etcd_members_fails_closed_on_a_timed_out_query(monkeypatch):
    """A query that hangs (apid accepted TCP but never answered) is a failed
    query like any other: bounded by the subprocess timeout and failed closed,
    never read as an empty membership that would prove a node left etcd."""

    def hangs(args, timeout=None):
        raise subprocess.TimeoutExpired(talosctl.BIN, timeout)

    monkeypatch.setattr(talosctl, "_run_nocheck", hangs)
    with pytest.raises(ReconcileError, match="could not read etcd membership"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


def test_etcd_members_raises_on_unparseable_output(monkeypatch):
    out = "not an etcd members table\n"
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, out, ""))
    with pytest.raises(ReconcileError, match="could not parse etcd membership"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


def test_etcd_members_raises_when_a_member_has_no_hostname(monkeypatch):
    """A member whose row stops before the hostname column (its hostname cell
    slices to empty at its offset) cannot be identified: it could be the
    addressless node under scrutiny, so the helper must fail closed."""
    stream = "NODE  ID  HOSTNAME  PEER URLS  CLIENT URLS  LEARNER\n10.0.0.1\n"
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, stream, ""))
    with pytest.raises(ReconcileError, match="member without a hostname"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


def test_etcd_members_raises_when_a_member_has_an_empty_hostname_cell(monkeypatch):
    """An etcd member added but never started reports an empty hostname (and no
    client URLs); tabwriter still pads its row to the header's column widths, so
    the hostname cell stays empty at its own offset. A whitespace split would
    elide that empty cell and shift the peer URL into the hostname slot, silently
    "confirming" the addressless control plane left etcd; slicing the row at the
    header's column offsets keeps the cell visibly empty and fails closed."""
    stream = (
        "NODE      ID        HOSTNAME            PEER URLS                 CLIENT URLS               LEARNER\n"  # noqa: E501
        "10.0.0.1  9eb1f01d  controlplane-01     https://192.0.2.1:2380    https://192.0.2.1:2379    false\n"  # noqa: E501
        "10.0.0.1  1a2b3c4d                      https://192.0.2.3:2380                              true\n"  # noqa: E501
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda *a, **k: (0, stream, ""))
    with pytest.raises(ReconcileError, match="member without a hostname"):
        talosctl.etcd_members(Path("talosconfig"), "cp-01")


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


def test_plan_apply_config_redacts_multiline_credential_block(tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A multiline `password: |` value is a block scalar: hiding the header alone
    # would leave the credential's body lines on screen.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+        registry:\n"
        "+            - host: registry.example.com\n"
        "+              username: builder\n"
        "+              password: |\n"
        "+                  3xMP1el3ak2Fo\n"
        "+                  5afirWM3dUA=\n"
        "+        machine:\n"
        "+            token: |\n"
        "+                LS0tLS1CRUdJTiBFQ0gKLS0tLS1FTkQK\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("3xMP1el3ak2Fo", "5afirWM3dUA=",
                   "LS0tLS1CRUdJTiBFQ0gKLS0tLS1FTkQK"):
        assert leaked not in out
    assert "password: <redacted>" in out
    assert "token: <redacted>" in out
    assert "username: builder" in out  # public sibling at the same column
    assert "registry.example.com" in out


def test_plan_apply_config_redacts_truncated_hunk_without_content_header(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A change deep inside a long `machine.files`/inline-manifest body can land
    # in a unified-diff hunk whose `content:`/`contents:` header line is outside
    # the hunk. The indented body fragments cannot be tied to a known block, so
    # they must be suppressed rather than printed verbatim.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+                  LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=\n"
        "+                  LS0tLS1CRUdJTiBQUklWQVRFIEtFWQotLS0tLUVORC0tLS0tCg==\n"
        "+                      password: deepbody-secret\n"
        "+                  path: /etc/secret/private\n"
        "         KUBELET_HOSTNAME=worker-01\n"
        "+              - 192.0.2.10\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=",
                   "LS0tLS1CRUdJTiBQUklWQVRFIEtFWQotLS0tLUVORC0tLS0tCg==",
                   "deepbody-secret"):
        assert leaked not in out
    assert "password: <redacted>" in out  # nested key is still caught by name
    assert "path: /etc/secret/private" in out  # public mapping kept
    assert "KUBELET_HOSTNAME=worker-01" in out  # public env kept
    assert "- 192.0.2.10" in out  # public array value kept


def test_plan_apply_config_redacts_space_context_lines_in_truncated_hunk(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # talosctl 1.14+ emits ~3 ` `-marker context lines around each change in a
    # hunk. When the change is deep inside an inline-manifest block whose
    # `contents:` header is outside the hunk, those context lines are body
    # fragments and must be suppressed just like the `+`/`-` lines — a bare
    # base64 blob, a PEM-`tls.crt:` value and a `hash:` token all leak unless the
    # ` ` marker is treated as indented body too.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "@@ -5,6 +5,7 @@\n"
        "               kind: Secret\n"
        "               stringData:\n"
        "                   password: legacy-secret\n"
        "               data:\n"
        "                   tls.crt: LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=\n"
        "               LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo==\n"
        "+                       hash: 3xMP1el3ak2Fo\n"
        "               apiVersion: v1\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    # the secret-bearing context/plus lines must all be gone
    for leaked in ("tls.crt: LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo=",
                   "LS0tLS1CRUdJTiBQUklWQVRFIEtFWQo==",
                   "3xMP1el3ak2Fo",
                   "legacy-secret"):
        assert leaked not in out
    assert "password: <redacted>" in out  # nested key caught by name
    assert "kind: Secret" in out  # structural manifest key carries no secret value
    assert "apiVersion: v1" in out
    assert "stringData:" in out
    assert "data:" in out


def test_plan_apply_config_redacts_existing_multiline_credential_body(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # Editing deep inside an *existing* `password: |` credential produces a hunk
    # without the block header; the surrounding ` `-context body lines of the
    # new/old credential must be redacted as a region, not printed verbatim.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "@@ -5,6 +5,7 @@\n"
        "               password: |\n"
        "                   3xMP1el3ak2Fo\n"
        "+                   5afirWM3dUA=\n"
        "                   LS0tLS1FTg==\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("3xMP1el3ak2Fo", "5afirWM3dUA=", "LS0tLS1FTg=="):
        assert leaked not in out
    assert "password: <redacted>" in out


def test_plan_apply_config_redacts_secret_shaped_array_entry(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A dash-prefixed array entry whose value is secret-shaped (`- LS0t...`) in
    # a truncated hunk must be suppressed, while public array values stay.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "@@ -5,6 +5,7 @@\n"
        "+              - 192.0.2.10\n"
        "+              - LS0tLS1FTkQtLS0tLQ==\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    assert "LS0tLS1FTkQtLS0tLQ==" not in out
    assert "- 192.0.2.10" in out


def test_plan_apply_config_redacts_env_shaped_body_fragments(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # The last base64 line of a long inline-manifest/machine.files body can be a
    # short padded tail (a multiple of 4 bytes ending in `=`), which looks like
    # a public `KEY=value` env entry with a short "key". Such body fragments
    # must be suppressed — bare or dash-prefixed, with a single `=` or `==`
    # padding — rather than printed verbatim, while genuinely public env entries
    # stay.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "         KUBELET_HOSTNAME=worker-01\n"
        "+            xk2m9pqw4v==\n"
        "+    - AbCdEfGhIjKlMnOpQrSt==\n"
        "+            uKq3xk2m9pqw4v=\n"
        "+    - AbCdEfGhIjK=\n"
        "+    - TS_HOSTNAME=quad-worker-01\n"
        "+    - KUBELET_HOSTNAME=\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("xk2m9pqw4v==", "AbCdEfGhIjKlMnOpQrSt==",
                   "uKq3xk2m9pqw4v=", "AbCdEfGhIjK="):
        assert leaked not in out
    assert "KUBELET_HOSTNAME=worker-01" in out  # public env kept
    assert "TS_HOSTNAME=quad-worker-01" in out  # public dash env kept
    assert "KUBELET_HOSTNAME=" in out  # empty env value kept


def test_plan_apply_config_redacts_secret_named_env_fragment_without_dash(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # A `machine.files` env-file / export-script body fragment can name a secret
    # (`SECRET_KEY=...`, `TOKEN=...`) without the `- ` dash that `_SECRET_ENV`
    # expects. The NAME alone marks it secret, whatever its value looks like —
    # even an all-lowercase value that would otherwise pass as public.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "         marker context line\n"
        "+    SECRET_KEY=deadbeefcafe1234\n"
        "+    API_TOKEN=0123456789abcdef\n"
        "+    TS_HOSTNAME=quad-worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("deadbeefcafe1234", "0123456789abcdef"):
        assert leaked not in out
    assert "TS_HOSTNAME=quad-worker-01" in out  # public dash env kept


def test_plan_apply_config_redacts_deep_mapping_lowercase_blob(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # Deep body mapping entries whose key is a password alias (`passwd:`) or an
    # unremarkable name (`api_auth:`) with an all-lowercase blob value cannot be
    # classified safely: `passwd` is caught by the widened key-name pattern, and
    # a bare ≥10-char unbroken alphanumeric run (no `/`, `.`, `-` separator) is
    # secret-shaped regardless of case. Separated lowercase hostnames/paths and
    # short-run words stay public.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+                    passwd: deadbeefcafe1234\n"
        "+                    api_auth: supersecretkey123\n"
        "+                    host: quad-worker-01\n"
        "+                    - registry.example.com\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    for leaked in ("deadbeefcafe1234", "supersecretkey123"):
        assert leaked not in out
    assert "host: quad-worker-01" in out  # short lowercase word stays public
    assert "registry.example.com" in out  # separators keep it public
    assert "passwd: <redacted>" in out


def test_plan_apply_config_keeps_punctuation_separated_public_fragments(
        tmp_path, monkeypatch, capsys):
    from taloscluster.output import set_dry_run

    # Hyphenated/dotted/slashed lowercase fragments stay public: each separator
    # splits the value into short runs, so a path or hostname is never flagged
    # by the 10+ char run rule. Only a long mixed-case value (possible base64
    # `+`/`=`-free blob) is still hidden as a blob.
    diff = (
        "Dry run summary:\nConfig diff:\n--- a\n+++ b\n"
        "+    - /etc/configure-files/hosts\n"
        "+    - registry.example.com\n"
        "+    - quad-worker-01\n"
    )
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (0, "", diff))
    set_dry_run(True)
    try:
        talosctl.apply_config(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}")
    finally:
        set_dry_run(False)
    out = capsys.readouterr().out
    assert "/etc/configure-files/hosts" in out
    assert "registry.example.com" in out
    assert "quad-worker-01" in out


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

    with pytest.raises(ReconcileError, match="not available yet"):
        talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1", timeout_s=300)


def test_bootstrap_treats_already_bootstrapped_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(talosctl, "_run_nocheck",
                        lambda args, timeout=None: (1, "", "etcd data directory is not empty"))
    talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1")


def test_bootstrap_retries_a_timed_out_call(tmp_path, monkeypatch):
    """A bootstrap call that hangs (apid accepted TCP but never answered) is the
    node still coming up: retry within the deadline like the not-ready case."""
    results = iter(["timeout", "timeout", (0, "", "")])
    calls = []

    def fake(args, timeout=None):
        calls.append(args)
        result = next(results)
        if result == "timeout":
            raise subprocess.TimeoutExpired(talosctl.BIN, timeout)
        return result

    monkeypatch.setattr(talosctl, "_run_nocheck", fake)
    monkeypatch.setattr(talosctl.time, "sleep", lambda s: None)

    talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1")

    assert len(calls) == 3


def test_bootstrap_gives_up_when_calls_keep_timing_out(tmp_path, monkeypatch):
    def hangs(args, timeout=None):
        raise subprocess.TimeoutExpired(talosctl.BIN, timeout)

    monkeypatch.setattr(talosctl, "_run_nocheck", hangs)
    monkeypatch.setattr(talosctl.time, "sleep", lambda s: None)
    clock = iter([0.0, 10.0, 400.0])
    monkeypatch.setattr(talosctl.time, "monotonic", lambda: next(clock))

    with pytest.raises(ReconcileError, match="timed out"):
        talosctl.bootstrap(tmp_path / "talosconfig", "10.0.0.1", "10.0.0.1", timeout_s=300)


def test_apply_config_failure_raises_reconcile_error(tmp_path, monkeypatch):
    """A failed config push raises the same typed error as every other
    talosctl failure, so callers can catch one reconcile error type."""
    monkeypatch.setattr(talosctl, "_run_nocheck", lambda args, timeout=None: (1, "", "boom"))

    with pytest.raises(ReconcileError, match="apply-config on 10.0.0.5 failed"):
        talosctl.apply_config(
            tmp_path / "talosconfig", "10.0.0.1", "10.0.0.5", "machine: {}"
        )


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


# ---------------------------------------------------------------------------
# maintenance-mode invocations: --insecure rides the subcommand
# ---------------------------------------------------------------------------

def test_maintenance_reachable_runs_insecure_behind_the_subcommand(monkeypatch):
    """`--insecure` is refused as a global flag by the client, so the probe
    runs `talosctl version --insecure -n NODE`, not `--insecure -n NODE version`."""
    seen = {}

    def fake_run_nocheck(args, timeout=None):
        seen["args"] = args
        seen["timeout"] = timeout
        return 0, "", ""

    monkeypatch.setattr(talosctl, "_run_nocheck", fake_run_nocheck)

    assert talosctl.maintenance_reachable("172.29.21.5") is True
    assert seen["args"] == ["version", "--insecure", "-n", "172.29.21.5"]
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


def test_reachable_passes_a_subprocess_timeout(monkeypatch):
    seen = {}

    def fake_run_nocheck(args, timeout=None):
        seen["timeout"] = timeout
        return 0, "", ""

    monkeypatch.setattr(talosctl, "_run_nocheck", fake_run_nocheck)

    assert talosctl.reachable(Path("talosconfig"), "ep", "172.29.21.5") is True
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


def test_reachable_treats_a_timed_out_probe_as_unreachable(monkeypatch):
    """The probes run in validate for every unjoined non-redfish server, so an
    unroutable address must cost the subprocess timeout once, not the OS TCP
    connect timeout -- and read as unreachable, like any other no-answer."""

    def hangs(args, timeout=None):
        raise subprocess.TimeoutExpired(talosctl.BIN, timeout)

    monkeypatch.setattr(talosctl, "_run_nocheck", hangs)
    assert talosctl.reachable(Path("talosconfig"), "ep", "172.29.21.5") is False


def test_maintenance_reachable_treats_a_timed_out_probe_as_unreachable(monkeypatch):
    def hangs(args, timeout=None):
        raise subprocess.TimeoutExpired(talosctl.BIN, timeout)

    monkeypatch.setattr(talosctl, "_run_nocheck", hangs)
    assert talosctl.maintenance_reachable("172.29.21.5") is False


def test_apply_config_insecure_runs_insecure_behind_the_subcommand(monkeypatch):
    """Same for the config push to a waiting-to-join machine: the subcommand
    comes first, then the flags, and the push is bounded by a subprocess
    timeout so a machine that dies mid-join cannot stall the join forever."""
    seen = {}

    def fake_run(args, capture=False, quiet_stderr=False, timeout=None):
        seen["args"] = args
        seen["timeout"] = timeout
        return ""

    monkeypatch.setattr(talosctl, "_run", fake_run)

    talosctl.apply_config_insecure("172.29.21.5", "version: v1alpha1")

    assert seen["args"] == [
        "apply-config", "--insecure", "-n", "172.29.21.5",
        "--file", seen["args"][5],
    ]
    assert seen["timeout"] == talosctl.PROBE_TIMEOUT_S


def _reset_args(monkeypatch, **kw) -> list[str]:
    """Run `reset` against a stubbed subprocess and return the argv it built."""
    captured: list[list[str]] = []

    def _nocheck(args, timeout=None):
        captured.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(talosctl, "_run_nocheck", _nocheck)
    talosctl.reset(Path("/nonexistent/talosconfig"), "10.0.0.1", "10.0.0.2", **kw)
    return captured[0]


def test_reset_wipes_a_vm_whole_and_shuts_it_down(monkeypatch):
    """The default is right for a VM the provider deletes seconds later: the
    whole system disk goes (talosctl's `--wipe-mode all` default) and the
    machine shuts down rather than rebooting."""
    args = _reset_args(monkeypatch)
    assert "--reboot=false" in args
    assert "--system-labels-to-wipe" not in args


def test_reset_to_maintenance_keeps_the_install_and_reboots(monkeypatch):
    """Hardware is deleted by nothing, so a scaled-down machine must be left
    reusable: only the cluster's identity and data go (STATE, EPHEMERAL), the
    Talos install stays, and the reboot brings it up with no machine config --
    maintenance mode, ready to join another cluster without a reinstall."""
    args = _reset_args(monkeypatch, to_maintenance=True)
    assert "--reboot=true" in args
    assert "--reboot=false" not in args
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--system-labels-to-wipe"]
    assert labels == ["STATE", "EPHEMERAL"]

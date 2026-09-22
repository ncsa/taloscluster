"""Pin the 2026-09-07 doc/docstring-drift fixes.

A mix of prose, docstrings and CLI messages drifted from the code over time.
These tests keep the surviving wording honest so the drift does not resurface:

- `sdn.exit_nodes` defaults to every cluster node, offline included.
- The managed-SDN docs example uses a placeholder, not the real host `phoenix`.
- The VIP move is not claimed to happen "without a reboot".
- The scaffold comment no longer says every node gets `ncsa/project`.
- The reachability-timeout message points at the troubleshooting guide, not the
  removed README section.
- Module docstrings no longer lean on the removed terraform / shell / yq tooling
  or an OpenStack-only connection.
- The configuration overview does not self-contradict the refused-key behavior
  or imply the plugin validate hook only runs against a plugin's own section
  when its hooks run, and it covers nested fixed-schema blocks such as
  `proxmox.network` in the same breath as the top-level sections.
- network.md describes the gateway's metal consumers instead of "the
  statically addressed machines the bare-metal support will add", machines.md
  describes the bare-metal machines by their `metal:` section, the Proxmox
  token secret is refused when a command needs it rather than "at secrets
  load time", and the one-line descriptions and the quickstart/usage pages
  cover bare metal.
- The tailscale and OpenStack credential pages state the same access-time
  refusal contract as the configuration overview, not "at secrets load time".
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TALOSCLUSTER = ROOT / "taloscluster"
DOCS = ROOT / "docs"


def test_proxmox_sdn_docstring_includes_offline_nodes():
    src = (TALOSCLUSTER / "config.py").read_text()
    assert "every cluster node, offline included" in src
    assert "every online cluster node" not in src
    assert "every online node" not in src


def test_exit_nodes_docs_default_includes_offline_nodes():
    text = (DOCS / "configuration" / "proxmox.md").read_text()
    assert "every cluster node, offline included" in text
    assert "every online node" not in text


def test_managed_sdn_example_uses_a_placeholder_not_phoenix():
    text = (DOCS / "providers" / "proxmox.md").read_text()
    assert "name: mycl" in text
    assert "name: phoenix" not in text


def test_vip_move_is_not_claimed_to_avoid_a_reboot():
    checked = 0
    for path in (
        DOCS / "providers" / "proxmox.md",
        DOCS / "configuration" / "proxmox.md",
        DOCS / "configuration" / "network.md",
        DOCS / "usage.md",
    ):
        text = path.read_text()
        assert "without a reboot" not in text
        # only the pages that describe moving the VIP make the claim
        if not re.search(r"(Changing|Moving) .?kubeapi_vip|Changing it later moves", text):
            continue
        checked += 1
        assert (
            "not guaranteed to avoid a restart" in text
            or "may or may not settle without a restart" in text
        )
    assert checked, "no page describes moving the VIP any more"


def test_reachability_timeout_points_at_troubleshooting():
    src = (TALOSCLUSTER / "converge.py").read_text()
    assert "docs/troubleshooting.md" in src
    assert "README: headscale hygiene" not in src


def test_module_docstrings_drop_removed_tooling_and_openstack_only_claims():
    for name in (
        "config.py",
        "state.py",
        "cli.py",
        "naming.py",
        "errors.py",
        "output.py",
        "converge.py",
        "talos/talosctl.py",
        "talos/machineconfig.py",
        "talos/factory.py",
        "openstack/compute.py",
        "openstack/session.py",
        "context.py",
        "openstack/backend.py",
    ):
        text = (TALOSCLUSTER / name).read_text()
        assert "terraform" not in text
        assert "README-python.md" not in text
        assert "bin/cluster.sh" not in text
        assert "cluster.sh" not in text
        # no lingering "python rewrite ... on OpenStack" / yq framing
        assert "pure-Python alternative to the terraform" not in text


def test_package_docstring_describes_both_providers():
    text = (TALOSCLUSTER / "__init__.py").read_text()
    assert "OpenStack or Proxmox" in text
    assert "See README-python.md" not in text


def test_configuration_overview_refused_keys_are_not_self_contradicting():
    text = (DOCS / "configuration.md").read_text()
    # The old "no longer loads and is silently dropped" joined the new behavior
    # ("no longer loads") to the old one ("is silently dropped") in one clause.
    assert "no longer loads and is silently dropped" not in text
    assert "no longer load quietly" in text
    # The old behavior ("used to silently fall back") stays, clearly marked.
    assert "used to silently fall back" in text
    # Nested fixed-schema blocks are covered here, not just on the Proxmox page.
    assert "proxmox.network" in text


def test_cli_help_strings_are_provider_neutral():
    src = (TALOSCLUSTER / "cli.py").read_text()
    # The plan / check / image / dashboard descriptions no longer single out one
    # provider, and the non-Tailscale dashboard path is described too.
    assert "Changes nothing in OpenStack or the cluster" not in src
    assert "needs no OpenStack credentials" not in src
    assert "upload to Glance" not in src
    assert "delete from Glance" not in src
    assert "Requires this machine to be on the tailnet" not in src
    assert "on the tailnet" in src
    assert "else on the cluster network" in src


def test_command_prose_is_provider_neutral():
    # print_env / destroy / image prose in converge.py and the errors/context
    # docstrings describe the generic provider, not OpenStack alone.
    converge = (TALOSCLUSTER / "converge.py").read_text()
    assert "OS_* auth exports" not in converge
    assert "the same application credential taloscluster does" not in converge
    assert "before OpenStack teardown" not in converge
    assert "to Glance" not in converge
    assert "from Glance" not in converge
    errors = (TALOSCLUSTER / "errors.py").read_text()
    assert "An OpenStack resource" not in errors
    context = (TALOSCLUSTER / "context.py").read_text()
    assert "needs an OpenStack connection" not in context


def test_pyproject_description_is_provider_neutral():
    text = (ROOT / "pyproject.toml").read_text()
    # The package description no longer names the removed terraform/cluster.sh
    # workflow; taloscluster also drives Proxmox now.
    assert "terraform" not in text
    assert "OpenStack or Proxmox" in text


def test_keystoneauth1_is_a_declared_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    # backend.py imports keystoneauth1.exceptions directly; it must be a declared
    # dependency, not a transitive one, so the import does not silently break.
    assert "keystoneauth1" in text


def test_urllib3_is_a_declared_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    # metal/redfish.py imports urllib3 directly to silence insecure-request
    # warnings; it must be a declared dependency, not a transitive one, so the
    # import does not silently break.
    assert "urllib3" in text


def test_changelog_latest_release_matches_pyproject_version():
    # The released pyproject version must have a matching dated CHANGELOG
    # heading; a Breaking entry under an unversioned heading with an old
    # pyproject version means the release bump was forgotten.
    pyproject = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert match, "pyproject.toml has no version"
    changelog = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    assert headings, "CHANGELOG.md has no release headings"
    assert headings[0] == match.group(1)


def test_configuration_overview_plugin_validate_runs_everywhere_up_front():
    text = (DOCS / "configuration.md").read_text()
    # The plugin `validate` hook runs in converge's validate phase for every
    # installed plugin before any mutation, not only against an active plugin's
    # own section when its hooks run.
    assert "each plugin validates the keys inside its own section when its hooks run" not in text
    assert "validate" in text
    assert "before any mutation" in text
    assert "every installed plugin" in text


def test_gateway_and_machines_docs_do_not_describe_metal_as_future_work():
    # network.md said the gateway is "accepted and validated for the statically
    # addressed machines the bare-metal support will add"; metal support exists
    # and its groups consume the value now. machines.md must likewise describe
    # the bare-metal machines by their `metal:` section, not the pools.
    network = (DOCS / "configuration" / "network.md").read_text()
    assert "will add" not in network
    gateway = network.split("### `network.cluster.gateway`", 1)[1].split("\n### ", 1)[0]
    assert "Metal" in gateway
    machines = (DOCS / "concepts" / "machines.md").read_text()
    assert "the [pools](../configuration/pools.md) describe them" not in machines
    assert "the [`metal`](../configuration/metal.md) section describes them" in machines


def test_proxmox_token_secret_refusal_is_not_tied_to_a_secrets_load():
    # There is no separate secrets load: secrets.yaml is merged into
    # cluster.yaml, and the configuration overview states the real contract --
    # the refusal happens when a command needs the credential.
    text = (DOCS / "configuration" / "proxmox.md").read_text()
    assert "secrets load time" not in text
    assert "refused when a command needs the credential" in text


def test_tailscale_and_openstack_credential_refusals_are_not_tied_to_a_secrets_load():
    # The tailscale and OpenStack pages drifted back to "at secrets load time"
    # after the Proxmox page was fixed; _require_secret runs only when the
    # credential is accessed, as the configuration overview states.
    tailscale = (DOCS / "configuration" / "tailscale.md").read_text()
    assert "secrets load time" not in tailscale
    assert "refused when a command needs the key" in tailscale
    for path in (
        DOCS / "configuration" / "openstack.md",
        DOCS / "providers" / "openstack.md",
    ):
        text = path.read_text()
        assert "secrets load time" not in text
        assert "when a command needs" in text


def test_descriptions_cover_bare_metal():
    # The one-line descriptions said OpenStack/Proxmox only; the tool also
    # brings bare-metal machines alongside the VM provider, so they must cover
    # bare metal, and the quickstart and usage pages must say how those
    # machines are prepared and joined.
    for path in (
        ROOT / "README.md",
        DOCS / "index.md",
        ROOT / "mkdocs.yml",
        ROOT / "pyproject.toml",
    ):
        assert "bare metal" in path.read_text(), path
    assert "bare-metal machines" in (DOCS / "quickstart.md").read_text()
    assert "`metal join`" in (DOCS / "usage.md").read_text()

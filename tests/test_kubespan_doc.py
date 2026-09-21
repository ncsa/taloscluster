"""Keep the KubeSpan documentation in lockstep with the behavior it documents.

``talos.kubespan`` turns on Talos's WireGuard overlay, which is what lets one
cluster span layer-2 networks -- and what it deliberately does not carry: the
Kubernetes API VIP and the management path still need plain routing, and peer
discovery needs egress, direct or through ``machine.env`` proxies. These tests
pin the mixed-provider reachability contract in the two pages against the
emitted machine configuration so neither page can drift from what the tool
actually does.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from taloscluster.config import Config
from taloscluster.talos.machineconfig import KUBESPAN_MTU_OVERHEAD

ROOT = Path(__file__).resolve().parent.parent
GENERAL = ROOT / "docs" / "configuration" / "general.md"
TALOS = ROOT / "docs" / "concepts" / "talos.md"

KUBESPAN_DEFAULT = next(
    field.default for field in dataclasses.fields(Config) if field.name == "kubespan"
)


def test_general_page_documents_the_emitted_settings():
    text = GENERAL.read_text()
    assert "### `talos.kubespan`" in text
    # the default matches the loader, and the documented WireGuard overhead
    # matches the constant the emitted MTU is computed with
    assert KUBESPAN_DEFAULT is False
    assert f"default `{str(KUBESPAN_DEFAULT).lower()}`" in text
    assert f"{KUBESPAN_MTU_OVERHEAD} bytes of WireGuard overhead" in text
    # the overlay only comes on with an explicit opt-in
    assert "Set it to `true`" in text


def test_general_page_states_the_mixed_provider_reachability_contract():
    text = GENERAL.read_text()
    # the overlay is what carries pod traffic between the node networks
    assert "span layer-2 networks" in text
    assert "discovery service" in text
    # KubeSpan does not carry the API VIP; the kubelet reaches it directly
    assert "does not carry the Kubernetes API VIP" in text
    assert "network.cluster.kubeapi_vip" in text
    assert "reachable from the metal L2" in text
    # the two documented ways of making the VIP reachable from the metal side
    assert "floating IP" in text
    assert "exit-node routing" in text
    # peer discovery traverses machine.env proxies when there is no egress
    assert "machine.env" in text
    assert "HTTPS_PROXY" in text
    assert "proxy.example.edu" in text  # examples use placeholder hostnames


def test_talos_page_states_what_kubespan_does_and_does_not_cover():
    text = TALOS.read_text()
    assert "## One pod network across networks" in text
    # what it covers: one pod network over nodes on different layer-2 networks
    assert "span layer-2 networks" in text
    # what it does not: the API VIP, and management traffic
    assert "kubelet on every node still reaches `kubeapi_vip` directly" in text
    assert "management access path" in text
    assert "floating IP or exit-node routing" in text
    assert "machine.env" in text
    # the page hands the operator requirements to the configuration reference
    assert "../configuration/general.md#taloskubespan" in text
    assert "machines.md#reaching-the-nodes" in text

"""Destructive-action confirmation and final health-check safety."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from taloscluster import converge
from taloscluster.errors import ReconcileError
from taloscluster.infrastructure import (
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
)


class FakeBackend:
    name = "openstack"

    def __init__(self, mutations=None):
        self.mutations = mutations if mutations is not None else []

    def load_inventory(self):
        return InfrastructureInventory()

    def delete_machine(self, *_args):
        self.mutations.append("compute")

    def destroy_summary(self, _inventory):
        return "0 servers, 0 ports, 0 floating ips, network + router + security group"

    def destroy_resources(self, _inventory):
        self.mutations.append("destroy")


def test_scale_down_decline_happens_before_mutation(monkeypatch):
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit, match="aborted"):
        converge._scale_down(
            FakeBackend(mutations), cfg, {}, InfrastructureInventory(), NetworkResult(),
            Path("talosconfig"),
            Path("kubeconfig"), assume_yes=False,
        )

    assert mutations == []


def test_scale_down_yes_skips_prompt_and_deletes(monkeypatch):
    cfg = SimpleNamespace(name="testcluster", controlplane={"count": 3})
    mutations: list[str] = []
    monkeypatch.setattr(converge.kubectl, "node_names", lambda _kc: ["old-worker"])
    monkeypatch.setattr(converge.kubectl, "drain", lambda *_a: mutations.append("drain"))
    monkeypatch.setattr(converge.talosctl, "reset", lambda *_a: mutations.append("reset"))
    monkeypatch.setattr(converge.kubectl, "delete_node", lambda *_a: mutations.append("delete"))
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    inventory = InfrastructureInventory(
        machines={
            "old-worker": InfrastructureMachine(
                "old-worker",
                attachments=(NetworkAttachment("private", "192.0.2.10"),),
            )
        }
    )
    converge._scale_down(
        FakeBackend(mutations), cfg, {}, inventory, NetworkResult(), Path("talosconfig"),
        Path("kubeconfig"), assume_yes=True,
    )

    assert mutations == ["drain", "reset", "delete", "compute"]


def test_destroy_decline_happens_before_plugin_teardown(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="testcluster")
    plugin_calls: list[str] = []
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: object())
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(
        converge, "_run_plugins", lambda *_a, **_kw: plugin_calls.append("destroy") or 0
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit, match="aborted"):
        converge.destroy(tmp_path)

    assert plugin_calls == []


def test_destroy_yes_skips_prompt_and_runs_plugin_teardown(monkeypatch, tmp_path):
    cfg = SimpleNamespace(name="testcluster")
    plugin_calls: list[str] = []
    monkeypatch.setattr(converge, "load_config", lambda _root: cfg)
    monkeypatch.setattr(converge, "load_secrets", lambda _root: object())
    backend = FakeBackend()
    monkeypatch.setattr(converge, "backend_for", lambda *_a: backend)
    monkeypatch.setattr(
        converge, "_run_plugins", lambda *_a, **_kw: plugin_calls.append("destroy") or 0
    )
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("--yes must not prompt")
    )

    assert converge.destroy(tmp_path, assume_yes=True) == 0
    assert plugin_calls == ["destroy"]
    assert backend.mutations == ["destroy"]


def test_final_health_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(converge, "_health_or_kube_fallback", lambda *_a, **_kw: False)

    with pytest.raises(ReconcileError, match="unhealthy"):
        converge._require_final_health(
            Path("talosconfig"), "testcluster-controlplane-01", "192.0.2.5",
            Path("kubeconfig"),
        )

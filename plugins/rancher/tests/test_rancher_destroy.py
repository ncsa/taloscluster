"""The rancher plugin's destroy refuses to delete an unrelated cluster.

Rancher is faked at the Client boundary; the downstream id is faked the same way
tests for converge do. Destroy must only delete a Rancher cluster whose id
matches the downstream cluster's cattle-cluster-agent id.
"""

from __future__ import annotations

import pytest
import yaml
from taloscluster.context import Context

from taloscluster_rancher import reconcile
from taloscluster_rancher.client import RancherCluster
from taloscluster_rancher.errors import RancherError

CLUSTER = RancherCluster(id="c-abc12", name="testcluster", state="active")


class FakeClient:
    def __init__(self, cluster=CLUSTER):
        self._cluster = cluster
        self.deleted = None

    def find_cluster(self, name):
        return self._cluster

    def delete_cluster(self, cluster_id):
        self.deleted = cluster_id


@pytest.fixture
def cluster_dir(tmp_path):
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump({
        "name": "testcluster",
        "include": ["secrets.yaml"],
        "rancher": {"admins": ["alice"], "users": ["carol"]},
    }))
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({
        "rancher": {"url": "https://rancher.example.com", "token": "token-x:y"},
    }))
    return tmp_path


@pytest.fixture
def wire(monkeypatch, cluster_dir):
    removed = []

    def _wire(downstream_id, cluster=CLUSTER):
        client = FakeClient(cluster=cluster)
        monkeypatch.setattr(reconcile, "_client", lambda secrets: client)
        monkeypatch.setattr(reconcile, "downstream_rancher_id",
                            lambda root: downstream_id)
        monkeypatch.setattr(reconcile, "_remove_agent",
                            lambda root: removed.append(root))
        return client, removed

    return _wire


def test_destroy_deletes_when_downstream_id_matches(cluster_dir, wire):
    client, removed = wire(downstream_id="c-abc12")
    reconcile.destroy(Context(root=cluster_dir, cfg=None))
    assert client.deleted == "c-abc12"
    assert removed == [cluster_dir]


def test_destroy_refuses_when_downstream_id_mismatches(cluster_dir, wire):
    client, removed = wire(downstream_id="c-other99")
    with pytest.raises(RancherError, match="does not match the downstream"):
        reconcile.destroy(Context(root=cluster_dir, cfg=None))
    assert client.deleted is None
    assert removed == []


def test_destroy_refuses_when_no_downstream_agent(cluster_dir, wire):
    client, removed = wire(downstream_id=None)
    with pytest.raises(RancherError, match="no cattle-cluster-agent"):
        reconcile.destroy(Context(root=cluster_dir, cfg=None))
    assert client.deleted is None
    assert removed == []


def test_destroy_removes_orphaned_agent_when_no_rancher_cluster(cluster_dir, wire):
    """An agent whose id matches no Rancher cluster bearing the name is orphaned;
    destroy must uninstall it from the downstream cluster instead of claiming
    there is nothing to remove."""
    client, removed = wire(downstream_id="c-orphan", cluster=None)
    reconcile.destroy(Context(root=cluster_dir, cfg=None))
    assert client.deleted is None
    assert removed == [cluster_dir]

"""Redaction of dry-run previews in taloscluster.output."""

from __future__ import annotations

from taloscluster import output


def test_redact_masks_secret_looking_keys_at_depth():
    values = {
        "replicas": 1,
        "auth": {"password": "hush", "token": "t", "nested": {"apiKey": "k", "plain": "v"}},
        "secretName": "s",
        "items": [{"credential": "c", "name": "n"}],
    }
    out = output.redact(values)
    assert out["replicas"] == 1
    assert out["auth"]["password"] == "REDACTED"
    assert out["auth"]["token"] == "REDACTED"
    assert out["auth"]["nested"]["apiKey"] == "REDACTED"
    assert out["auth"]["nested"]["plain"] == "v"
    assert out["secretName"] == "REDACTED"
    assert out["items"] == [{"credential": "REDACTED", "name": "n"}]
    assert values["auth"]["password"] == "hush"  # display only


def test_redact_masks_every_value_of_a_kubernetes_secret():
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "csi-rbd-secret", "namespace": "ceph-csi-rbd"},
        "stringData": {"userID": "kubernetes", "userKey": "AQC0"},
        "data": {"cloud.conf": "W0dsb2JhbF0="},
    }
    out = output.redact(doc)
    assert out["metadata"] == {"name": "csi-rbd-secret", "namespace": "ceph-csi-rbd"}
    assert out["stringData"] == {"userID": "REDACTED", "userKey": "REDACTED"}
    assert out["data"] == {"cloud.conf": "REDACTED"}
    # a ConfigMap's data is not a secret block; only its credential-looking keys are
    cm = {"kind": "ConfigMap", "data": {"config": "x", "password": "y"}}
    assert output.redact(cm)["data"] == {"config": "x", "password": "REDACTED"}


def test_redact_extra_keys():
    assert output.redact({"pin": "1234", "name": "n"}, extra_keys=["pin"]) == {
        "pin": "REDACTED", "name": "n",
    }


def test_show_yaml_redacts_multi_document_manifests(capsys):
    manifest = (
        "---\nkind: Secret\nstringData:\n  userKey: AQC0\n"
        "---\nkind: Namespace\nmetadata:\n  name: ns\n"
    )
    output.show_yaml(manifest)
    out = capsys.readouterr().out
    assert "AQC0" not in out
    assert "userKey: REDACTED" in out
    assert "name: ns" in out
    assert out.count("---") == 2


def test_show_yaml_redacts_values_mapping(capsys):
    output.show_yaml({"replicas": 1, "auth": {"password": "hush"}})
    out = capsys.readouterr().out
    assert "hush" not in out
    assert "replicas: 1" in out
    assert "---" not in out

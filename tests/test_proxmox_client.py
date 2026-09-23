"""Proxmox token transport, retries, and asynchronous task behavior."""

from __future__ import annotations

import pytest
import requests

from taloscluster.errors import ReconcileError
from taloscluster.proxmox.client import ProxmoxClient


class Response:
    def __init__(self, status: int, data=None, text: str = ""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        return {"data": self._data}


class Session:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_token_header_and_tls_verification_are_applied():
    session = Session([Response(200, {"version": "9.2.2"})])
    client = ProxmoxClient(
        "https://pve.example",
        "user@pve!provider",
        "secret",
        verify="/tmp/ca.pem",
        session=session,
    )

    assert client.get("version") == {"version": "9.2.2"}
    assert session.headers["Authorization"] == "PVEAPIToken=user@pve!provider=secret"
    assert session.calls[0][1] == "https://pve.example/api2/json/version"
    assert session.calls[0][2]["verify"] == "/tmp/ca.pem"


@pytest.mark.parametrize(
    "configured_url",
    [
        "https://pve.example:8006",
        "https://pve.example:8006/",
        "https://pve.example:8006/api2/json",
        "https://pve.example:8006/api2/json/",
    ],
)
def test_api_url_accepts_origin_and_legacy_api_path(configured_url):
    session = Session([Response(200, {"version": "9.2.2"})])
    client = ProxmoxClient(configured_url, "id", "secret", session=session)

    client.get("version")

    assert session.calls[0][1] == "https://pve.example:8006/api2/json/version"


def test_reads_retry_but_mutations_do_not(monkeypatch):
    monkeypatch.setattr("taloscluster.proxmox.client.time.sleep", lambda _delay: None)
    session = Session(
        [
            requests.ConnectionError("temporary"),
            Response(200, []),
            Response(503, None, "busy"),
        ]
    )
    client = ProxmoxClient("https://pve", "id", "secret", session=session)

    assert client.get("nodes") == []
    with pytest.raises(ReconcileError, match="503"):
        client.request("POST", "pools", data={"poolid": "test"})
    assert len(session.calls) == 3


def test_error_status_with_upid_is_polled_on_task_owner_node():
    upid = "UPID:pve001:00000001:00000002:00000003:qmdestroy:800:user@pve:"
    session = Session(
        [
            Response(501, upid),
            Response(200, {"status": "stopped", "exitstatus": "OK", "endtime": None}),
        ]
    )
    client = ProxmoxClient(
        "https://pve", "id", "secret", session=session, poll_interval=0
    )

    client.mutate("DELETE", "nodes/pve004/qemu/800")

    assert "/nodes/pve001/tasks/UPID%3Apve001" in session.calls[1][1]


def test_failed_task_reports_exit_status():
    upid = "UPID:pve001:1:2:3:qmcreate:800:user@pve:"
    session = Session(
        [Response(200, upid), Response(200, {"status": "stopped", "exitstatus": "ERROR"})]
    )
    client = ProxmoxClient(
        "https://pve", "id", "secret", session=session, poll_interval=0
    )

    with pytest.raises(ReconcileError, match="ERROR"):
        client.mutate("POST", "nodes/pve001/qemu")


def test_warnings_exit_status_is_treated_as_success():
    upid = "UPID:pve001:1:2:3:qmcreate:800:user@pve:"
    session = Session(
        [
            Response(200, upid),
            Response(200, {"status": "stopped", "exitstatus": "WARNINGS: 3"}),
        ]
    )
    client = ProxmoxClient(
        "https://pve", "id", "secret", session=session, poll_interval=0
    )

    client.mutate("POST", "nodes/pve001/qemu")

    assert len(session.calls) == 2


def test_malformed_warnings_exit_status_is_still_a_failure():
    upid = "UPID:pve001:1:2:3:qmcreate:800:user@pve:"
    session = Session(
        [
            Response(200, upid),
            Response(200, {"status": "stopped", "exitstatus": "WARNINGS: nope"}),
        ]
    )
    client = ProxmoxClient(
        "https://pve", "id", "secret", session=session, poll_interval=0
    )

    with pytest.raises(ReconcileError, match="WARNINGS: nope"):
        client.mutate("POST", "nodes/pve001/qemu")


def test_wait_task_rejects_an_invalid_upid():
    client = ProxmoxClient("https://pve", "id", "secret", session=Session([]))

    with pytest.raises(ReconcileError, match="invalid Proxmox task id"):
        client.wait_task("not-a-upid")


def test_wait_task_raises_when_the_task_never_stops(monkeypatch):
    monkeypatch.setattr("taloscluster.proxmox.client.time.sleep", lambda _delay: None)
    session = Session([Response(200, {"status": "running"})])
    client = ProxmoxClient(
        "https://pve", "id", "secret", session=session, task_timeout=5, poll_interval=0
    )
    clock = iter([0.0, 0.0, 6.0])  # in-deadline sample, then timeout at 0 + 5
    monkeypatch.setattr("taloscluster.proxmox.client.time.monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="did not finish within 5s"):
        client.wait_task("UPID:pve001:1:2:3:qmcreate:800:user@pve:")

    # The task was polled at least once before the deadline expired, and the
    # "running" status response was consumed.
    assert "/tasks/" in session.calls[0][1]

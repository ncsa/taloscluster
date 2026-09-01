"""Small Proxmox VE API client with token auth and centralized task polling."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import requests

from ..errors import ReconcileError

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_API_PATH = "/api2/json"


class ProxmoxClient:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        *,
        verify: bool | str = True,
        session: requests.Session | None = None,
        read_attempts: int = 3,
        task_timeout: int = 900,
        poll_interval: float = 1.0,
    ) -> None:
        base_url = url.rstrip("/")
        self.url = base_url if base_url.endswith(_API_PATH) else base_url + _API_PATH
        self.verify = verify
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        )
        self.read_attempts = max(read_attempts, 1)
        self.task_timeout = task_timeout
        self.poll_interval = poll_interval

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        attempts = self.read_attempts if method == "GET" else 1
        response: requests.Response | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    f"{self.url}/{path.lstrip('/')}",
                    params=params,
                    data=data,
                    files=files,
                    timeout=(10, 120),
                    verify=self.verify,
                )
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise ReconcileError(f"Proxmox API request failed: {exc}") from exc

            payload: Any = None
            try:
                body = response.json()
                payload = body.get("data") if isinstance(body, dict) else None
            except ValueError:
                body = None

            # Proxmox 9 can return an error HTTP status with a valid asynchronous
            # task in the response body (notably qmdestroy). The task is the
            # authoritative result and still has to be polled.
            if response.status_code >= 400:
                if isinstance(payload, str) and payload.startswith("UPID:"):
                    return payload
                if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                detail = _error_detail(body, response.text)
                raise ReconcileError(
                    f"Proxmox API {method} {path} failed "
                    f"({response.status_code}): {detail}"
                )
            return payload

        raise ReconcileError(f"Proxmox API {method} {path} did not return a response")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def mutate(self, method: str, path: str, **kw: Any) -> Any:
        result = self.request(method, path, **kw)
        if isinstance(result, str) and result.startswith("UPID:"):
            self.wait_task(result)
        return result

    def wait_task(self, upid: str) -> None:
        parts = upid.split(":")
        if len(parts) < 3 or parts[0] != "UPID" or not parts[1]:
            raise ReconcileError(f"invalid Proxmox task id: {upid!r}")
        node = parts[1]
        path = f"nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}/status"
        deadline = time.monotonic() + self.task_timeout
        while time.monotonic() < deadline:
            status = self.get(path)
            if isinstance(status, dict) and status.get("status") == "stopped":
                exit_status = status.get("exitstatus")
                if exit_status != "OK":
                    raise ReconcileError(
                        f"Proxmox task {upid} failed: {exit_status or 'unknown status'}"
                    )
                return
            time.sleep(self.poll_interval)
        raise TimeoutError(f"Proxmox task {upid} did not finish within {self.task_timeout}s")


def _error_detail(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict):
            return "; ".join(f"{key}: {value}" for key, value in sorted(errors.items()))
        if body.get("message"):
            return str(body["message"])
    return fallback.strip() or "unknown error"

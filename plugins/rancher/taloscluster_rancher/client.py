"""Thin, typed wrapper around the Rancher v3 management API.

Only the handful of endpoints the tool needs:
  - clusters (find by name / create an import cluster)
  - clusterRegistrationTokens (fetch the import manifest to install the agent)
  - principals (resolve a netid/username to a user principal id via search)
  - clusterroletemplatebindings (list / create / delete cluster members)

Auth is a Rancher bearer token (`token-xxxxx:yyyyyyyyyyyy`) in the
`Authorization: Bearer` header. TLS certificate verification is enabled by
default; pass verify=False only for a server with a self-signed cert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests
from taloscluster.output import action, dry_run, info

from .errors import RancherError

TIMEOUT = 60


def _manifest_url(shell: str) -> str | None:
    """Extract the manifest `.yaml` URL from a Rancher import command string."""
    for tok in shell.split():
        cleaned = tok.rstrip("'\"")
        if cleaned.startswith("https://") and cleaned.endswith(".yaml"):
            return cleaned
    return None


@dataclass(frozen=True)
class RancherCluster:
    """A Rancher cluster (import cluster)."""

    id: str               # e.g. c-xxxxx
    name: str
    state: str            # e.g. provisioning / active
    namespace_ready: bool = False   # backing namespace created


@dataclass(frozen=True)
class MemberBinding:
    """A clusterroletemplatebinding row."""

    id: str
    userPrincipalId: str | None   # LDAP principal DN (the id we bind by)
    groupPrincipalId: str | None
    roleTemplateId: str


class Client:
    def __init__(self, url: str, token: str, verify: bool = True) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.verify = verify
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    # ------------------------------------------------------------------ HTTP
    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict[str, Any]:
        url = f"{self.url}{path}"
        try:
            resp = self.session.request(
                method, url, params=params, json=json, timeout=TIMEOUT, verify=self.verify
            )
        except requests.RequestException as e:
            raise RancherError(f"{method} {path} failed: {e}") from e
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            raise RancherError(f"{method} {path} returned {resp.status_code}: {detail}")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise RancherError(f"{method} {path} returned non-JSON body") from e

    def _get(self, path: str, **kw) -> dict[str, Any]:
        return self._request("GET", path, **kw)

    def _post(self, path: str, json: dict, **kw) -> dict[str, Any]:
        return self._request("POST", path, json=json, **kw)

    def _delete(self, path: str, **kw) -> None:
        self._request("DELETE", path, **kw)

    # ------------------------------------------------------------- clusters
    def find_cluster(self, name: str) -> RancherCluster | None:
        data = self._get("/v3/clusters", params={"filter": f"name={name}"})
        matches = [
            RancherCluster(id=c["id"], name=c["name"], state=c.get("state", ""))
            for c in data.get("data", [])
            if c.get("name") == name
        ]
        if len(matches) > 1:
            ids = ", ".join(c.id for c in matches)
            raise RancherError(
                f"multiple Rancher clusters named {name!r} ({ids}); "
                "rename one to disambiguate"
            )
        return matches[0] if matches else None

    def create_import_cluster(self, name: str) -> RancherCluster:
        data = self._post("/v3/clusters", {"type": "cluster", "name": name})
        cid = data.get("id")
        if not cid:
            raise RancherError(f"creating import cluster {name}: no id in response")
        return RancherCluster(id=cid, name=name, state=data.get("state", ""))

    def delete_cluster(self, cluster_id: str) -> None:
        """Remove an import cluster from Rancher entirely."""
        if dry_run():
            action(f"delete cluster {cluster_id} from Rancher")
            return
        action(f"deleting cluster {cluster_id} from Rancher")
        self._delete(f"/v3/clusters/{cluster_id}")

    def ensure_cluster(self, name: str, downstream_id: str | None = None) -> RancherCluster:
        """Return/create the import cluster for `name`.

        `downstream_id` is the Rancher cluster id the *downstream* cluster is
        registered under (read from its cattle-cluster-agent). It lets us tell
        apart:
          - a re-run on our own cluster: existing Rancher id == downstream_id
            -> reuse it;
          - a freshly-created but half-imported cluster (`pending`, no agent):
            recover it — an interrupted prior run left it pending; continue its
            import;
          - anything else that merely shares the name (a healthy, or a
            live-but-disconnected, unrelated cluster with no matching agent):
            abort rather than attach to it.
        If no Rancher cluster named `name` exists, a new import cluster is created.
        """
        existing = self.find_cluster(name)
        if existing is not None:
            if downstream_id and downstream_id == existing.id:
                info(f"cluster {name} already registered as ours ({existing.id})")
                return existing
            if downstream_id:
                raise RancherError(
                    f"Rancher cluster {name!r} ({existing.id}) does not match the "
                    f"downstream cluster ({downstream_id}); refuse to attach to it"
                )
            if existing.state == "pending":
                info(f"recovering stale import cluster {name} ({existing.id}, {existing.state})")
                return existing
            raise RancherError(
                f"a Rancher cluster named {name!r} already exists ({existing.id}) and "
                "its downstream cluster has no Rancher agent, so it is not this "
                "cluster; choose a unique name or delete the existing cluster"
            )
        if dry_run():
            action(f"create import cluster {name} in Rancher")
            return RancherCluster(id="<dry-run>", name=name, state="provisioning")
        action(f"registering cluster {name} with Rancher")
        return self.create_import_cluster(name)

    # ------------------------------------------------------- cluster import
    def get_cluster(self, cluster_id: str) -> RancherCluster:
        c = self._get(f"/v3/clusters/{cluster_id}")
        namespace_ready = "BackingNamespaceCreated" in {
            cond.get("type")
            for cond in (c.get("conditions") or [])
            if cond.get("status")
        }
        return RancherCluster(
            id=c["id"],
            name=c.get("name", ""),
            state=c.get("state", ""),
            namespace_ready=namespace_ready,
        )

    def wait_for_namespace(self, cluster_id: str, timeout: float = 120,
                           poll: float = 2.0) -> None:
        """Wait until the import cluster's backing namespace is created.

        Right after `POST /v3/clusters` Rancher reports `pending`; the backing
        namespace is created a moment later and a registration token POST 404s
        until it exists (`namespaces "<id>" not found`). An import cluster stays
        `pending` (waiting for the agent to connect), so we key off the namespace
        condition, not cluster state.
        """
        deadline = time.monotonic() + timeout
        while True:
            cluster = self.get_cluster(cluster_id)
            if cluster.namespace_ready:
                return
            if time.monotonic() >= deadline:
                raise RancherError(
                    f"timed out waiting for namespace of cluster {cluster_id} to be created"
                )
            time.sleep(poll)

    def fetch_import_command(self, cluster: RancherCluster) -> str:
        """Return the import manifest (raw YAML) to apply to the downstream cluster.

        The registration token's `command` is a shell invocation (e.g. `kubectl
        apply -f <url>`); we extract the manifest URL and fetch its YAML so it can
        be piped straight into `kubectl apply -f -`. Falls back to the raw
        `` command text if no URL is present.
        """
        if dry_run():
            return ""
        self.wait_for_namespace(cluster.id)
        self._post(
            "/v3/clusterRegistrationTokens",
            {"type": "clusterRegistrationToken", "clusterId": cluster.id},
        )
        data = self._get(f"/v3/clusters/{cluster.id}/clusterRegistrationTokens")
        rows = data.get("data", [])
        if not rows:
            raise RancherError(f"no registration token for cluster {cluster.id}")
        row = rows[-1]
        shell = row.get("command") or row.get("insecureCommand") or ""
        url = _manifest_url(shell)
        if url:
            return self._fetch_yaml(url)
        return shell

    def _fetch_yaml(self, url: str) -> str:
        try:
            resp = self.session.get(url, timeout=TIMEOUT, verify=self.verify)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise RancherError(f"fetching import manifest from {url} failed: {e}") from e

    # ---------------------------------------------------------------- users
    def resolve_principal(self, name: str) -> dict[str, Any] | None:
        """Resolve a username/netid to a Rancher user principal.

        Uses the Rancher principal-search action (`POST /v3/principals?action=search`
        with `{"name": ...}`), which matches against the configured auth providers
        without hardcoding any LDAP DN layout. An email suffix is stripped so
        `alice@example.com` resolves like `alice`. Returns the first user
        principal whose id matches, or None if none is found.
        """
        stem = name.split("@", 1)[0]
        data = self._post("/v3/principals?action=search", {"name": stem})
        for p in data.get("data", []):
            pid: str | None = p.get("id")
            if p.get("principalType") == "user" and pid:
                id_stem = pid.rsplit("//", 1)[-1].split(",")[0].split("=")[-1]
                if id_stem == stem:
                    return p
                info(f"principal {pid!r} matches search, using as {name!r}")
                return p
        return None

    # ------------------------------------------------- role template bindings
    def list_member_bindings(self, cluster_id: str) -> list[MemberBinding]:
        data = self._get(
            "/v3/clusterroletemplatebindings",
            params={"clusterId": cluster_id},
        )
        return [
            MemberBinding(
                id=row["id"],
                userPrincipalId=row.get("userPrincipalId"),
                groupPrincipalId=row.get("groupPrincipalId"),
                roleTemplateId=row.get("roleTemplateId", ""),
            )
            for row in data.get("data", [])
        ]

    def add_member(self, cluster_id: str, principal_id: str, role: str) -> None:
        if dry_run():
            action(f"add member {principal_id} as {role} to {cluster_id}")
            return
        action(f"adding member {principal_id} as {role} to {cluster_id}")
        self._post(
            "/v3/clusterroletemplatebindings",
            {
                "clusterId": cluster_id,
                "userPrincipalId": principal_id,
                "roleTemplateId": role,
            },
        )

    def remove_member(self, binding_id: str) -> None:
        if dry_run():
            action(f"remove member binding {binding_id}")
            return
        action(f"removing member binding {binding_id}")
        self._delete(f"/v3/clusterroletemplatebindings/{binding_id}")


def _error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        msgs = body.get("message") or [e.get("message", "") for e in body.get("errors", [])]
        if isinstance(msgs, list):
            msgs = [m for m in msgs if m]
        return "; ".join(msgs) if msgs else resp.text[:300]
    except ValueError:
        return resp.text[:300]

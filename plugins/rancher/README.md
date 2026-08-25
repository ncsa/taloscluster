# taloscluster-rancher

A [taloscluster](../../README.md) plugin: register the cluster with Rancher and
reconcile its members from `cluster.yaml` and `secrets.yaml`.

## Install

```bash
uv tool install "taloscluster[rancher] @ git+https://github.com/ncsa/taloscluster"
```

Once installed it runs as part of `taloscluster converge` / `plan` / `destroy`
(and reports under `status` / `check`) — there is nothing extra to invoke.

## Configuration

`cluster.yaml` (committed) — `rancher:` holds the desired members as NCSA
netids/usernames:

```yaml
rancher:
  admins: [alice, bob]   # -> cluster-owner
  users:  [carol]        # -> cluster-member
```

`secrets.yaml` (gitignored) — `rancher:` holds the API endpoint + bearer token:

```yaml
rancher:
  url:   https://gonzo-rancher.ncsa.illinois.edu
  token: token-xxxxx:yyyyyyyyyyyy
```

## Running it on its own

```
taloscluster plugin rancher [converge|plan|destroy|status|check] [-C DIR]
```

- **converge** — register the cluster in Rancher (import) and install
  `cattle-cluster-agent` into the downstream cluster via kubectl (using the local
  `kubeconfig`); then **reconcile members**: add/repair every admin
  (`cluster-owner`) and user (`cluster-member`) binding, remove bindings for anyone
  no longer in the config (so taking a netid out of the list revokes that user's
  access to this cluster), and preserve the cluster creator/owner. **Name
  collisions are reconciled via the Rancher agent id:** if a Rancher cluster
  already bears the configured name, the tool reads the downstream cluster's
  `cattle-cluster-agent` id and reuses the existing cluster only when that id
  matches; if the downstream has no agent (or a different id) it aborts rather
  than attach to an unrelated cluster.
- **plan** — `converge --dry-run`: print every action converge would take without
  changing anything (it does read cluster.yaml / secrets.yaml and list Rancher
  state).
- **destroy** — **delete the cluster from Rancher** (not just members) and remove
  the Rancher agent (`cattle-system` namespace) from the downstream cluster via
  kubectl. Runs before the OpenStack teardown, while the cluster is still
  reachable.
- **status** — whether the cluster is registered, its Rancher id, whether the
  agent is installed, and the current member bindings.
- **check** — whether converge would change anything: not ok when the cluster is
  unregistered, the agent is missing, or the bindings differ from the config.

`converge` publishes `{cluster_id, url, members}`, which a plugin declaring
`AFTER = ("rancher",)` picks up — the argocd plugin uses the cluster id to
annotate its ArgoCD cluster Secret.

Members are resolved via Rancher's principal-search action
(`POST /v3/principals?action=search`), which matches each netid against the
configured auth providers (LDAP/SAML) and returns the principal id used in the
binding — no hardcoded directory layout. A member whose netid cannot be resolved
is skipped with a warning.

## Not configured

If `rancher:` is missing from **either** `cluster.yaml` (no admins/users) **or**
`secrets.yaml` (no url/token), the cluster is not managed by Rancher: the plugin
is skipped entirely, and `taloscluster plugin list` shows it as `not configured`.

TLS certificate verification is enabled by default; pass `verify=False` in
`taloscluster_rancher/client.py::Client` only if the Rancher server uses a self-signed cert.
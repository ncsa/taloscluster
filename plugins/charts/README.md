# taloscluster-charts

taloscluster plugin: installs helm charts (and plain manifests) into the
cluster as part of `taloscluster sync` / `plan` / `check` / `status` /
`destroy`.

Each entry under the `charts:` section of cluster.yaml is one chart or one set
of manifests. Charts the plugin knows by name ship common values; a `values:`
mapping on the entry overrides them. Values taloscluster already knows -- the
metallb ingress pool from `proxmox.network.external.ingress_pool` -- come from
the plugin Context and are never duplicated in config.

## Entries

Known entries: `gateway`, `metallb`, `traefik`, `cert-manager`, `sealed-secrets`,
`nfs` (consumes `storageClasses`) and `ceph` (consumes `clusterID`, `monitors`,
`rbd`, `fs` and the optional `userID`/`userKey` credentials, scaffolded into secrets.yaml under the same `charts.ceph` path). Any other entry must
set `repo` or `manifest` itself. Every key is documented in
[docs/configuration/charts.md](../../docs/configuration/charts.md).

## Behaviour

- `sync` is drift-driven: releases install/upgrade only when missing, a pinned
  version differs, or a `latest` entry has a newer chart version upstream.
  Applied manifests/namespaces/pool resources update only when drifted.
- `check` reports drift and, for `latest` entries, `upgrade_available`.
- `destroy` uninstalls the releases and removes the applied resources.
- `plan` shows every would-change action, with the merged values
  (secret-looking keys redacted) for releases that would install or upgrade and
  the resources it would apply. Up-to-date releases print a single line.

## Development

```sh
uv run pytest            # tests
uv run ruff check .      # lint
```

Install alongside taloscluster with the `charts` extra:

```sh
uv tool install "taloscluster[charts] @ git+https://github.com/ncsa/taloscluster"
taloscluster plugin list
```

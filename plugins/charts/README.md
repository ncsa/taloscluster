# taloscluster-charts

taloscluster plugin: installs helm charts (and plain manifests) into the cluster as part of `taloscluster sync` / `plan` / `check` / `status` / `destroy`.

Each entry under the `charts:` section of the merged configuration is one chart or one set of manifests. Charts the plugin knows by name ship common values; a `values:` mapping on the entry overrides them. Values taloscluster already knows — the metallb ingress pool, which comes from `network.external.ingress_pool` on Proxmox and from the provider's ingress VIP on OpenStack — come from the plugin Context and are never duplicated in config.

Every key, the validate-phase refusals and the drift rules are documented in [docs/configuration/charts.md](../../docs/configuration/charts.md).

## Entries

Known entries: `gateway`, `metallb`, `traefik`, `cert-manager`, `sealed-secrets`, `nfs` (consumes `storageClasses`) and `ceph` (consumes `clusterID`, `monitors`, and `rbd`/`fs` — booleans, or mappings with the pool / fsName that also create a StorageClass). Any other entry must set `repo` or `manifest` itself.

The optional `userID`/`userKey` CephX credentials are ordinary keys of the `ceph` entry: `init` scaffolds them under the same `charts.ceph` path in `secrets.yaml`, where they merge with the entry's other keys, and like every credential they may live in `cluster.yaml` or any included file.

The ceph values come from the Ceph cluster itself: `ceph fsid` (clusterID), `ceph mon dump` (monitors) and a dedicated CephX user created with `ceph auth get-or-create` whose key `ceph auth get-key` prints (userID/userKey). The exact commands and capabilities are in [docs/configuration/charts.md](../../docs/configuration/charts.md#gathering-the-values-from-ceph).

## Behaviour

- `sync` is drift-driven: releases install/upgrade only when missing, a pinned version differs, or a `latest` entry has a newer chart version upstream. Applied manifests/namespaces/pool resources update only when drifted.
- `check` reports drift and, for `latest` entries, `upgrade_available`.
- `destroy` uninstalls the releases and removes the applied resources.
- `plan` shows every would-change action, with the merged values (secret-looking keys redacted) for releases that would install or upgrade and the resources it would apply. Up-to-date releases print a single line.

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

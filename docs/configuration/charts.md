# Charts plugin

Back to the [configuration index](../configuration.md).

The charts plugin installs Helm charts and plain manifests into the cluster itself during converge, so a cluster gets its platform pieces (load balancer, ingress, certificates, storage) without an external GitOps server. It is skipped, and shown as `not configured` by `taloscluster plugin list`, unless the merged configuration enables at least one entry — or every entry is disabled while something is still installed for it to remove. `taloscluster init` scaffolds one with every known chart present but `enabled: false`, which leaves the plugin inactive; flipping `enabled` is the whole activation story. An enabled entry needs `helm` and `kubectl` on the management machine's PATH. See [Plugins](../concepts/plugins.md#how-plugins-run) for what converge, plan, destroy, status and check do.

Converge is drift-driven: a release is installed or upgraded only when it is missing, a pinned `version` differs from the installed chart (compared without the leading `v` some charts tag, so `1.21.2` and `v1.21.2` are the same version), a `latest` entry has a newer chart version upstream, the merged values differ from what the release was last installed with, or the release is not in helm's `deployed` state. A release left `failed` by an interrupted converge is upgraded again on the next run; one stuck in a `pending-*` or `uninstalling` state — which helm refuses to upgrade over with `another operation (install/upgrade/rollback) is in progress` — is uninstalled first and installed fresh. Releases are looked up across every helm state, since plain `helm list` hides the `pending-*` ones. Namespaces, the Gateway API manifests, the MetalLB pool and the cert-manager issuers are applied only when missing or drifted. `plan` prints a single `up to date` line for anything that would not change, and for a release that would install or upgrade it shows the helm command plus the merged values with secret-looking keys (`password`, `token`, `secret`, `key`, `credential`) redacted. `check` applies the same drift rules — a release that is missing, not `deployed`, pinned to a different version, or carrying different values fails the entry — and for `latest` entries reports whether an upgrade is available. `destroy` uninstalls the releases in reverse order and removes the resources the plugin applied, including the namespaces it created — those carry an `app.kubernetes.io/managed-by: taloscluster` label, which the plugin writes only when it creates the namespace, never on one that already existed (a pre-existing namespace still gets its Pod Security labels converged, just not the marker) — so a namespace without it (one that pre-existed or was created by something else), one that another entry still uses, and the cluster's own `default`, `kube-system` and `kube-public` are left in place. Entries converge one at a time, so a failing entry — a `version: latest` Gateway API manifest when the GitHub releases API is rate limited or unreachable, say — is warned about and fails the run only after the other entries have converged. The same lookup failure does not stop `check`, which reports an enabled manifest entry it cannot probe as `not_installed` and a disabled one as `absent`, nor `destroy`, which skips the manifests it cannot name with a warning and still removes everything else. Before the first bootstrap there is no kubeconfig, so `plan` reports the charts as deferred instead of failing on it, even when `helm` is not installed yet.

During converge's validate phase, before any core change, the plugin refuses a malformed `charts:` section: a non-mapping section, an unknown entry without `repo` or `manifest`, an entry with both, a key an entry does not use (for example `email` on anything but `cert-manager`), a `version` on a manifest entry, an enabled `nfs` without `storageClasses`, an enabled `ceph` without `clusterID` and `monitors`, or a `cert-manager` with issuers on but no `email`. It also refuses cert-manager's letsencrypt issuers alongside a traefik `values` block that configures its own ACME resolver, since the two clients fight over the HTTP-01 challenge path.

Entries are converged in dependency order: `gateway` before `traefik` (whose Gateway provider needs the CRDs), and `metallb` and its pool before `traefik` claims an address from it. Other entries follow in `cluster.yaml` order.

## cluster.yaml

```yaml
charts:
  gateway:
    enabled: true
    version: latest
  metallb:
    enabled: true
    version: latest
  traefik:
    enabled: true
    version: latest
  cert-manager:
    enabled: true
    email: acme@example.edu
    staging: false
    prod: true
  sealed-secrets:
    enabled: true
  nfs:
    enabled: true
    storageClasses:
      - name: nfs-data
        server: nfs.example.edu
        share: /exports/data
        defaultClass: true
  ceph:
    enabled: false
    clusterID: 2f6a1c0e-0000-4000-8000-000000000000
    monitors: [mon-a.example.edu:6789, mon-b.example.edu:6789]
    rbd:
      pool: kubernetes
      defaultClass: true
    fs:
      fsName: cephfs
  my-chart:
    repo: https://charts.example.com
    namespace: my-namespace
    values: {}
```

### Known entries

Entries the plugin knows by name ship a chart repository, an install namespace with Pod Security Admission labels, and common values. A `values` mapping on the entry is deep-merged over the common values (mappings merge, lists replace).

| Entry | Installs | Namespace | Notes |
| --- | --- | --- | --- |
| `gateway` | Gateway API `standard-install.yaml` from the kubernetes-sigs release | — | A manifest entry; `version` is a release tag (`v1.6.2`) or `latest` |
| `metallb` | The MetalLB chart | `metallb-system` (privileged) | Renders an `IPAddressPool` and `L2Advertisement` from the provider's ingress pool; FRR is disabled |
| `traefik` | The Traefik chart | `traefik` (restricted) | One replica, `LoadBalancer` service pinned to the pool's first address, HTTP redirected to HTTPS; enables the Gateway provider when `gateway` is enabled |
| `cert-manager` | The cert-manager chart with CRDs | `cert-manager` (restricted) | Consumes `email`, `staging` and `prod`; the ingress shim defaults to the `letsencrypt-prod` ClusterIssuer |
| `sealed-secrets` | The Bitnami sealed-secrets controller | `sealed-secrets` (restricted) | Named `sealed-secrets-controller` so `kubeseal` finds it |
| `nfs` | The `csi-driver-nfs` chart | `nfs` (privileged) | Consumes `storageClasses` |
| `ceph` | The `ceph-csi-rbd` and/or `ceph-csi-cephfs` charts | one privileged namespace per chart | Consumes `clusterID`, `monitors`, `rbd`, `fs` (booleans, or mappings that also create a StorageClass) and the optional [`userID`/`userKey` credentials](#secretsyaml) |

The MetalLB pool is never written in `charts`: it comes from the provider, the [`ingress_pool`](network.md#networkexternal) range on Proxmox, and is reused for the traefik service address.

### Generic entry keys

Every entry accepts these; a key an entry does not consume is refused.

#### `charts.<entry>.enabled`

Optional · boolean · default `true`

`false` uninstalls the release or deletes the applied manifests, and removes the resources the plugin created for it, including the namespace it created unless another enabled entry still uses it.

#### `charts.<entry>.version`

Optional · string · default `latest`

A pinned chart version — a leading `v` is ignored when it is compared to the installed chart, so `1.21.2` matches a chart tagged `v1.21.2` — or a release tag for a manifest entry. `latest` upgrades only when a newer chart version exists upstream (read with `helm show chart`, so `plan` never mutates). Manifest entries with explicit URLs do not take a version.

#### `charts.<entry>.repo`

Required for an unknown chart entry · URL

The Helm chart repository, passed as `helm --repo`. An unknown entry must set `repo` or `manifest`; a known chart entry may override its default repository.

#### `charts.<entry>.manifest`

Required for an unknown manifest entry · URL or list of URLs

Manifests applied with `kubectl apply -f`. Mutually exclusive with `repo`. Manifest entries are removed with `kubectl delete -f` on disable or destroy.

#### `charts.<entry>.namespace`

Optional · name or mapping · default the entry's known namespace, else the entry name

Either a namespace name or a mapping `{name, enforce, audit, warn}` whose three optional levels set the `pod-security.kubernetes.io/*` labels on the namespace the plugin creates. A namespace the plugin creates also carries `app.kubernetes.io/managed-by: taloscluster`, the marker disable and destroy look for before removing it; a namespace that pre-existed gets its Pod Security labels converged but never gains the marker, and is never removed. `default`, `kube-system` and `kube-public` are never removed.

#### `charts.<entry>.values`

Optional · mapping · default empty

Overrides deep-merged over the plugin's common values and handed to `helm upgrade --install`.

### cert-manager keys

#### `charts.cert-manager.email`

Required when `staging` or `prod` is on · string

The ACME account address for the letsencrypt ClusterIssuers.

#### `charts.cert-manager.staging` and `charts.cert-manager.prod`

Optional · boolean · default `false` and `false`

Add a `letsencrypt-staging` or `letsencrypt-prod` ClusterIssuer using the HTTP-01 solver through the `traefik` ingress class. The issuers are applied after the chart is installed and deleted before it is uninstalled.

### nfs keys

#### `charts.nfs.storageClasses`

Required when `nfs` is enabled · list of mappings

One StorageClass per export. Each item needs `name`, `server` and `share`; `defaultClass: true` marks at most one class as the cluster default. Optional per class: `subDir` (default `<cluster name>/${pvc.metadata.namespace}-${pvc.metadata.name}-${pv.metadata.name}`), `onDelete` (default `retain`), `reclaimPolicy` (default `Retain`), `volumeBindingMode` (default `Immediate`), `mountOptions` (default `[nfsvers=4.1]`), `annotations` and `parameters`. Set either this or `values.storageClasses`, not both.

### ceph keys

#### `charts.ceph.clusterID` and `charts.ceph.monitors`

Required when `ceph` is enabled · string, list of `host:port`

The Ceph cluster fsid and its monitor addresses, rendered into the `csiConfig` every enabled ceph-csi chart shares.

#### `charts.ceph.rbd` and `charts.ceph.fs`

Optional · boolean or mapping · default `false` and `false`

Install the `ceph-csi-rbd` (block) and `ceph-csi-cephfs` (shared file system) charts. At least one must be on when the entry is enabled; each runs in its own privileged namespace named after the chart. `true` installs the driver alone, so you create StorageClasses yourself. A mapping also has that chart create one StorageClass pointing at the entry's `clusterID`: `rbd` needs `pool`, and `fs` needs `fsName` with an optional `pool`. Both accept `name` (default the chart's own, `csi-rbd-sc` and `csi-cephfs-sc`), `defaultClass: true` on at most one of the two, `reclaimPolicy` (default `Retain`), `mountOptions`, `annotations`, and `parameters`, a mapping of extra `storageClass.*` chart values for that class such as `imageFeatures` or `fuseMountOptions`. Set either a mapping or `values.storageClass`, not both.

### Gathering the values from Ceph

Run these on any Ceph node (or wherever `ceph` has an admin keyring), for example a Proxmox host or a cephadm shell. The fsid is the `clusterID`:

```sh
ceph fsid
```

The monitors are the `mon.*` entries of the monitor map; take the v1 address of each and write it as `host:6789` (the v2 port `3300` also works with current ceph-csi):

```sh
ceph mon dump
# 0: [v2:192.0.2.11:3300/0,v1:192.0.2.11:6789/0] mon.a
# 1: [v2:192.0.2.12:3300/0,v1:192.0.2.12:6789/0] mon.b
```

With `rbd` on, always create and initialise the pool the images will live in before anything else; the user capabilities below and the StorageClass refer to it by name:

```sh
ceph osd pool create kubernetes
rbd pool init kubernetes
```

With `fs` on, the file system must already exist. Its `name` is the `fsName`; the metadata and data pools on the same line belong to CephFS, and `fs.pool` only matters when a file system has several data pools:

```sh
ceph fs ls
# name: kubernetes, metadata pool: cephfs.kubernetes.meta, data pools: [cephfs.kubernetes.data ]
```

Create a dedicated CephX user for the cluster instead of handing out `client.admin`. The plugin delivers a single `userID`/`userKey` pair to both ceph-csi charts, so the user needs the provisioner and node capabilities of every chart it enables (see the [ceph-csi capability list](https://github.com/ceph/ceph-csi/blob/devel/docs/capabilities.md)):

```sh
# rbd only
ceph auth get-or-create client.kubernetes \
  mon 'profile rbd' mgr 'allow rw' osd 'profile rbd pool=kubernetes'

# cephfs only
ceph auth get-or-create client.kubernetes \
  mon 'allow r' mgr 'allow rw' osd 'allow rw tag cephfs *=*' mds 'allow rw'

# rbd and cephfs with one user
ceph auth get-or-create client.kubernetes \
  mon 'allow r, profile rbd' mgr 'allow rw' \
  osd 'allow rw tag cephfs *=*, profile rbd pool=kubernetes' mds 'allow rw'
```

The `userID` is the name without the `client.` prefix (`kubernetes`) and the `userKey` is its key:

```sh
ceph auth get-key client.kubernetes
```

Put the pool and the file system name into the `rbd` and `fs` mappings so each chart creates its StorageClass (the secret names and namespaces default to the ones the plugin delivers):

```yaml
charts:
  ceph:
    enabled: true
    clusterID: 2f6a1c0e-0000-4000-8000-000000000000
    monitors: [192.0.2.11:6789, 192.0.2.12:6789]
    rbd:
      pool: kubernetes        # the pool created above
      defaultClass: true
    fs:
      fsName: cephfs          # a name from `ceph fs ls`
```

`values` is handed to every enabled ceph-csi chart, so it holds what both share (log level, resources); anything per class belongs in the `rbd` or `fs` mapping.

The classes default to `reclaimPolicy: Retain`, so deleting a PVC keeps its image or subvolume and leaves the PV `Released`. Deleting that PV by hand removes only the Kubernetes object; to have ceph-csi delete the data as well, switch the Released PV to `Delete` and the controller reclaims it:

```sh
kubectl patch pv <pv> -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
```

A `Released` PV can instead be reattached by clearing its `claimRef` and creating a PVC that names it in `volumeName`.

Once a Retained PV is deleted by hand, its image stays in the pool with nothing referencing it. `scripts/rbd-review.sh [POOL]`, run on a Ceph node, walks the ceph-csi images in a pool, shows each one's size, usage, claim and timestamps, skips images a node has mapped, and deletes the ones you confirm together with their ceph-csi journal records.

## secrets.yaml

```yaml
charts:
  ceph:
    userID: admin
    userKey: AQC...
```

### `charts.ceph.userID` and `charts.ceph.userKey`

Optional · string, string · set both or neither

CephX credentials the ceph-csi provisioners use. They are ordinary keys of the `ceph` entry: the scaffold puts them in `secrets.yaml` under the same `charts.ceph` path, where they merge with the entry's `clusterID`, `monitors`, `rbd` and `fs` from `cluster.yaml`, but like every credential they may live in `cluster.yaml` or any included file. When present, converge delivers them as the `csi-rbd-secret` and `csi-cephfs-secret` Secrets in the ceph namespaces, only when missing or drifted. Leave them out to manage those Secrets yourself (for example with sealed-secrets); converge then warns that they are absent.

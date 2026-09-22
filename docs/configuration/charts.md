# Charts plugin

Back to the [configuration index](../configuration.md).

The charts plugin installs Helm charts and plain manifests into the cluster itself during converge, so a cluster gets its platform pieces (load balancer, ingress, certificates, storage) without an external GitOps server. It is skipped, and shown as `not configured` by `taloscluster plugin list`, unless the merged configuration carries a `charts` mapping. `taloscluster init` scaffolds one with every known chart present but `enabled: false`; flipping `enabled` is the whole activation story. See [Plugins](../concepts/plugins.md#how-plugins-run) for what converge, plan, destroy, status and check do.

Converge is drift-driven: a release is installed or upgraded only when it is missing, a pinned `version` differs from the installed chart, a `latest` entry has a newer chart version upstream, or the merged values differ from what the release was last installed with. Namespaces, the Gateway API manifests, the MetalLB pool and the cert-manager issuers are applied only when missing or drifted. `plan` prints a single `up to date` line for anything that would not change, and for a release that would install or upgrade it shows the helm command plus the merged values with secret-looking keys (`password`, `token`, `secret`, `key`, `credential`) redacted. `check` reports drift, and for `latest` entries whether an upgrade is available. `destroy` uninstalls the releases in reverse order and removes the resources the plugin applied. Before the first bootstrap there is no kubeconfig, so `plan` reports the charts as deferred instead of failing.

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
    rbd: true
    fs: true
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
| `ceph` | The `ceph-csi-rbd` and/or `ceph-csi-cephfs` charts | one privileged namespace per chart | Consumes `clusterID`, `monitors`, `rbd`, `fs` and the optional [`userID`/`userKey` credentials](#secretsyaml) |

The MetalLB pool is never written in `charts`: it comes from the provider, the [`ingress_pool`](network.md#networkexternal) range on Proxmox, and is reused for the traefik service address.

### Generic entry keys

Every entry accepts these; a key an entry does not consume is refused.

#### `charts.<entry>.enabled`

Optional · boolean · default `true`

`false` uninstalls the release or deletes the applied manifests, and removes the resources the plugin created for it.

#### `charts.<entry>.version`

Optional · string · default `latest`

A pinned chart version, or a release tag for a manifest entry. `latest` upgrades only when a newer chart version exists upstream (read with `helm show chart`, so `plan` never mutates). Manifest entries with explicit URLs do not take a version.

#### `charts.<entry>.repo`

Required for an unknown chart entry · URL

The Helm chart repository, passed as `helm --repo`. An unknown entry must set `repo` or `manifest`; a known chart entry may override its default repository.

#### `charts.<entry>.manifest`

Required for an unknown manifest entry · URL or list of URLs

Manifests applied with `kubectl apply -f`. Mutually exclusive with `repo`. Manifest entries are removed with `kubectl delete -f` on disable or destroy.

#### `charts.<entry>.namespace`

Optional · name or mapping · default the entry's known namespace, else the entry name

Either a namespace name or a mapping `{name, enforce, audit, warn}` whose three optional levels set the `pod-security.kubernetes.io/*` labels on the namespace the plugin creates.

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

Optional · boolean · default `false` and `false`

Install the `ceph-csi-rbd` (block) and `ceph-csi-cephfs` (shared file system) charts. At least one must be on when the entry is enabled; each runs in its own privileged namespace named after the chart.

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

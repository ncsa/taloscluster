# ArgoCD plugin

Back to the [configuration index](../configuration.md).

The argocd plugin registers this cluster with an ArgoCD instance running elsewhere. It renders a cluster Secret (built from this cluster's own `kubeconfig`), an AppProject with `admin` and `user` roles, and, when the repositories are set, an app-of-apps Application whose values carry the per-cluster settings below. It is skipped unless `secrets.yaml` names a way to reach the ArgoCD cluster. During a shared converge it runs after Rancher and adds the Rancher cluster-id annotation when that hook returned an id. Running ArgoCD alone has no preceding Rancher result. See [Plugins](../concepts/plugins.md#argocd) for integration details.

## cluster.yaml

```yaml
argocd:
  admins: [carol@example.com]
  users: [dave@example.com]
  git:
    url: https://git.example.com/kubernetes/cluster.git
  infra:
    url: https://git.example.com/kubernetes/infra.git
  sync: true
  automated: true
  metallb:
    enabled: true
  ingress:
    enabled: true
    class: traefik
    traefik:
      version: "34.0.0"
  certmanager:
    enabled: true
    email: admin@example.edu
  sealedsecrets:
    enabled: true
  cinder:
    enabled: false
  nfs:
    enabled: true
    servers:
      shared:
        server: nfs.example.edu
        path: /exports/mycluster
        defaultClass: true
  monitoring:
    enabled: false
```

### `argocd.admins`

Optional · list of email addresses · default empty

Groups granted the AppProject `admin` role. Merged with `rancher.admins` when a rancher section exists.

### `argocd.users`

Optional · list of email addresses · default empty

Groups granted the AppProject `user` role. Merged with `rancher.users`.

### `argocd.git.url`

Required to render the Application · URL

This cluster's own GitOps repository. When set, the plugin renders a repository Secret, a root Application pointing at this repository's `charts/apps`, and a second `<name>-cluster` Application pointing at `argocd.infra.url`. Both repository URLs must be set together. Omit both the Git URL and Git credentials for registration with only the cluster Secret and AppProject.

### `argocd.infra.url`

Required when `argocd.git.url` is set · URL

The repository whose `charts/apps` chart is the app-of-apps the cluster Application points at. NCSA's is [ncsa/radiant-cluster](https://github.com/ncsa/radiant-cluster/tree/main/charts/apps).

Converge validates the `argocd:` section before any cluster change: setting only one of `git.url` / `infra.url`, specifying git credentials without `git.url`, a non-mapping `argocd:` sub-section, a top-level `argocd:` option the plugin does not understand, an unknown key inside a per-app section, a chart `version` the plugin would silently ignore, or a `url`/`token` apply target without a `kubeconfig`/`context` (see below) is refused in the validate phase while the cluster is still untouched. Validation runs whether or not the plugin is active, so a malformed or unsupported-mode section is reported even though it would otherwise be silently discarded by activation. `taloscluster plan` reports the same rejections before anything is attempted. The documented scalar types are enforced too: `sync`, `automated`, and each per-app `enabled` must be a real YAML boolean (a quoted `"false"` is refused rather than treated as truthy), `admins`/`users` must be lists of email addresses, the repository URLs non-empty strings, and the secrets.yaml apply-target and credential values plain strings.

### `argocd.sync`

Optional · boolean · default `false`

Controls the chart-only sync toggle: pass `sync` into the infrastructure chart’s Helm values. It controls whether the chart applies its own apps when it runs; it does **not** govern the two parent Applications’ automated sync (see `argocd.automated`). The two knobs are independent: you can keep chart-level sync on while turning off parent auto-sync, or vice-versa.

### `argocd.automated`

Optional · boolean · default `true`

Enable automated sync, pruning, and self-healing on the two parent Applications (the root app-of-apps and the `<cluster>-cluster` app). When `false`, those Applications are created without a `syncPolicy.automated` block, so ArgoCD does not continuously apply them and they sync only when triggered manually. This is separate from `argocd.sync`, which only reaches the chart's Helm values.

### Per-app sections

Optional · mapping each

`metallb`, `ingress`, `sealedsecrets`, `certmanager`, `cinder`, `nfs` and `monitoring` each accept `enabled: true` to turn the app on. The plugin forwards `version` for `metallb`, `sealedsecrets`, `certmanager`, and `cinder`; Traefik uses `ingress.traefik.version`. It does not forward `ingress.version`, `nfs.version`, or `monitoring.version` — setting one of those is refused during validation rather than silently ignored; pin those charts under `ingress.traefik.version` instead. When a supported version key is absent, the chart default is kept. Each per-app section is validated: an unknown key inside one (a typo or an unsupported override) is refused, and a forwarded `version` must be a non-empty string. A few apps take extra keys:

- **`ingress.class`**: ingress class name, default `traefik`. Also used as the cert-manager solver class.
- **`ingress.traefik.version`**: pins the Traefik chart.
- **`certmanager.email`**: ACME registration email.
- **`nfs.servers`**: mapping of server name to `server`, `path` and `defaultClass`, copied verbatim into the nfs chart values. Only rendered when `nfs.enabled` is true.

When `metallb.enabled` is true, the plugin renders the provider's load-balancer address pool into `metallb.addresses`. On OpenStack this is the single ingress VIP, emitted as `address/32`; on Proxmox it is the [`ingress_pool`](proxmox.md#ingress_pool) range (`start-end`), emitted verbatim because MetalLB accepts it directly. The ingress controller's `publicIP`/`privateIP` are only populated on OpenStack from the floating IP and VIP; a Proxmox cluster has no single VIP, so those stay empty.

When `cinder.enabled` is true on an OpenStack cluster, the application credential from `secrets.yaml` is not embedded in any ArgoCD Application. The plugin instead writes a Secret named `cinder-csi-cloud-config` into the `cinder-csi` namespace of this cluster itself (using this cluster’s own kubeconfig from `kubeconfig`), carrying the Cinder `cloud.conf` — the `[Global]` auth block with the application credential. The infra chart’s cinder Application must reference that existing Secret through the upstream cinder-csi chart’s `secret.enabled=true, secret.create=false, secret.name=cinder-csi-cloud-config, secret.filename=cloud.conf` instead of building a Secret from values. [`ncsa/radiant-cluster`](https://github.com/ncsa/radiant-cluster/tree/main/charts/apps) needs a one-line change to its `charts/apps/templates/storage/cinder.yaml` (switch `secret.create` from `true` to `false`, and drop the now-unused `secret.data`); with a forked `argocd.infra.url`, make the equivalent change there. Setting `cinder.enabled: true` without `openstack.credential_id` / `openstack.credential_secret` in `secrets.yaml` is a configuration error the plugin reports. Converge ensures the `cinder-csi` Namespace (ArgoCD also syncs it, so the plugin does not `check` or `destroy` it) and applies the Secret; check compares it and destroy removes it, all against this cluster. Converge also deletes the Secret when `cinder.enabled` is turned off, so disabling Cinder clears the credential. The cluster kubeconfig needs create, get, patch, and delete on Secrets in the `cinder-csi` namespace. The cinder-csi service account still needs the chart’s usual RBAC to read its own Secret.

## secrets.yaml

Use `kubeconfig` and `context` for the plugin activation. A `url`/`token` pair alone is not a supported apply target and does not activate the plugin.

```yaml
argocd:
  kubeconfig: ../argocd-kubeconfig
  # context: argocd
  # url: https://argocd.example.edu
  # token: CHANGE-ME
  git:
    username: deploy
    token: CHANGE-ME
```

### `argocd.kubeconfig`

One of · path

Kubeconfig for the cluster running ArgoCD, resolved relative to the taloscluster cluster directory when the path is relative. Manifests are applied with `kubectl --kubeconfig <path>`.

### `argocd.context`

One of · string

A context in your default kubeconfig, passed as `kubectl --context`. May be combined with `kubeconfig`.

### `argocd.url` and `argocd.token`

Not a supported apply target · URL and string

ArgoCD API endpoint and token. The plugin applies manifests via kubectl only, so a `url`/`token` pair without a `kubeconfig` or `context` does not activate the plugin and is refused during validate as an unsupported apply target: set `kubeconfig` or `context` above instead.

### `argocd.git.username` and `argocd.git.token`

Optional · strings

Credentials for `argocd.git.url`, rendered into its ArgoCD repository Secret. Supplying either credential also requires that Git URL. The plugin does not create a separate credential Secret for `argocd.infra.url`; configure access to a private infrastructure repository in ArgoCD separately.

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

### `argocd.sync`

Optional · boolean · default `false`

Pass `sync` into the infrastructure chart’s Helm values. Both generated parent Applications always have automated sync, pruning, and self-healing enabled, including when this value is `false`. This setting does not disable their automated sync policies.

### Per-app sections

Optional · mapping each

`metallb`, `ingress`, `sealedsecrets`, `certmanager`, `cinder`, `nfs` and `monitoring` each accept `enabled: true` to turn the app on. The plugin forwards `version` for `metallb`, `sealedsecrets`, `certmanager`, and `cinder`; Traefik uses `ingress.traefik.version`. It does not forward `ingress.version`, `nfs.version`, or `monitoring.version`. When a supported version key is absent, the chart default is kept. A few apps take extra keys:

- **`ingress.class`**: ingress class name, default `traefik`. Also used as the cert-manager solver class.
- **`ingress.traefik.version`**: pins the Traefik chart.
- **`certmanager.email`**: ACME registration email.
- **`nfs.servers`**: mapping of server name to `server`, `path` and `defaultClass`, copied verbatim into the nfs chart values. Only rendered when `nfs.enabled` is true.

On OpenStack, the plugin obtains the MetalLB VIP, ingress floating IP, and project from the provider. The current Proxmox backend reports `ingress_pool` only as provider status and leaves the ingress endpoint empty, so the plugin does not automatically populate MetalLB addresses from that range. Configure Proxmox load-balancer addresses through your GitOps setup.

OpenStack application credentials from `secrets.yaml` are also embedded in the generated infrastructure Application’s Helm values; anyone able to read that Application can read those credentials.

## secrets.yaml

Use `kubeconfig`, `context`, or both for working plugin operations. URL/token alone makes the plugin appear configured, but all reconcile and reporting hooks reject that mode.

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

Accepted but unsupported for operations · URL and string

ArgoCD API endpoint and token. Accepted by the config, but applying currently requires the kubectl mode above.

### `argocd.git.username` and `argocd.git.token`

Optional · strings

Credentials for `argocd.git.url`, rendered into its ArgoCD repository Secret. Supplying either credential also requires that Git URL. The plugin does not create a separate credential Secret for `argocd.infra.url`; configure access to a private infrastructure repository in ArgoCD separately.

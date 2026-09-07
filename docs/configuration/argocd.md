# ArgoCD plugin

Back to the [configuration index](../configuration.md).

The argocd plugin registers this cluster with an ArgoCD instance running elsewhere. It renders a cluster Secret (built from this cluster's own `kubeconfig`), an AppProject with `admin` and `user` roles, and, when the repositories are set, an app-of-apps Application whose values carry the per-cluster settings below. It is skipped unless `secrets.yaml` names a way to reach the ArgoCD cluster. When the rancher plugin is installed it runs after it and annotates the Secret with the Rancher cluster id. See `plugins/argocd/README.md` for details.

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

This cluster's own GitOps repository. Without it only the Secret and the AppProject are applied.

### `argocd.infra.url`

Required to render the Application · URL

The repository whose `charts/apps` chart is the app-of-apps the cluster Application points at.

### `argocd.sync`

Optional · boolean · default `false`

Enable automated sync on the cluster Application.

### Per-app sections

Optional · mapping each

`metallb`, `ingress`, `sealedsecrets`, `certmanager`, `cinder`, `nfs` and `monitoring` each accept `enabled: true` to turn the app on. A `version` key pins the chart version; when absent the chart default is kept. A few apps take extra keys:

- **`ingress.class`**: ingress class name, default `traefik`. Also used as the cert-manager solver class.
- **`ingress.traefik.version`**: pins the Traefik chart.
- **`certmanager.email`**: ACME registration email.
- **`nfs.servers`**: mapping of server name to `server`, `path` and `defaultClass`, copied verbatim into the nfs chart values. Only rendered when `nfs.enabled` is true.

The MetalLB address, the ingress IP and the OpenStack project are not configured here; taloscluster computes them during the same converge.

## secrets.yaml

Any one of `kubeconfig`, `context`, or `url` plus `token` is enough to activate the plugin.

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

Kubeconfig for the cluster running ArgoCD. Manifests are applied with `kubectl --kubeconfig <path>`.

### `argocd.context`

One of · string

A context in your default kubeconfig, passed as `kubectl --context`. May be combined with `kubeconfig`.

### `argocd.url` and `argocd.token`

One of · URL and string

ArgoCD API endpoint and token. Accepted by the config, but applying currently requires the kubectl mode above.

### `argocd.git.username` and `argocd.git.token`

Optional · strings

Credentials for the GitOps repository, rendered into an ArgoCD repository Secret.

# taloscluster-argocd

A [taloscluster](../../README.md) plugin: register the cluster with ArgoCD by rendering and applying its **cluster Secret** (so ArgoCD can reach this cluster) and its **AppProject** (admin/user roles).

## Install

```bash
uv tool install "taloscluster[argocd] @ git+https://github.com/ncsa/taloscluster"
```

Once installed it runs as part of `taloscluster converge` / `plan` / `destroy` (and reports under `status` / `check`) — there is nothing extra to invoke. It runs after the `rancher` plugin when that one is installed too.

## Configuration

Every key, the validate-phase refusals and the rendered resources are documented in [docs/configuration/argocd.md](../../docs/configuration/argocd.md).

`cluster.yaml` (committed) — `argocd:` holds extra project roles as full emails. These are merged with the `rancher:` members (if a rancher section exists):

```yaml
argocd:
  admins: [carol@example.com]   # merged with rancher.admins -> project 'admin' role
  users:  [dave@example.com]    # merged with rancher.users  -> project 'user' role
  git:
    url: https://git.example.com/kubernetes/cluster.git  # this cluster's GitOps repo
  infra:
    url: https://git.example.com/kubernetes/infra.git    # repo holding the charts/apps chart
  nfs:
    enabled: true
    servers:                      # passed through to the nfs chart values verbatim
      shared:
        server: nfs.example.edu
        path: /exports/mycluster
        defaultClass: true
```

`infra.url` is required to render the `<cluster>-cluster` Application; it points at the repository whose `charts/apps` chart is the app-of-apps. `nfs.servers` is optional and copied verbatim under the nfs chart's `servers:`.

`secrets.yaml` (gitignored) — `argocd:` holds how to reach the ArgoCD cluster to apply changes. The plugin applies via kubectl, so an apply target is a `kubeconfig` path or a `context` (or both); like every credential these — and the Git credentials — may live in `cluster.yaml`, `secrets.yaml` or any included file, so the split here is only the scaffolded default:

```yaml
argocd:
  kubeconfig: ../argocd-kubeconfig   # kubeconfig for the cluster running ArgoCD
  #context: argocd                    # optional: passed as kubectl --context
```

- **`kubeconfig`**: applies with `kubectl --kubeconfig <path> [--context]`.
- **`context`** alone: uses your default kubeconfig (`~/.kube/config`) with `kubectl --context <value>` — no kubeconfig needed.

A `url` + `token` pair alone is refused as an unsupported apply target: the plugin applies manifests via kubectl only and does not speak the ArgoCD API. Give either `kubeconfig` or `context` instead.

## What converge does

1. Reads this cluster's own gitignored `./kubeconfig` (server, CA, client cert/key).
2. Renders:
   - `argocd-<cluster>-secret` — an ArgoCD cluster Secret built from that kubeconfig's server/CA/client-cert, so ArgoCD can authenticate to and manage this cluster.
   - `argocd-<cluster>` — an AppProject with `admin` / `user` roles whose groups are the merged rancher + argocd member emails.
3. Applies the Secret then the Project to the ArgoCD cluster via `kubectl --kubeconfig <argocd kubeconfig> [--context] apply -f -`.

The ingress VIP / floating ip and the OpenStack project embedded in the cluster-apps values come from taloscluster itself, which computed them during the same converge.

## Running it on its own

```
taloscluster plugin argocd [converge|plan|destroy|status|check] [-C DIR]
```

- **converge** — apply the cluster Secret + AppProject. Idempotent (kubectl apply).
- **plan** — dry-run converge: render the manifests and show the apply actions.
- **destroy** — delete the AppProject then the cluster Secret via kubectl. Runs before the OpenStack teardown, while the cluster is still reachable.
- **status** — which of the rendered resources are present on the ArgoCD cluster.
- **check** — not ok while any resource is missing or differs from its rendered manifest, i.e. converge would apply something.

## Not configured

If the merged configuration has no `argocd:` apply target (no kubeconfig, and no context), the plugin is skipped entirely, and `taloscluster plugin list` shows it as `not configured`.

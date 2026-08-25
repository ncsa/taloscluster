# taloscluster-argocd

A [taloscluster](../../README.md) plugin: register the cluster with ArgoCD by
rendering and applying its **cluster Secret** (so ArgoCD can reach this cluster)
and its **AppProject** (admin/user roles).

## Install

```bash
uv tool install "taloscluster[argocd] @ git+https://github.com/ncsa/taloscluster"
```

Once installed it runs as part of `taloscluster converge` / `plan` / `destroy`
(and reports under `status` / `check`) — there is nothing extra to invoke. It
runs after the `rancher` plugin when that one is installed too.

## Configuration

`cluster.yaml` (committed) — `argocd:` holds extra project roles as full emails.
These are merged with the `rancher:` members (if a rancher section exists):

```yaml
argocd:
  admins: [carol@example.com]   # merged with rancher.admins -> project 'admin' role
  users:  [dave@example.com]    # merged with rancher.users  -> project 'user' role
```

`secrets.yaml` (gitignored) — `argocd:` holds how to reach the ArgoCD cluster to
apply changes. Any one of these is sufficient:

```yaml
argocd:
  kubeconfig: ../argocd-kubeconfig   # kubeconfig for the cluster running ArgoCD
  #context: argocd                    # optional: passed as kubectl --context
```

- **`context`** alone: uses your default kubeconfig (`~/.kube/config`) with
  `kubectl --context <value>` — no kubeconfig/url/token needed.
- **`kubeconfig`**: applies with `kubectl --kubeconfig <path> [--context]`.
- **`url` + `token`**: accepted by config, but apply currently requires the
  kubectl mode (kubeconfig or context).

## What converge does

1. Reads this cluster's own gitignored `./kubeconfig` (server, CA, client cert/key).
2. Renders:
   - `argocd-<cluster>-secret` — an ArgoCD cluster Secret built from that
     kubeconfig's server/CA/client-cert, so ArgoCD can authenticate to and manage
     this cluster.
   - `argocd-<cluster>` — an AppProject with `admin` / `user` roles whose groups
     are the merged rancher + argocd member emails.
3. Applies the Secret then the Project to the ArgoCD cluster via
   `kubectl --kubeconfig <argocd kubeconfig> [--context] apply -f -`.

The ingress VIP / floating ip and the OpenStack project embedded in the
cluster-apps values come from taloscluster itself, which computed them during the
same converge.

## Running it on its own

```
taloscluster plugin argocd [converge|plan|destroy|status|check] [-C DIR]
```

- **converge** — apply the cluster Secret + AppProject. Idempotent (kubectl apply).
- **plan** — dry-run converge: render the manifests and show the apply actions.
- **destroy** — delete the AppProject then the cluster Secret via kubectl. Runs
  before the OpenStack teardown, while the cluster is still reachable.
- **status** — which of the rendered resources are present on the ArgoCD cluster.
- **check** — not ok while any resource is missing or differs from its rendered
  manifest, i.e. converge would apply something.

## Not configured

If `secrets.yaml` has no `argocd:` apply target (no kubeconfig, and no url+token),
the plugin is skipped entirely, and `taloscluster plugin list` shows it as
`not configured`.

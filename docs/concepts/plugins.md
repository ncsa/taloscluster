# Plugins

Everything past the cluster itself lives in plugins. taloscluster builds the machines, bootstraps Kubernetes and writes the kubeconfig; registering that cluster somewhere, or installing things into it, is a plugin's job. Plugins are separate Python packages that live in the `plugins/` folder of the repository and are installed only when you want them:

```bash
uv tool install "taloscluster[argocd,rancher] @ git+https://github.com/ncsa/taloscluster"
uv tool install "taloscluster[all] @ git+https://github.com/ncsa/taloscluster"
```

Two plugins ship today:

| Plugin | What it does | Configuration |
| --- | --- | --- |
| `rancher` | Imports the cluster into a Rancher server, installs the cluster agent, and keeps the owner and member lists in sync | [rancher](../configuration/rancher.md) |
| `argocd` | Registers the cluster with an ArgoCD instance and hands it an app-of-apps that installs the platform applications | [argocd](../configuration/argocd.md) |

## How plugins run

A plugin hooks into the normal commands; there is nothing extra to invoke.

- **`converge`** runs plugins last, after the cluster is healthy and the kubeconfig is written.
- **`plan`** runs them in dry-run mode and prints what they would do.
- **`destroy`** runs them first, in reverse order, while the cluster is still reachable, so a plugin can deregister the cluster before it disappears.
- **`status`** and **`check`** include a section per plugin. A plugin whose check says converge would change something flips the exit code to 1, like an available upgrade does.

A plugin is inert until it is configured. It needs its own section in both `cluster.yaml` and `secrets.yaml`; `taloscluster init` adds both, commented out. `taloscluster plugin list` shows what is installed, in run order, and whether each is configured. A single plugin can be run on its own, for example `taloscluster plugin rancher converge` to redo a registration without touching the cluster.

A plugin that fails does not undo the converge that built the cluster. It is reported, the other plugins still run, and the command exits non-zero.

## Plugins depend on each other

Plugins run in a defined order and can pass information forward. A plugin declares the names it wants to run after, and whatever its converge returns is stored for the plugins that follow. The rancher plugin publishes the Rancher cluster id it created; the argocd plugin runs after it and stamps that id on the ArgoCD cluster Secret so the two systems point at the same cluster. The dependency is soft: a name that is not installed is ignored, and a plugin must treat an earlier plugin's output as optional.

## Rancher

The rancher plugin takes usernames from [`rancher.admins` and `rancher.users`](../configuration/rancher.md) and reconciles them to Rancher's `cluster-owner` and `cluster-member` roles, adding missing bindings and removing bindings for anyone no longer listed. Taking a name out of the list revokes that person's access on the next converge.

## ArgoCD

The argocd plugin is highly specialised. It does more than register the cluster: it renders an ArgoCD cluster Secret from this cluster's kubeconfig, an AppProject whose roles come from [`argocd.admins` and `argocd.users`](../configuration/argocd.md), and an app-of-apps Application whose values carry the per-cluster settings from `cluster.yaml`. That Application points at the Helm chart in [ncsa/radiant-cluster `charts/apps`](https://github.com/ncsa/radiant-cluster/tree/main/charts/apps), which installs and configures the platform applications (MetalLB, the ingress controller, cert-manager, sealed-secrets, storage, monitoring) according to the per-app sections you enable. The repository holding that chart is set with [`argocd.infra.url`](../configuration/argocd.md#argocdinfraurl). Unless you run that chart or one with the same values layout, expect to fork the plugin rather than reuse it as is.

## Writing a plugin

A new plugin is a new folder under `plugins/` with a `pyproject.toml` that declares an entry point in the `taloscluster.plugins` group. No change to taloscluster itself is needed; whatever is installed is what runs. The module implements as much of the protocol as it has: `configured` and `converge` are required, `init`, `destroy`, `status` and `check` are optional, and an `AFTER` tuple names the plugins to run after. Each hook receives a context with the parsed configuration, the kubeconfig and talosconfig paths, the provider details and the addresses converge already computed, so a plugin never re-derives them. The [README](https://github.com/ncsa/taloscluster#writing-a-plugin) has the full protocol and the output conventions that make `plan` work for free.

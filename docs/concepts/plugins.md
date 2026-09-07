# Plugins

Optional integrations live in plugins. taloscluster builds the machines, bootstraps Kubernetes and writes the kubeconfig; plugins can register the cluster with other services and install platform applications. Core taloscluster also installs metrics-server and the kubelet serving certificate approver through Talos bootstrap manifests. Plugins are separate Python packages that live in the `plugins/` folder of the repository and are installed only when you want them:

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

A plugin is inert until its own activation check succeeds. Rancher requires a `rancher` mapping in `cluster.yaml` and URL/token keys in `secrets.yaml`. ArgoCD activates from an apply target in `secrets.yaml`, even without an `argocd` section in `cluster.yaml`; its hooks currently require a kubeconfig or context. `taloscluster init` adds starter sections with connection credentials commented out, leaving the bundled plugins inactive. `taloscluster plugin list` shows what is installed, in run order, and whether each is configured. A single plugin can be run on its own, for example `taloscluster plugin rancher converge` to redo a registration without touching the cluster.

A converge or destroy hook failure is reported, the other plugins still run, and the command exits nonzero. Core destroy continues infrastructure teardown after plugin failures. A failing check contributes `ok: false`; status errors appear in the report without changing the exit status. Plugin load, activation, and init failures are warnings and do not necessarily fail the command.

## Plugins depend on each other

Plugins run in a defined order and can pass information forward. A plugin declares the names it wants to run after, and whatever its converge returns is stored for the plugins that follow. The rancher plugin publishes the Rancher cluster id it created; the argocd plugin runs after it and stamps that id on the ArgoCD cluster Secret so the two systems point at the same cluster. The dependency is soft: a name that is not installed is ignored, and a plugin must treat an earlier plugin's output as optional.

## Rancher

The rancher plugin takes usernames from [`rancher.admins` and `rancher.users`](../configuration/rancher.md) and reconciles them to Rancher's `cluster-owner` and `cluster-member` roles, adding missing bindings and removing bindings for anyone no longer listed. Stale individual user bindings are removed, while the creator-owner binding and group bindings are preserved. Access inherited through those retained bindings may remain. Destroy deletes the Rancher cluster registration and removes the downstream `cattle-system` namespace.

## ArgoCD

The argocd plugin is highly specialised. It does more than register the cluster: it renders an ArgoCD cluster Secret from this cluster's kubeconfig, an AppProject whose roles come from [`argocd.admins` and `argocd.users`](../configuration/argocd.md), and an app-of-apps Application whose values carry the per-cluster settings from `cluster.yaml`. That Application points at the Helm chart in [ncsa/radiant-cluster `charts/apps`](https://github.com/ncsa/radiant-cluster/tree/main/charts/apps), which installs and configures the platform applications (MetalLB, the ingress controller, cert-manager, sealed-secrets, storage, monitoring) according to the per-app sections you enable. The repository holding that chart is set with [`argocd.infra.url`](../configuration/argocd.md#argocdinfraurl). Unless you run that chart or one with the same values layout, expect to fork the plugin rather than reuse it as is.

## Writing a plugin

Add a folder under `plugins/` with its own `pyproject.toml` declaring an entry point — that is the whole registration, no core change:

```toml
[project.entry-points."taloscluster.plugins"]
myplugin = "taloscluster_myplugin"
```

The named module implements as much of the protocol as it has. Only `configured` and `converge` are required. An optional `init(root)` hook can scaffold missing configuration sections when `taloscluster init` runs:

```python
AFTER: tuple[str, ...] = ("rancher",)     # run after these, if they are installed

def configured(ctx) -> bool: ...          # is this plugin set up for this cluster?
def converge(ctx, assume_yes=False) -> dict | None: ...
def destroy(ctx, assume_yes=False) -> None: ...
def status(ctx) -> dict: ...              # rendered by core, text or yaml
def check(ctx) -> dict: ...               # must carry "ok": bool
```

`ctx` is a `Context` carrying what taloscluster already knows, so a plugin never re-derives it: `ctx.root`, `ctx.cfg` (the parsed `cluster.yaml`), `ctx.kubeconfig` / `ctx.talosconfig`, and `ctx.infrastructure` / `ctx.openstack` / `ctx.kubernetes` / `ctx.ingress` (url, region, project; floating ips and VIPs). During a converge these are already in hand, so reading them costs nothing.

Whatever `converge` returns is stored in `ctx.results[<name>]` before the next plugin runs — that is how `argocd` picks up the Rancher cluster id. `AFTER` is a wish, not a dependency: a name that is not installed is ignored, and a plugin must treat an earlier plugin's output as optional.

Print through `taloscluster.output` (`log` / `info` / `action`) and honour `dry_run()`, and `plan` works for free. Report data, never text — core renders `status` / `check` dicts for both text and yaml.

Optional hooks are skipped during core command fan-out. An explicitly requested missing hook is an error. Hook reporting and exit behavior follow the rules above.

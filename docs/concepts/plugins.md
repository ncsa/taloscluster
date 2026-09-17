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
- **`plan`** runs them in dry-run mode and prints what they would do. Before the first bootstrap there is nothing to register yet: a plugin whose work needs the cluster's own kubeconfig or an allocated endpoint (for example argocd building its cluster Secret) reports the registration as deferred until `converge` bootstraps the cluster, so `plan` stays usable and successful.
- **`destroy`** runs them first, in reverse order, while the cluster is still reachable, so a plugin can deregister the cluster before it disappears.
- **`status`** and **`check`** include a section per plugin. A plugin whose check says converge would change something flips the exit code to 1, like an available upgrade does.

Before any cluster change, converge asks each plugin that declares a `validate` hook to validate its own configuration, whether or not it is active for this cluster. The hook runs during converge's validate phase, ahead of the image, network, machine and compute phases, so a malformed or contradictory plugin section (a repository URL without its pair, credentials without their URL, a non-mapping section, an unsupported option, an unsupported connection mode) stops the run while the cluster is still untouched instead of surfacing as a late failure once everything is already built. `plan` runs the same phase, so a bad plugin config is reported before anything is attempted. Because a plugin's own activation check can silently discard a supplied-but-invalid section (a non-mapping section, or a `url`/`token` apply target the plugin cannot use), the `validate` hook is consulted regardless of activation and distinguishes an absent section — which it treats as a no-op — from invalid supplied configuration, which it rejects.

A plugin is inert until its own activation check succeeds. Rancher requires a `rancher` mapping in `cluster.yaml` and URL/token keys in `secrets.yaml`. ArgoCD activates only from a kubectl apply target (a `kubeconfig` or `context` in `secrets.yaml`), even without an `argocd` section in `cluster.yaml`; a `url`/`token` pair alone does not activate it because the plugin applies manifests via kubectl only. `taloscluster init` adds starter sections with connection credentials commented out, leaving the bundled plugins inactive. `taloscluster plugin list` shows what is installed, in run order, and whether each is configured. A single plugin can be run on its own, for example `taloscluster plugin rancher converge` to redo a registration without touching the cluster. A standalone `taloscluster plugin NAME converge`, `plan` or `destroy` runs that plugin's `validate` hook before any mutation, so the same supplied-but-invalid-sections a converge would refuse up front (an unsupported override, a lone repository URL, an unsupported apply target) are refused here too, instead of being silently ignored or applied to incomplete resources. A direct `taloscluster plugin NAME destroy` removes what that plugin manages and asks for the cluster name to confirm before it runs, matching the top-level `destroy`; pass `--yes` to skip the prompt, or `--dry-run` to preview first.

A converge or destroy hook failure is reported, the other plugins still run, and the command exits nonzero. Core destroy continues infrastructure teardown after plugin failures. A failing check contributes `ok: false`; status errors appear in the report without changing the exit status. Plugin load, activation, and init failures are warnings and do not necessarily fail the command. Even a `Die` raised by plugin code — the same fatal-abort signal core itself uses — is contained as that one plugin's failure rather than unwinding the whole run, so one plugin dying cannot take the rest of the command down with it. Two installed plugins that advertise the same entry-point `name` are ambiguous: the first wins and a warning is printed, because taloscluster keys plugins by name.

## Plugins depend on each other

Plugins run in a defined order and can pass information forward: a plugin declares the names it wants to run after, and whatever it returns — a converge result or a `check`/`status` report — is stored for the plugins that follow, where an earlier result is read from `ctx.results` (see [Writing a plugin](#writing-a-plugin)). The rancher plugin publishes the Rancher cluster id it resolved; the argocd plugin runs after it and stamps that id on the ArgoCD cluster Secret so the two systems point at the same cluster. The dependency is soft: a name that is not installed is ignored, and a plugin must treat an earlier plugin's output as optional.

## Rancher

The rancher plugin takes usernames from [`rancher.admins` and `rancher.users`](../configuration/rancher.md) and reconciles them to Rancher's `cluster-owner` and `cluster-member` roles, adding missing bindings and removing bindings for anyone no longer listed. Each username must be listed under exactly one tier; a name under both, `admins`/`users` that are not lists of usernames, or `url`/`token` values that are not non-empty strings are refused during converge's validate phase — ahead of any core change — while a username is resolved to a Rancher principal only on an exact id match, so a short or misspelled netid is refused by converge and check instead of granting `cluster-owner` to whoever a prefix search returns first or silently removing that user's existing binding as stale while unresolved. Two netids from different tiers that resolve to the same principal (for example `alice` and `alice@example.com`, whose email suffix is stripped during resolution) are likewise refused by converge and check before any binding changes, since they would otherwise flap between the two roles on alternating runs. Stale individual user bindings are removed, while the creator-owner binding and group bindings are preserved. Access inherited through those retained bindings may remain. Both converge and destroy refuse to act on a Rancher cluster whose id does not match the downstream cluster's `cattle-cluster-agent`, so an unrelated cluster sharing the name is never attached to or deleted; converge also refuses when that agent is registered but no Rancher cluster bears the configured name at all — a registration renamed or deleted in the Rancher UI — rather than create a fresh import cluster that could never match the agent's existing id; the agent is orphaned then, and `check`/`status` report `downstream_id` with an `orphan_reason` instead of a bare `registered: false`. `check` and `status` enforce the same identity match — a downstream agent id that differs from the Rancher cluster id reads as `id_match: false` (with `downstream_id` and `id_mismatch_reason` reported) and fails `check` even when the memberships otherwise match. Destroy deletes the Rancher cluster registration and removes the downstream `cattle-system` namespace, and when no Rancher cluster bears the name but the agent is registered it recognizes the orphaned agent and uninstalls it from the downstream cluster instead of reporting nothing to remove. See [Troubleshooting](../troubleshooting.md#the-downstream-agent-is-orphaned-matches-no-rancher-cluster).

## ArgoCD

The argocd plugin is highly specialised. It does more than register the cluster: it renders an ArgoCD cluster Secret from this cluster's kubeconfig, an AppProject whose roles come from [`argocd.admins` and `argocd.users`](../configuration/argocd.md), and an app-of-apps Application whose values carry the per-cluster settings from `cluster.yaml`. That Application points at the Helm chart in [ncsa/radiant-cluster `charts/apps`](https://github.com/ncsa/radiant-cluster/tree/main/charts/apps), which installs and configures the platform applications (MetalLB, the ingress controller, cert-manager, sealed-secrets, storage, monitoring) according to the per-app sections you enable. The repository holding that chart is set with [`argocd.infra.url`](../configuration/argocd.md#argocdinfraurl). Unless you run that chart or one with the same values layout, expect to fork the plugin rather than reuse it as is.

Provider credentials are never placed in an ArgoCD Application. When Cinder is enabled, the plugin delivers the OpenStack cloud.conf as a Secret to the downstream cluster itself (the `cinder-csi` namespace, via this cluster's own kubeconfig) and expects the infra chart to consume it with the upstream cinder-csi chart's `secret.create=false` reference — see [ArgoCD configuration](../configuration/argocd.md#per-app-sections) for the required chart change and permissions. The delivered Secret is created by converge, reported by status, compared by check, and removed by destroy, all against the downstream cluster.

## Writing a plugin

Add a folder under `plugins/` with its own `pyproject.toml` declaring an entry point — that is the whole registration, no core change:

```toml
[project.entry-points."taloscluster.plugins"]
myplugin = "taloscluster_myplugin"
```

The named module implements as much of the protocol as it has. Only `configured` and `converge` are required. An optional `init(root)` hook can scaffold missing configuration sections when `taloscluster init` runs; an optional `validate(root, ctx)` hook is run during converge's validate phase, before any cluster mutation, to reject a malformed or contradictory `cluster.yaml` / `secrets.yaml` section:

```python
AFTER: tuple[str, ...] = ("rancher",)     # run after these, if they are installed
CONFIG_SECTIONS: tuple[str, ...] = ("myplugin",)  # top-level config keys this plugin owns

def init(root) -> None: ...               # scaffold missing config sections
def validate(root, ctx) -> None: ...      # raise ConfigError on bad config
def configured(ctx) -> bool: ...          # is this plugin set up for this cluster?
def converge(ctx, assume_yes=False) -> dict | None: ...
def destroy(ctx, assume_yes=False) -> None: ...
def status(ctx) -> dict: ...              # rendered by core, text or yaml
def check(ctx) -> dict: ...               # must carry "ok": bool
```

`CONFIG_SECTIONS` lists the top-level `cluster.yaml` / `secrets.yaml` keys the plugin owns. Core's loader retains those keys as valid even though it does not parse them, so an installed plugin's section is not mistaken for a misspelled or unsupported top-level key; a plugin that omits it contributes nothing, and the plugin is free to validate the keys inside its own section in `validate`.

`ctx` is a `Context` carrying what taloscluster already knows, so a plugin never re-derives it: `ctx.root`, `ctx.cfg` (the parsed `cluster.yaml`), `ctx.kubeconfig` / `ctx.talosconfig`, and `ctx.infrastructure` / `ctx.openstack` / `ctx.kubernetes` / `ctx.ingress` (url, region, project; floating ips and VIPs). During a converge these are already in hand, so reading them costs nothing.

Whatever a plugin's `converge` — or its `check`/`status` report — returns is stored in `ctx.results[<name>]` before the next plugin runs: `run` stores a converge result and `collect` stores each report dict. That is how `argocd` picks up the Rancher cluster id during both a converge and a subsequent check/status, so it renders the same downstream identity instead of an empty value. When rancher's `check` reports an identity mismatch, the id it publishes is the downstream agent's own id — or none when there is no agent — not the id of the unrelated Rancher cluster bearing the name, so argocd stamps the identity a converge would attach to instead of drifting against the foreign cluster. A standalone `taloscluster plugin argocd converge`/`check` runs without rancher (so `ctx.results` starts empty); the argocd plugin then reads the same cluster id off the downstream cluster's own `cattle-cluster-agent` and stamps it, so a standalone run still points ArgoCD at the same Rancher cluster. `AFTER` is a wish, not a dependency: a name that is not installed is ignored, and a plugin must treat an earlier plugin's output as optional.

Print through `taloscluster.output` (`log` / `info` / `action`) and honour `dry_run()`, and `plan` works for free. Report data, never text — core renders `status` / `check` dicts for both text and yaml.

Optional hooks are skipped during core command fan-out. An explicitly requested missing hook is an error. Hook reporting and exit behavior follow the rules above.

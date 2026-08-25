# taloscluster

`taloscluster` provisions and manages [Talos Linux](https://www.talos.dev/)
Kubernetes clusters on OpenStack from a single declarative file. You describe
the cluster you want in `cluster.yaml`; `taloscluster converge` makes reality
match it — create, scale up, scale down (drain first), and rolling
Talos/Kubernetes upgrades are all the same command. Re-running is always safe.

There is no state file. Every resource is named deterministically and tagged
(`managed-by=taloscluster`, `cluster=<name>`); converge discovers what exists by
tag, creates what's missing, and never touches resources it didn't create.

## Install

Requires `talosctl` and `kubectl` on PATH. With
[uv](https://docs.astral.sh/uv/) nothing else is needed:

```bash
uv tool install git+https://github.com/ncsa/taloscluster   # install
uv tool upgrade taloscluster                                # update
```

Add the optional [plugins](#plugins) with an extra — `[argocd]`, `[rancher]` or
`[all]`:

```bash
uv tool install "taloscluster[all] @ git+https://github.com/ncsa/taloscluster"
```

Or run it without installing:

```bash
uvx --from git+https://github.com/ncsa/taloscluster taloscluster --help
```

From a checkout of this repo: `uv run taloscluster --help`, or with pip:
`python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

To hack on it, install it editable so the `taloscluster` command runs your
working tree (edits take effect immediately, no reinstall):

```bash
git clone https://github.com/ncsa/taloscluster && cd taloscluster
uv tool install --editable ".[all]"   # editable install on PATH, plugins included
uv sync --extra dev && uv run pytest  # dev deps + tests
```

The `[all]` extra matters: without it you get the core tool only and
`taloscluster plugin list` comes back empty. The plugins in `plugins/*` are
workspace members, so they are linked editable too — an edit under
`plugins/argocd/` is live in the installed command with no reinstall. Re-run the
install after adding a *new* plugin folder, or after changing an entry point.

Add `--force` to replace an existing install, and `uv tool uninstall
taloscluster` to remove it again (that drops the plugins with it — they live in
the same tool environment).

## Quick start

```bash
taloscluster init mycluster   # scaffold cluster.yaml, secrets.yaml, .gitignore
vi secrets.yaml             # openstack application credential + tailscale key
vi cluster.yaml             # versions, pools, openstack endpoint, allowlists
taloscluster plan             # dry-run: print every action, change nothing
taloscluster converge         # build the cluster
```

The machine running taloscluster must be on the tailnet — the initial bootstrap
reaches the first controlplane node by its tailscale name.

## Commands

| command | what it does |
| --- | --- |
| `init [NAME]` | scaffold `cluster.yaml` / `secrets.yaml` / `.gitignore`; never overwrites existing files |
| `plan` | dry-run converge: print every create/update/delete |
| `converge` | make the cluster match cluster.yaml (phases: image → secrets → network/SG → discover → scale-down → upgrade → compute → bootstrap → kubeconfig → health) |
| `status` | show the OpenStack url/region/project, managed resources, kube-api/ingress floating ips + `kubectl get nodes` (`-o yaml` for machine-readable output) |
| `check` | compare `talos.version` / `kubernetes.version` against the newest upstream releases and against what the nodes run; exits 1 if an update, a drifted node or a leftover cordon was found (`-o yaml` for machine-readable output) |
| `dashboard [NODE...]` | `talosctl dashboard` on all (reachable) nodes, or just the ones given |
| `env` | print `export OS_*` lines for the openstack CLI: `eval "$(taloscluster env)"` |
| `image download\|remove` | build/upload the Glance boot image, or delete it (converge never deletes it) |
| `destroy` | delete every managed resource + local state; the shared boot image is kept |
| `plugin list` / `plugin NAME [ACTION]` | show the installed plugins, or run one on its own (see [Plugins](#plugins)) |

Options: every command takes `-C DIR` to operate on another cluster directory.
`converge`, `image` and `destroy` take `--dry-run` (print, don't do) and
`--yes` (skip the confirmation that any deletion otherwise requires).

Typical `cluster.yaml` edits and what converge does with them: bump a pool
`count` to add nodes; lower it to drain + remove them; bump `talos.version`
or `kubernetes.version` for a rolling upgrade (existing nodes are upgraded
*before* new ones are added — `taloscluster check` tells you which bumps are
available); edit a `security` allowlist to reconcile the security-group rules.

## Plugins

Everything past the cluster itself — registering it with Rancher, handing it to
ArgoCD — lives in optional plugins. They are separate packages, installed only
if you want them:

```bash
uv tool install "taloscluster[argocd,rancher] @ git+https://github.com/ncsa/taloscluster"
uv tool install "taloscluster[all]            @ git+https://github.com/ncsa/taloscluster"
```

From a checkout, `uv tool install --editable ".[all]"` links them to your working
tree (see [Install](#install)).

Once installed, a plugin hooks into the normal commands — there is nothing extra
to remember:

| command | what plugins do |
| --- | --- |
| `converge` | run last, after the cluster is healthy and the kubeconfig is written |
| `plan` | the same, dry-run: every plugin action is printed, nothing is applied |
| `destroy` | run **first**, in reverse order, while the cluster is still reachable |
| `status` | each plugin adds a section (also under `plugins:` in `-o yaml`) |
| `check` | each plugin reports whether converge would change anything; a plugin that says no flips the exit code to 1 |

A plugin is inert until you configure it: it needs its own section in
`cluster.yaml` **and** `secrets.yaml`. `taloscluster init` scaffolds both,
commented out.

```bash
taloscluster plugin list                    # installed, in run order, configured or not
taloscluster plugin argocd plan             # dry-run just this one
taloscluster plugin rancher converge        # re-run one registration, no cluster converge
taloscluster plugin argocd check -o yaml
```

| plugin | what it does |
| --- | --- |
| `rancher` | imports the cluster into Rancher, installs the cluster agent, and reconciles members from `rancher.admins` / `rancher.users` (netids → cluster-owner / cluster-member) |
| `argocd` | applies the cluster Secret, AppProject, repo Secret and app-of-apps Applications to an ArgoCD cluster, from `argocd.admins` / `argocd.users` (emails) |

### Writing a plugin

Add a folder under `plugins/` with its own `pyproject.toml` declaring an entry
point — that is the whole registration, no core change:

```toml
[project.entry-points."taloscluster.plugins"]
myplugin = "taloscluster_myplugin"
```

The named module implements as much of the protocol as it has. Only `configured`
and `converge` are required:

```python
AFTER: tuple[str, ...] = ("rancher",)     # run after these, if they are installed

def configured(ctx) -> bool: ...          # is this plugin set up for this cluster?
def converge(ctx, assume_yes=False) -> dict | None
def destroy(ctx, assume_yes=False) -> None
def status(ctx) -> dict                   # rendered by core, text or yaml
def check(ctx) -> dict                    # must carry "ok": bool
```

`ctx` is a `Context` carrying what taloscluster already knows, so a plugin never
re-derives it: `ctx.root`, `ctx.cfg` (the parsed `cluster.yaml`), `ctx.kubeconfig`
/ `ctx.talosconfig`, and `ctx.openstack` / `ctx.kubernetes` / `ctx.ingress`
(url, region, project; floating ips and VIPs). During a converge these are
already in hand, so reading them costs nothing.

Whatever `converge` returns is stored in `ctx.results[<name>]` before the next
plugin runs — that is how `argocd` picks up the Rancher cluster id. `AFTER` is a
wish, not a dependency: a name that is not installed is ignored, and a plugin
must treat an earlier plugin's output as optional.

Print through `taloscluster.output` (`log` / `info` / `action`) and honour
`dry_run()`, and `plan` works for free. Report data, never text — core renders
`status` / `check` dicts for both text and yaml.

A plugin that raises is contained: it is reported and the others still run, but
the command exits non-zero.

## The three files

1. **`cluster.yaml`** — desired state: versions, node pools, network,
   allowlists, extensions. Committable, but note the `security` allowlists
   reveal which source addresses may reach your APIs.
2. **`secrets.yaml`** — OpenStack application credential + tailscale pre-auth
   key. Gitignored; restorable by reissuing credentials.
3. **`talossecrets.yaml`** — ⚠️ the cluster's cryptographic identity (cluster
   CA, etcd CA, join tokens). Generated on the first converge, gitignored,
   mode 0600. **It cannot be regenerated for a running cluster** — back it up
   out-of-band like a private CA key. `destroy` deletes it on purpose, so the
   next converge starts a brand-new cluster.

(`talosconfig` and `kubeconfig` are derived from these and regenerated by
converge; safe to delete.)

## Versions & upgrades

`cluster.yaml` pins both versions; nothing auto-upgrades. Find targets with:

```bash
curl -s https://api.github.com/repos/siderolabs/talos/releases/latest | jq -r .tag_name
talosctl gen config --help | grep kubernetes-version   # k8s pairing Talos tested
```

Two rules: upgrade Kubernetes **one minor at a time**, and **bump Talos before
Kubernetes** when moving both (converge already orders it that way within a
run). Bumping `talos.version` builds a new ~1 GB boot image via
[factory.talos.dev](https://factory.talos.dev/) on the next converge.

## Extensions

Every node boots from one shared image per Talos version
(`talos-<version>-tailscale`, baked with tailscale + qemu-guest-agent).
Tailscale only authenticates if a key is present in `secrets.yaml`. Extra
extensions (e.g. nvidia for a GPU pool) and freeform machine-config patches go
in `cluster.yaml`, cluster-wide under `talos:` or per worker pool:

```yaml
workers:
  gpu:
    count: 2
    flavor: gpu.a100
    disk: 100
    extensions:
      - siderolabs/nonfree-kmod-nvidia
      - siderolabs/nvidia-container-toolkit
    config_patches:
      - |
        machine:
          sysctls: {...}
```

Extra extensions take effect on the node's first upgrade pass (on OpenStack
the boot image is the first-boot system; the factory installer image applies
on `talosctl upgrade`), which converge handles automatically.

## Tags (node labels)

Every node carries kubernetes node labels via talos (`machine.nodeLabels`):
`ncsa/role`, `ncsa/pool`, and `ncsa/project` — the OpenStack project name the
application credential is scoped to (spaces replaced by `_`). Extra tags go in
`cluster.yaml`, cluster-wide at the top level or per pool (pool wins, and a
user tag may override a default):

```yaml
tags:
  team: platform

workers:
  gpu:
    tags:
      workload: gpu
```

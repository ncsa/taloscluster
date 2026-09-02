# taloscluster

`taloscluster` provisions and manages [Talos Linux](https://www.talos.dev/)
Kubernetes clusters on OpenStack or Proxmox from a single declarative file. You describe
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
taloscluster init --openstack mycluster  # or: --proxmox
vi secrets.yaml             # provider credential + tailscale key
vi cluster.yaml             # versions, pools, provider, network, allowlists
taloscluster plan             # dry-run: print every action, change nothing
taloscluster converge         # build the cluster
```

The machine running taloscluster must be on the tailnet — the initial bootstrap
reaches the first controlplane node by its tailscale name.

### Headscale hygiene

Before recreating a destroyed cluster with the same name, remove its stale nodes
from Headscale so tailnet DNS cannot resolve the reused hostnames to old machines.
Also replace the `tailscale.auth_key` in `secrets.yaml` when it is ephemeral,
single-use, or expired.

## Commands

| command | what it does |
| --- | --- |
| `init [--openstack\|--proxmox] [NAME]` | scaffold provider-specific `cluster.yaml` / `secrets.yaml` plus `.gitignore`; defaults to OpenStack; installed plugins append only missing sections |
| `plan` | dry-run converge: print every create/update/delete |
| `converge` (`sync`, `apply`) | make the cluster match cluster.yaml (phases: image → secrets → network/SG → discover → scale-down → upgrade → compute → bootstrap → kubeconfig → health) |
| `status` | show the provider endpoint, managed resources, cluster endpoints + `kubectl get nodes` (`-o yaml` for machine-readable output) |
| `check` | compare `talos.version` / `kubernetes.version` against the newest upstream releases and against what the nodes run; exits 1 if an update, a drifted node or a leftover cordon was found (`-o yaml` for machine-readable output) |
| `dashboard [NODE...]` | `talosctl dashboard` on all (reachable) nodes, or just the ones given |
| `env` | print provider CLI authentication exports: `eval "$(taloscluster env)"` |
| `image download\|remove` | upload or remove the shared provider boot image (converge never removes it) |
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

### Proxmox on existing networks

Stage 2 uses an existing bridge or VNet and a private Layer 2 Kubernetes API VIP;
it does not create Proxmox SDN objects. A minimal provider section is:

```yaml
controlplane:
  count: 3
  cores: 4
  memory: 8 # GB
  disk: 40

proxmox:
  url: https://pve.example.edu:8006
  storage: vms
  iso_storage: isos
  cidata_storage: local
  placement_strategy: spread
  nodes: [pve001, pve002, pve003]
  network:
    cluster:
      bridge: vmbr0
      kubeapi_vip: 172.29.21.240

network:
  cidr: 172.29.21.0/24
  dns: [172.29.21.1]
  ntp: [pool.ntp.org]
```

Use `vnet:` instead of `bridge:` for an existing VNet. A pool may set `node:`
to pin all of its machines; otherwise placement spreads creates across the
configured online nodes while reserving memory for earlier choices in the same
run. `cidata_storage` must be node-local because cidata temporarily contains the
Talos machine configuration and provider/extension secrets.

Set `proxmox.url` to the Proxmox server origin; taloscluster adds `/api2/json`
internally. Existing configurations that include the API path remain supported.

```yaml
# secrets.yaml
proxmox:
  token_id: taloscluster@pve!provider
  token_secret: CHANGE-ME
```

Every command performs a read-only `/access/permissions` preflight before a
Proxmox upload, VM/pool change, power action, or deletion. Missing privileges are
reported with their ACL paths. TLS certificate verification is enabled by
default; `proxmox.tls_verify` may name a CA bundle path.

The token also needs `Sys.AccessNetwork` on the node path so Proxmox can fetch
the boot ISO with `download-url`; tokens created before this requirement must
add it.

### Proxmox directly routed external NIC

`proxmox.network.external` adds a second VirtIO NIC on an externally routed
subnet, carrying the Kubernetes API VIP and the MetalLB `ingress_pool`:

```yaml
proxmox:
  network:
    cluster:
      bridge: vmbr0
    external:
      bridge: vmbr0
      vlan: 1691
      cidr: 141.142.36.0/25
      gateway: 141.142.36.1
      anchor_cidr: 169.254.32.0/20
      kubeapi_vip: 141.142.36.79
      ingress_pool: 141.142.36.75-141.142.36.78
```

`kubeapi_vip` moves to the external section when it is present. Each machine
gets a deterministic link-local anchor address derived from the cluster and
hostname; a collision aborts the run, so size `anchor_cidr` at `/20` or larger
rather than reusing a `/24`.

The per-VM Proxmox firewall defaults to deny-in/allow-out. Ports 6443 and 50000
are restricted to the `security.kubernetes` and `security.talos` allowlists, but
**80 and 443 are accepted from any source** — the external NIC is a routable
subnet, so ingress is deliberately open to the internet. Restrict it upstream if
that is not what you want.

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
2. **`secrets.yaml`** — OpenStack application credential or Proxmox API token + tailscale pre-auth
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

# Commands

Run `taloscluster --help` to list commands, `taloscluster COMMAND --help` for command help, and `taloscluster --version` to show the installed version. For workflows that combine commands, see [Usage](usage.md).

## Common options

Every subcommand accepts `-C DIR` / `--dir DIR` to select the cluster directory, defaulting to the current directory. Put this option after the subcommand: `taloscluster status -C mycluster`. Core provider operations read `cluster.yaml` and `secrets.yaml` together. `init` scaffolds them, while core version checks can use `cluster.yaml` alone; configured plugins may require additional credentials.

| Option | Commands | Meaning |
| --- | --- | --- |
| `--dry-run` | `converge`, `image`, `metal`, `destroy`, `plugin` | Print proposed changes without applying them |
| `--yes` | `converge`, `image`, `destroy`, `plugin` | Skip deletion prompts where implemented, including a direct `plugin NAME destroy` |
| `--reboot` | `converge`, `plan` | Apply or preview reboots needed for changed Proxmox VM sizing |
| `-o text` / `-o yaml` | `status`, `check`, `plugin` | Select output format; plugin output selection applies to `status` and `check` actions |
| `--serve` | `metal boot`, `metal join` | Serve the install ISO from the machine running the command over the LAN |

`sync` and `apply` accept the same options as `converge`. Text output is the default. Dry runs still need access to relevant services: planning reads provider inventory and can POST idempotent schematics to the Talos Image Factory.

## `init`

```bash
taloscluster init [--openstack | --proxmox] [--metal] [-C DIR] [NAME]
taloscluster init --proxmox -C mycluster mycluster
```

Scaffold `cluster.yaml`, `secrets.yaml`, and `.gitignore`. The scaffolded `cluster.yaml` lists `secrets.yaml` under [`include`](configuration/general.md#include), which is how the credentials merge in. The provider defaults to OpenStack and the name defaults to `mycluster`. `NAME` sets the name inside the configuration; `-C DIR` selects the directory and creates it if needed.

`--metal` appends a `metal:` example group with one server to `cluster.yaml` and the group's BMC credentials as `CHANGE-ME` placeholders to `secrets.yaml` ([Metal](configuration/metal.md)); it requires one of the provider flags, since bare-metal machines join a cluster a provider manages. The scaffolded group has `redfish: false`, so the pair loads until the credentials are real. The example group sits on another layer-2 network, so `--metal` also sets [`talos.kubespan`](configuration/general.md#taloskubespan) to `true` — written into the scaffolded file, and inserted under the existing `talos` section when `cluster.yaml` already exists, which the appended group needs to load. A long machine list can move into a file [`include`](configuration/general.md#include) names.

Existing configuration files are preserved. Installed plugins append missing sections, and `.gitignore` receives missing secret and derived-file entries. New `secrets.yaml` files have mode 0600. Edit both configuration files before running `plan`.

## `plan`

```bash
taloscluster plan [--reboot]
taloscluster plan -C mycluster --reboot
```

Preview a converge, including creates, updates, deletions, supported Proxmox VM sizing changes and OpenStack subnet DNS updates, firewall changes, the metal machines an [`auto_join`](configuration/metal.md#metalgroupauto_join) group would join, machine-configuration diffs with secrets redacted when the cluster is reachable, and plugin actions. Unsupported edits to an existing cluster (an OpenStack `flavor`/`disk`/`availability_zone` change, a Proxmox NIC, placement or `storage` change) are refused and reported. A new cluster without Talos secrets or allocated addresses cannot produce all machine-config diffs. A configured plugin whose work depends on the cluster's own kubeconfig or an allocated endpoint reports it as deferred until converge bootstraps the cluster, instead of failing the plan. Equivalent to `converge --dry-run`. `--reboot` also previews which nodes would restart to apply sizing changes.

## `converge`

```bash
taloscluster converge [--dry-run] [--yes] [--reboot]
taloscluster converge -C mycluster
```

Make the cluster match its configuration. `sync` and `apply` are aliases. Converge prepares images and Talos secrets, reconciles networking, discovers nodes, scales down removed nodes, upgrades existing nodes, creates new nodes, bootstraps a new cluster, writes client configuration, checks health, and runs plugins.

Change pool counts to scale, pinned versions to upgrade, or source allowlists to change access. Deletions require confirmation unless `--yes` is supplied. `--reboot` applies pending Proxmox sizing changes one node at a time with health checks. See [Usage](usage.md) and [Proxmox changes](providers/proxmox.md#changing-a-proxmox-cluster-after-it-exists) for supported updates and restrictions.

## `status`

```bash
taloscluster status [-o text | -o yaml]
taloscluster status -C mycluster -o yaml
```

Show the provider endpoint, managed resources, the bare-metal machines a [`metal`](configuration/metal.md) section configures, Kubernetes and ingress endpoints, and Kubernetes nodes when reachable. Configured plugins add their own reports. Use YAML output for scripts.

## `check`

```bash
taloscluster check [-o text | -o yaml]
taloscluster check -C mycluster -o yaml
```

Compare pinned Talos and Kubernetes versions with upstream releases and the versions nodes actually run. The Kubernetes releases are the ones Sidero has published a Talos kubelet image for (`ghcr.io/siderolabs/kubelet`), which trail the upstream release by a few days, so `check` never suggests a version Talos cannot run yet. Report the latest patch of the pinned minor separately from the newest release overall, version drift, and leftover cordons. It also warns about the configuration problems converge's preflight reports, such as a stated host network overlapping the Kubernetes pod or service subnets. Configured plugins also report whether they need changes. This command reports updates; it does not install them.

Exit status is `1` when an update, drift, cordon, unsuccessful plugin check, or an incomplete check is reported. A check is incomplete when upstream releases could not be fetched, a node's version is unknown, or a configured machine is missing from both Talos discovery and the Kubernetes node list, so it never passes unverified: the report carries `incomplete: true` and an `incomplete_reasons` list, and text output warns for each reason. Nothing is assumed current in that case. `check` can also inspect pinned versions before a cluster exists, where missing machines are expected rather than reported.

## `dashboard`

```bash
taloscluster dashboard [NODE ...]
taloscluster dashboard 192.168.0.10 192.168.0.11
```

Open `talosctl dashboard` for the given node addresses, or discover all nodes when none are supplied. Unreachable Talos API targets are reported and omitted. Use real node addresses, and ensure the management machine can reach them through the tailnet or a direct route.

## `env`

```bash
taloscluster env
eval "$(taloscluster env)"
```

Print shell authentication exports for the selected provider: `OS_*` variables for OpenStack or `PVE_API_*` variables for Proxmox. Values come from `cluster.yaml` and `secrets.yaml`. The output contains credential secrets and is intended for shell evaluation; avoid saving it in logs.

## `image`

```bash
taloscluster image download [--dry-run]
taloscluster image remove [--dry-run] [--yes]
```

`download` builds or downloads the shared boot image for the configured Talos version and base schematic, then uploads it to the provider if missing. Converge normally handles this automatically. `remove` deletes the shared image and requires confirmation unless `--yes` is set. The image can be used by multiple clusters; neither converge nor destroy removes it automatically. `remove` also deletes the legacy `talos-<version>-tailscale` image a cluster still carries from before the schematic id joined the name; on Proxmox it refuses to delete an image that any cluster-managed VM still boots from, since a VM whose cdrom volume is gone fails to start.

## `metal`

```bash
taloscluster metal inspect SERVER
taloscluster metal boot SERVER [--serve] [--force]
taloscluster metal wait SERVER
taloscluster metal apply SERVER [--force]
taloscluster metal eject SERVER
taloscluster metal join SERVER [--serve] [--force]
taloscluster metal join srv01 --serve
```

Join the bare-metal machines of a [`metal`](configuration/metal.md) section — the explicit path, one machine at a time; converge itself joins the machines of an [`auto_join`](configuration/metal.md#metalgroupauto_join) group during its compute phase (see [Lifecycle](concepts/lifecycle.md)). Every command names one machine — a `metal.<group>.servers` key. `inspect` prints a Redfish summary of the machine's power state, one-time boot setting, NICs and disks. `boot` mounts the Talos install ISO in the machine's virtual media, sets a one-time boot from it and powers the machine on; `--serve` downloads that ISO and serves it from the machine running the command over the LAN instead of handing the BMC a factory URL, for a controller with no internet egress — a standalone `boot --serve` keeps serving until Ctrl-C, and `join` keeps serving while it waits and applies. `wait` polls for the maintenance-mode apid on the machine's cluster address, `apply` generates the machine config against the cluster endpoint the provider resolved — the same one the VM nodes' configurations carry, so the provider must be reachable — writes it to `.metal/` in the cluster directory (mode 0600, since it carries cluster credentials) and pushes it to the maintenance-mode node, and `eject` unmounts the virtual media. `join` runs boot, wait, apply, eject and verify in order, where verify waits for the node to come back with its configuration and reports the Talos version it runs. A machine with [`redfish: false`](configuration/metal.md#metalgroupredfish) is never touched through its BMC: its `join` becomes wait, apply and verify, and `inspect`, `boot` and `eject` skip the BMC with a notice, leaving the operator to boot the machine into maintenance mode themselves. `--dry-run` prints the state-changing actions — the media mount, one-time boot and power-on, the machine-config write and push, the media eject — without doing them; `inspect` and `wait` only look, so the flag changes nothing for them, and a `join --dry-run` lists the flow without polling, since nothing was booted and the wait steps would only run out their timeouts.

The BMC is only ever used to mount media, one-time boot from it and control power: BIOS boot-mode settings and the persistent boot order are left alone, so after the one-time boot the machine boots whatever its own order says — for an installed machine, its disk. A machine that already answers the cluster's apid is refused by `boot`, `apply` and `join`, since the flow reinstalls it and wipes the disk — `boot` would force-restart a running node straight into the install media. The probe asks the machine's maintenance apid directly and its cluster apid through the control plane, the way every other talosctl call is dialled, since the machine running the command may not route the machine's address; a machine that answers neither cannot be told from a joined one and is refused too, unless `--force` says the machine is really not joined. The refusal also covers a cluster directory whose `talosconfig` is missing: the client config is derived state, so a throwaway one is built from the machine secrets and the guard keeps probing. `taloscluster converge` reconfigures an installed node instead, or reset the machine first if a re-join is really intended.

## `destroy`

```bash
taloscluster destroy [--dry-run] [--yes]
taloscluster destroy -C mycluster --dry-run
```

Run plugin cleanup first, in reverse order, while the cluster is reachable, then delete the cluster’s managed infrastructure resources and local `talossecrets.yaml` and `kubeconfig`. A cluster that never bootstrapped has no kubeconfig, so its destroy skips the plugin cleanup — there is nothing for the plugins to remove. The shared boot image is retained. Destruction requires confirmation unless `--yes` is supplied. Keep any required backups before destroying a cluster. The bare-metal machines of a [`metal`](configuration/metal.md) section are not deleted — no provider manages them — so destroy names each one and the `talosctl reset` its hardware needs before it can join another cluster; a cluster with such a section keeps its `talosconfig` so that reset can run after the destroy, `--yes` included, until the next converge overwrites the file.

## `plugin`

```bash
taloscluster plugin list
taloscluster plugin NAME [converge | plan | destroy | status | check] [--dry-run] [--yes]
taloscluster plugin argocd plan
taloscluster plugin rancher check -o yaml
```

`list` shows installed plugins in execution order and, when a cluster configuration is available, whether each is configured. It can run outside a cluster directory; a `cluster.yaml` that is present but does not load is an error like any other command's, not a missing configuration.

With a plugin name, run only that plugin. A standalone `converge`, `plan` or `destroy` runs that plugin's `validate` hook before any mutation, so a malformed or contradictory section (an unsupported override, a lone repository URL, an unsupported apply target) is refused up front. A direct `destroy` prompts for the cluster name before it runs, exactly like the top-level `destroy`, unless `--yes` is supplied; a `--dry-run` lists what it would delete first. The action defaults to `converge`; `plan` runs its converge hook in dry-run mode. `-o text` or `-o yaml` selects output for `status` and `check`. An unconfigured plugin is skipped, while an unknown plugin or an unsupported hook is an error. See [Plugins](concepts/plugins.md) for installation, configuration, and hook behavior.

## Exit status

Successful commands normally exit `0`. Plugin load, activation, and init failures can be warnings only, and `status` embeds plugin errors in its report without making the exit status nonzero. Errors exit `1`, reported as a single `ERROR:` line on stderr — provider API errors (a Neutron conflict, an OpenStack wait/delete timeout, a quota error, a rejected credential, an unreachable cloud) are wrapped at the backend boundary so no raw SDK traceback leaks to the command line, and a failed boot-image download from the Talos factory is reported the same way; `check` and plugin checks also use `1` for a report that needs attention. Invalid command-line arguments exit `2`, and an interrupted command handled by the CLI exits `130` — a confirmation prompt whose stdin has no answer, as in CI without `--yes`, aborts the same way instead of tracebacking.

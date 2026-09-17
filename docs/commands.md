# Commands

Run `taloscluster --help` to list commands, `taloscluster COMMAND --help` for command help, and `taloscluster --version` to show the installed version. For workflows that combine commands, see [Usage](usage.md).

## Common options

Every subcommand accepts `-C DIR` / `--dir DIR` to select the cluster directory, defaulting to the current directory. Put this option after the subcommand: `taloscluster status -C mycluster`. Core provider operations read `cluster.yaml` and `secrets.yaml` together. `init` scaffolds them, while core version checks can use `cluster.yaml` alone; configured plugins may require additional credentials.

| Option | Commands | Meaning |
| --- | --- | --- |
| `--dry-run` | `converge`, `image`, `destroy`, `plugin` | Print proposed changes without applying them |
| `--yes` | `converge`, `image`, `destroy`, `plugin` | Skip deletion prompts where implemented, including a direct `plugin NAME destroy` |
| `--reboot` | `converge`, `plan` | Apply or preview reboots needed for changed Proxmox VM sizing |
| `-o text` / `-o yaml` | `status`, `check`, `plugin` | Select output format; plugin output selection applies to `status` and `check` actions |

`sync` and `apply` accept the same options as `converge`. Text output is the default. Dry runs still need access to relevant services: planning reads provider inventory and can POST idempotent schematics to the Talos Image Factory.

## `init`

```bash
taloscluster init [--openstack | --proxmox] [-C DIR] [NAME]
taloscluster init --proxmox -C mycluster mycluster
```

Scaffold `cluster.yaml`, `secrets.yaml`, and `.gitignore`. The provider defaults to OpenStack and the name defaults to `mycluster`. `NAME` sets the name inside the configuration; `-C DIR` selects the directory and creates it if needed.

Existing configuration files are preserved. Installed plugins append missing sections, and `.gitignore` receives missing secret and derived-file entries. New `secrets.yaml` files have mode 0600. Edit both configuration files before running `plan`.

## `plan`

```bash
taloscluster plan [--reboot]
taloscluster plan -C mycluster --reboot
```

Preview a converge, including creates, updates, deletions, supported Proxmox VM sizing changes and OpenStack subnet DNS updates, firewall changes, machine-configuration diffs with secrets redacted when the cluster is reachable, and plugin actions. Unsupported edits to an existing cluster (an OpenStack `flavor`/`disk`/`availability_zone` change, a Proxmox NIC, placement or `storage` change) are refused and reported. A new cluster without Talos secrets or allocated addresses cannot produce all machine-config diffs. A configured plugin whose work depends on the cluster's own kubeconfig or an allocated endpoint reports it as deferred until converge bootstraps the cluster, instead of failing the plan. Equivalent to `converge --dry-run`. `--reboot` also previews which nodes would restart to apply sizing changes.

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

Show the provider endpoint, managed resources, Kubernetes and ingress endpoints, and Kubernetes nodes when reachable. Configured plugins add their own reports. Use YAML output for scripts.

## `check`

```bash
taloscluster check [-o text | -o yaml]
taloscluster check -C mycluster -o yaml
```

Compare pinned Talos and Kubernetes versions with upstream releases and the versions nodes actually run. Report the latest patch of the pinned minor separately from the newest release overall, version drift, and leftover cordons. Configured plugins also report whether they need changes. This command reports updates; it does not install them.

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

`download` builds or downloads the shared boot image for the configured Talos version and base schematic, then uploads it to the provider if missing. Converge normally handles this automatically. `remove` deletes the shared image and requires confirmation unless `--yes` is set. The image can be used by multiple clusters; neither converge nor destroy removes it automatically.

## `destroy`

```bash
taloscluster destroy [--dry-run] [--yes]
taloscluster destroy -C mycluster --dry-run
```

Run plugin cleanup first, in reverse order, while the cluster is reachable, then delete the cluster’s managed infrastructure resources and local `talossecrets.yaml`, `talosconfig`, and `kubeconfig`. The shared boot image is retained. Destruction requires confirmation unless `--yes` is supplied. Keep any required backups before destroying a cluster.

## `plugin`

```bash
taloscluster plugin list
taloscluster plugin NAME [converge | plan | destroy | status | check] [--dry-run] [--yes]
taloscluster plugin argocd plan
taloscluster plugin rancher check -o yaml
```

`list` shows installed plugins in execution order and, when a cluster configuration is available, whether each is configured. It can run outside a cluster directory.

With a plugin name, run only that plugin. A standalone `converge`, `plan` or `destroy` runs that plugin's `validate` hook before any mutation, so a malformed or contradictory section (an unsupported override, a lone repository URL, an unsupported apply target) is refused up front. A direct `destroy` prompts for the cluster name before it runs, exactly like the top-level `destroy`, unless `--yes` is supplied; a `--dry-run` lists what it would delete first. The action defaults to `converge`; `plan` runs its converge hook in dry-run mode. `-o text` or `-o yaml` selects output for `status` and `check`. An unconfigured plugin is skipped, while an unknown plugin or an unsupported hook is an error. See [Plugins](concepts/plugins.md) for installation, configuration, and hook behavior.

## Exit status

Successful commands normally exit `0`. Plugin load, activation, and init failures can be warnings only, and `status` embeds plugin errors in its report without making the exit status nonzero. Errors exit `1`, reported as a single `ERROR:` line on stderr — provider API errors (a Neutron conflict, an OpenStack wait/delete timeout, a quota error, a rejected credential, an unreachable cloud) are wrapped at the backend boundary so no raw SDK traceback leaks to the command line; `check` and plugin checks also use `1` for a report that needs attention. Invalid command-line arguments exit `2`, and an interrupted command handled by the CLI exits `130`.

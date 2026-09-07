# Troubleshooting

Run commands from the cluster directory, or select it with `-C DIR`. Start with `taloscluster status` to inspect resources and endpoints, and `taloscluster plan` to compare configuration with the current cluster.

## Configuration is missing or uses the wrong provider

Check that you selected the directory containing both `cluster.yaml` and `secrets.yaml`. `init NAME` sets the cluster name but does not select a directory; use `init -C DIR NAME` to create files elsewhere. Exactly one provider must be selected, and its credential block must match. See [Configuration](configuration.md).

## A node cannot be reached

Confirm that the VM is running and that the management machine can reach its Talos API on port 50000. With Tailscale, check the login server and pre-auth key and confirm that both the management machine and nodes joined the same tailnet. Without Tailscale, check the route to the real private node addresses.

The [`security`](configuration/security.md) allowlists must permit your management network for both the Talos and Kubernetes APIs. Talos API commands must use real node addresses; the Kubernetes API VIP or floating IP is not a Talos API endpoint. See [Machines and access](concepts/machines.md#reaching-the-nodes).

## Recreating a cluster reuses stale Headscale entries

Before reusing a destroyed cluster’s name, remove its stale nodes from Headscale so reused hostnames resolve to the new machines. Replace `tailscale.auth_key` when it has expired or has already exhausted its permitted uses.

## Proxmox reports missing privileges

The permission preflight reports missing privileges with their ACL paths. Compare the token’s effective permissions with [Proxmox API token permissions](providers/proxmox.md#proxmox-api-token-permissions). Check `Sys.AccessNetwork` on compute-node paths if downloading the boot ISO fails, and SDN privileges when using a managed network.

## Proxmox sizing still shows pending changes

Use `taloscluster plan --reboot`, then `taloscluster converge --reboot`, to review and apply the needed VM restarts. A reboot inside the guest, including a Talos upgrade, does not replace the Proxmox VM process that holds pending CPU and memory settings. Disk growth needs a reboot for Talos to extend its partition; use `--reboot` in the same converge that grows the disk. See [Changing a Proxmox cluster](providers/proxmox.md#changing-a-proxmox-cluster-after-it-exists).

## Managed SDN nodes have no egress or transfers stall

Check FRR, IP forwarding, BGP and VXLAN firewall rules, the exit nodes’ routed uplinks, and VXLAN offload settings against the [managed SDN prerequisites](providers/proxmox.md#managed-evpn-sdn). An available zone alone does not establish that BGP is connected. Resolve other administrators’ pending SDN changes before retrying a run that refuses to apply them.

## `check` exits with status 1

Read the report, or use `taloscluster check -o yaml`. Exit status 1 can mean an available version update, a node running a different version, a leftover cordon, a plugin needing changes, or an error. It is not by itself evidence that the cluster is down. See [`check`](commands.md#check).

## `check` succeeds while version data is unavailable

An upstream lookup failure is a warning, and unknown node versions do not count as drift. A zero exit status therefore does not guarantee that all releases and nodes were checked. Inspect warnings, empty upstream version fields, and the reported node list.

## An installed plugin does not run

Use `taloscluster plugin list` to distinguish a missing package from an unconfigured plugin. Install the needed extra in the same tool environment as taloscluster, then fill in the plugin’s configuration and secrets. Running `init` again adds missing plugin sections while preserving existing configuration. See [Plugins](concepts/plugins.md).

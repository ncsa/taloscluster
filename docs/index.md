# taloscluster

`taloscluster` provisions and manages [Talos Linux](https://www.talos.dev/) Kubernetes clusters on OpenStack or Proxmox from a single declarative file. You describe the cluster you want in `cluster.yaml`, and `taloscluster converge` makes reality match it. Creating, scaling up, scaling down and rolling Talos or Kubernetes upgrades are all the same command, and re-running it is always safe.

There is no state file. Every resource is named deterministically and tagged, converge discovers what exists by tag, creates what is missing, and never touches resources it did not create.

## Where to start

- **[Configuration](configuration.md)**: every key in `cluster.yaml` and `secrets.yaml`, with one page per section.
- **[README on GitHub](https://github.com/ncsa/taloscluster#readme)**: installation, commands, provider setup and the plugin system.

## Quick start

```bash
uv tool install "taloscluster @ git+https://github.com/ncsa/taloscluster"
taloscluster init --proxmox mycluster   # or --openstack
cd mycluster
vi secrets.yaml                         # provider credential + tailscale key
vi cluster.yaml                         # versions, pools, provider, network, allowlists
taloscluster plan                       # dry run, changes nothing
taloscluster converge                   # create the cluster
```

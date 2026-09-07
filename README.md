# taloscluster

`taloscluster` provisions and manages [Talos Linux](https://www.talos.dev/) Kubernetes clusters on OpenStack or Proxmox. Describe the cluster in `cluster.yaml`, keep credentials in `secrets.yaml`, and run `taloscluster converge` to create, scale, or upgrade it.

Resources are discovered by name and ownership tags, without a separate infrastructure state file. Optional plugins register the cluster with Rancher or ArgoCD.

**[Documentation](https://ncsa.github.io/taloscluster/)** · [Quickstart](https://ncsa.github.io/taloscluster/quickstart/) · [Command reference](https://ncsa.github.io/taloscluster/commands/)

## Install

Install [uv](https://docs.astral.sh/uv/) and make `talosctl` and `kubectl` available on PATH, then:

```bash
uv tool install git+https://github.com/ncsa/taloscluster
```

For optional Rancher and ArgoCD plugins, install the `all` extra:

```bash
uv tool install "taloscluster[all] @ git+https://github.com/ncsa/taloscluster"
```

See [Installation](https://ncsa.github.io/taloscluster/installation/) for updating, running without installing, and development setup.

## Get started

Prepare your provider credentials and node access using the [Quickstart](https://ncsa.github.io/taloscluster/quickstart/), then:

```bash
taloscluster init --openstack -C mycluster mycluster  # or --proxmox
cd mycluster
vi cluster.yaml secrets.yaml
taloscluster plan
taloscluster converge
taloscluster status
```

Edit the generated templates for your environment before converging. Back up `talossecrets.yaml` after the first converge; it holds the cluster’s cryptographic identity.

## Documentation

- [Usage](https://ncsa.github.io/taloscluster/usage/): scaling, upgrades, access changes, and teardown.
- [Commands](https://ncsa.github.io/taloscluster/commands/): subcommands, options, examples, and exit status.
- [Configuration](https://ncsa.github.io/taloscluster/configuration/): every cluster and secrets setting.
- [Proxmox setup](https://ncsa.github.io/taloscluster/providers/proxmox/): permissions, networking, and managed SDN.
- [Plugins](https://ncsa.github.io/taloscluster/concepts/plugins/): Rancher, ArgoCD, and writing a plugin.
- [Troubleshooting](https://ncsa.github.io/taloscluster/troubleshooting/): common problems and diagnostic steps.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run mkdocs serve
```

See [development installation](https://ncsa.github.io/taloscluster/installation/#development-installation) for running the working tree with plugins, [CHANGELOG.md](CHANGELOG.md) for release history, and [todo.md](todo.md) for outstanding work.

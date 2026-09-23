# Configuration

A cluster directory holds two files that taloscluster reads together. `cluster.yaml` describes the desired state and is safe to commit. `secrets.yaml` holds credentials and is gitignored; `cluster.yaml` lists it under [`include`](configuration/general.md#include) and it is merged into `cluster.yaml` before validation like any included file, so its keys are ordinary `cluster.yaml` keys that happen to live in a file you do not commit. `taloscluster init [--openstack|--proxmox] [--metal] NAME` scaffolds both, and the core loader validates required fields and supported values. A top-level key neither core nor an installed plugin owns is refused — a misspelled or unsupported section is caught instead of silently ignored. A miscapped or unsupported key inside a fixed-schema section is refused too, including in nested fixed-schema blocks such as `proxmox.network` — `talos.extensons`, `network.dnss`, `openstack.regoin` or `proxmox.network.clustr` no longer load quietly (a typo like `openstack.regoin` used to silently fall back to the `RegionOne` default). Freeform maps are left open: cluster and pool `tags` are arbitrary label maps, security host labels are free-form, and `config_patches` hold arbitrary YAML. Sections owned by an installed plugin (e.g. `argocd`, `rancher`) are retained as valid; each plugin's `validate` hook runs in converge's validate phase for every installed plugin before any mutation, so a malformed or contradictory plugin section is refused up front instead of being checked only when the plugin's hooks run later.

All example addresses and hostnames in these pages are placeholders: RFC 5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) for routed networks, RFC 1918 ranges for private node networks, and `example.edu` hostnames.

## cluster.yaml

| Key | Required | What it is | Details |
| --- | --- | --- | --- |
| `include` | no | Extra YAML files merged into `cluster.yaml` | [General](configuration/general.md#include) |
| `name` | yes | Cluster name, prefix of every hostname | [General](configuration/general.md#name) |
| `tags` | no | Extra Kubernetes node labels for every node | [General](configuration/general.md#tags) |
| `talos` | yes | Talos version, extra extensions, machine-config patches | [General](configuration/general.md#talos) |
| `kubernetes` | yes | Kubernetes version | [General](configuration/general.md#kubernetes) |
| `controlplane` | yes | Control plane pool: count and sizing | [Pools](configuration/pools.md) |
| `workers` | no | Worker pools by name: count, sizing, extensions, tags | [Pools](configuration/pools.md) |
| `openstack` | at most one | OpenStack endpoint, availability zone, external network, optional region | [OpenStack](configuration/openstack.md) |
| `proxmox` | at most one | Proxmox endpoint, storages, placement, and the bridge, VNet or SDN each network is reached through | [Proxmox](configuration/proxmox.md) |
| `metal` | no | Bare-metal machine groups joined alongside the VM provider | [Metal](configuration/metal.md) |
| `network` | yes | The node L2 (`network.cluster`), the optional routed L2 (`network.external`), DNS and NTP servers | [Network](configuration/network.md) |
| `security` | no | Named ingress allowlists per port | [Security](configuration/security.md) |
| `tailscale` | no | Opt into the tailscale extension, login server | [Tailscale](configuration/tailscale.md) |
| `rancher` | no | Rancher plugin: members to grant access | [Rancher](configuration/rancher.md) |
| `argocd` | no | ArgoCD plugin: project roles, repositories, per-app settings | [ArgoCD](configuration/argocd.md) |
| `charts` | no | Charts plugin: Helm charts and manifests installed into the cluster | [Charts](configuration/charts.md) |

At most one of `openstack` or `proxmox` may be present, and one of them is required: a [`metal`](configuration/metal.md) section may accompany it, but a cluster whose machines are all bare metal is refused at load. The VM provider selects the backend and decides which pool sizing keys are required.

Minimal Proxmox example:

```yaml
name: mycluster

# secrets.yaml (gitignored) is merged in through this include
include: [secrets.yaml]

talos:
  version: v1.13.8
kubernetes:
  version: v1.36.1

controlplane:
  count: 3
  cores: 4
  memory: 8
  disk: 40

workers:
  worker:
    count: 3
    cores: 8
    memory: 16
    disk: 100

proxmox:
  url: https://pve.example.edu:8006
  storage: local-lvm
  iso_storage: local
  network:
    cluster:
      bridge: vmbr0

network:
  cluster:
    cidr: 10.0.0.0/24
    kubeapi_vip: 10.0.0.10
  dns: [192.0.2.53]
  ntp: [ntp.example.edu]

security:
  kubernetes:
    office vpn: 198.51.100.0/24
  talos:
    office vpn: 198.51.100.0/24
```

## secrets.yaml

Never commit this file. `taloscluster init` writes it with mode 0600 and adds it to `.gitignore`. Its contents are merged into `cluster.yaml`, so it follows the same schema and the same rules: a value set both here and in `cluster.yaml` is refused naming both files, and the provider section must be the one `cluster.yaml` selects (two provider sections across the files trip the one-provider rule). Where a credential is written is your choice — the split below is the scaffolded default, and the plugin credentials follow the same rule, so a Rancher `url`/`token`, an ArgoCD apply target or the OpenStack application credential may live in `cluster.yaml` or another included file just as well. `secrets.yaml` is an ordinary included file: the scaffold writes `include: [secrets.yaml]` into `cluster.yaml`, a section it carries opts its feature in exactly as one in `cluster.yaml` does — a `tailscale:` block here switches Tailscale on, and a `rancher:` block here activates the plugin — and the loader refuses a `secrets.yaml` that no `include:` names rather than silently dropping its credentials, so a cluster directory from before this contract needs that one line added. Each credential value must be a real, non-empty string: a null, non-string, or still-scaffolded `CHANGE-ME` placeholder is refused when a command needs the credential (see `init` and the provider/Tailscale pages) instead of failing later as an opaque 401. Commands that never talk to the provider, such as `check`, load a cluster whose credentials are absent, and a listed include file that is missing reads as empty there, with a warning, so `check` still gates CI before the credentials exist.

| Key | Required | What it is | Details |
| --- | --- | --- | --- |
| `openstack` | on OpenStack | Application credential id and secret | [OpenStack](configuration/openstack.md#secretsyaml) |
| `proxmox` | on Proxmox | API token id and secret | [Proxmox](configuration/proxmox.md#secretsyaml) |
| `metal` | for `redfish` groups | BMC username and password, per group | [Metal](configuration/metal.md#metalgroupbmc) |
| `tailscale` | no | Pre-auth key nodes register with | [Tailscale](configuration/tailscale.md#secretsyaml) |
| `rancher` | no | Rancher server URL and token | [Rancher](configuration/rancher.md#secretsyaml) |
| `argocd` | no | How to reach the ArgoCD cluster | [ArgoCD](configuration/argocd.md#secretsyaml) |
| `charts` | no | Charts plugin: CephX credentials for the ceph-csi Secrets | [Charts](configuration/charts.md#secretsyaml) |

```yaml
proxmox:
  token_id: "taloscluster@pve!provider"
  token_secret: "CHANGE-ME"

tailscale:
  auth_key: "CHANGE-ME"
```

## Other files in the directory

`talossecrets.yaml` is the cluster's cryptographic identity, generated on the first converge and gitignored — it holds the cluster CA, the etcd CA, the join tokens, and the passphrase the machines' system disks are encrypted with. It cannot be regenerated for a running cluster, so back it up out of band. `talosconfig` is derived from it and refreshed by converge. `kubeconfig` is fetched during bootstrap or an API endpoint move and is otherwise retained. Keep both client files with the cluster directory, especially when moving the API endpoint.

# Configuration

A cluster directory holds two files that taloscluster reads together. `cluster.yaml` describes the desired state and is safe to commit. `secrets.yaml` holds credentials and is gitignored. `taloscluster init [--openstack|--proxmox] NAME` scaffolds both, and the core loader validates required fields and supported values. A top-level key neither core nor an installed plugin owns is refused — a misspelled or unsupported section is caught instead of silently ignored. A miscapped or unsupported key inside a fixed-schema section is refused too — `talos.extensons`, `network.dnss` or `openstack.regoin` no longer loads and is silently dropped (a typo like `openstack.regoin` used to silently fall back to the `RegionOne` default). Freeform maps are left open: cluster and pool `tags` are arbitrary label maps, security host labels are free-form, and `config_patches` hold arbitrary YAML. Sections owned by an installed plugin (e.g. `argocd`, `rancher`) are retained as valid; each plugin validates the keys inside its own section when its hooks run, so a successful core validation does not validate every plugin setting.

All example addresses and hostnames in these pages are placeholders (RFC 5737 documentation ranges and `example.edu`).

## cluster.yaml

| Key | Required | What it is | Details |
| --- | --- | --- | --- |
| `name` | yes | Cluster name, prefix of every hostname | [General](configuration/general.md#name) |
| `tags` | no | Extra Kubernetes node labels for every node | [General](configuration/general.md#tags) |
| `talos` | yes | Talos version, extra extensions, machine-config patches | [General](configuration/general.md#talos) |
| `kubernetes` | yes | Kubernetes version | [General](configuration/general.md#kubernetes) |
| `controlplane` | yes | Control plane pool: count and sizing | [Pools](configuration/pools.md) |
| `workers` | no | Worker pools by name: count, sizing, extensions, tags | [Pools](configuration/pools.md) |
| `openstack` | one of | OpenStack endpoint, availability zone, external network, optional region | [OpenStack](configuration/openstack.md) |
| `proxmox` | one of | Proxmox endpoint, storages, placement, networks | [Proxmox](configuration/proxmox.md) |
| `network` | yes | Private CIDR, DNS and NTP servers | [Network](configuration/network.md) |
| `security` | no | Named ingress allowlists per port | [Security](configuration/security.md) |
| `tailscale` | no | Opt into the tailscale extension, login server | [Tailscale](configuration/tailscale.md) |
| `rancher` | no | Rancher plugin: members to grant access | [Rancher](configuration/rancher.md) |
| `argocd` | no | ArgoCD plugin: project roles, repositories, per-app settings | [ArgoCD](configuration/argocd.md) |

Exactly one of `openstack` or `proxmox` must be present. It selects the backend and decides which pool sizing keys are required.

Minimal Proxmox example:

```yaml
name: mycluster

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
      kubeapi_vip: 10.0.0.10

network:
  cidr: 10.0.0.0/24
  dns: [192.0.2.53]
  ntp: [ntp.example.edu]

security:
  kubernetes:
    office vpn: 198.51.100.0/24
  talos:
    office vpn: 198.51.100.0/24
```

## secrets.yaml

Never commit this file. `taloscluster init` writes it with mode 0600 and adds it to `.gitignore`. The provider block must match the provider chosen in `cluster.yaml`, and the other provider's block may not be present. Each credential value must be a real, non-empty string: a null, non-string, or still-scaffolded `CHANGE-ME` placeholder is refused at secrets load time (see `init` and the provider/Tailscale pages) instead of failing later as an opaque 401.

| Key | Required | What it is | Details |
| --- | --- | --- | --- |
| `openstack` | on OpenStack | Application credential id and secret | [OpenStack](configuration/openstack.md#secretsyaml) |
| `proxmox` | on Proxmox | API token id and secret | [Proxmox](configuration/proxmox.md#secretsyaml) |
| `tailscale` | no | Pre-auth key nodes register with | [Tailscale](configuration/tailscale.md#secretsyaml) |
| `rancher` | no | Rancher server URL and token | [Rancher](configuration/rancher.md#secretsyaml) |
| `argocd` | no | How to reach the ArgoCD cluster | [ArgoCD](configuration/argocd.md#secretsyaml) |

```yaml
proxmox:
  token_id: "taloscluster@pve!provider"
  token_secret: "CHANGE-ME"

tailscale:
  auth_key: "CHANGE-ME"
```

## Other files in the directory

`talossecrets.yaml` is the cluster's cryptographic identity, generated on the first converge and gitignored. It cannot be regenerated for a running cluster, so back it up out of band. `talosconfig` is derived from it and refreshed by converge. `kubeconfig` is fetched during bootstrap or an API endpoint move and is otherwise retained. Keep both client files with the cluster directory, especially when moving the API endpoint.

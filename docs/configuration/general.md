# General settings

Back to the [configuration index](../configuration.md).

```yaml
name: mycluster

tags:
  team: platform

talos:
  version: v1.13.8
  extensions:
    - siderolabs/iscsi-tools
  config_patches:
    - |
      machine:
        sysctls:
          vm.max_map_count: "262144"
  kubespan: true

kubernetes:
  version: v1.36.1
```

## `include`

Optional · list of file names · default nothing included

Extra YAML files, named relative to the cluster directory, that are merged into `cluster.yaml` before it is validated. [`secrets.yaml`](../configuration.md#secretsyaml) is one of them — the scaffold writes `include: [secrets.yaml]` — and the loader refuses a `secrets.yaml` in the directory that the list does not name, so an existing cluster cannot silently lose its credentials. They carry the same keys as `cluster.yaml` — the core sections and the sections installed plugins own alike — so where a value lives is your choice: the schema, the error messages and the result are the same either way. A top-level section is itself a setting: a section in an included file opts the cluster into its feature exactly as writing it in `cluster.yaml` does — a `tailscale:` section in `secrets.yaml` or any other include switches Tailscale on. Mappings merge key by key; a value set in two files is refused, naming the key and both files, rather than one file quietly winning. The list may not name `cluster.yaml` itself; an included file may not include further files, may not be listed twice, and the paths stay inside the cluster directory (symlinks included). Once the files are merged there is one tree and one schema, so an unknown or misplaced key deeper inside a section is reported against `cluster.yaml` whichever file supplied it; only an included file's top-level keys are reported against that file. The motivating case is a long list that would drown the main file, such as the bare-metal machines:

```yaml
include: [metal.yaml]
```

## `name`

Required · string

The cluster name. Lowercase letters, digits and internal hyphens only. It prefixes every hostname (`<name>-controlplane-01`, `<name>-<pool>-01`) and determines the Proxmox resource pool (`taloscluster-<name>`) and, by default, the managed SDN zone id. Boot images are shared and named by Talos version, independently of the cluster name. Together with the longest pool name it must keep hostnames under 63 characters.

## `tags`

Optional · mapping of label to value

Extra Kubernetes node labels applied to every node through Talos `machine.nodeLabels`. Keys and values are stringified. Every node gets `ncsa/role` and `ncsa/pool`. OpenStack also adds `ncsa/project` when the project name is available; Proxmox supplies no default project label. Spaces in all label values become `_`, and a tag here may override a default. Per-pool `tags` win over cluster-wide tags on the same key.

## `talos`

### `talos.version`

Required · `vMAJOR.MINOR.PATCH`

Talos release to run; use the canonical `vMAJOR.MINOR.PATCH` form. A missing `v` prefix is normalized to the canonical form during load, and prerelease/build suffixes are accepted, but upstream lookup and upgrade behavior is designed around release versions. Must be v1.13.0 or newer because the generated machine configuration uses multi-document network kinds that older releases reject. Bumping it builds a new boot image from factory.talos.dev and rolls the upgrade over existing nodes on the next converge. Nothing auto-upgrades, and moving backwards is refused: converge's validate phase reads the running version from the cluster and refuses a pin older than it, the same refusal [`kubernetes.version`](#kubernetesversion) gets, rather than reinstall every node onto an older release.

### `talos.extensions`

Optional · list of strings · default empty

Extra Talos system extensions merged with the QEMU guest agent (VM pools only — bare metal has no QEMU host for it to reach, so it is left out of metal images and installers), Tailscale when enabled, and pool-level `extensions`. An explicit `siderolabs/tailscale` entry keeps that extension even without a `tailscale` section. Proxmox installs the resolved image on first boot; OpenStack initially boots the shared volume image, so a node converges onto those extensions through a Talos upgrade rather than at first boot. Extensions activate during installation or upgrade, not merely when a machine configuration is applied. Converge detects an extension-only change by comparing a node's running schematic (the Image Factory's `schematic` extension reported by `talosctl get extensions`) against cluster.yaml, and after a bootstrap or scale-up asks any node that came up short of its configured extensions to reinstall, so adding or removing an extension reliably takes effect.

### `talos.config_patches`

Optional · list of YAML documents as strings · default empty

Freeform machine-config patches applied to every node. Pool-level `config_patches` are applied after these, so a pool patch wins on conflict.

### `talos.kubespan`

Optional · boolean · default `false`

Set it to `true` to enable Talos KubeSpan on every node: the machine configuration turns the WireGuard overlay on and sizes its MTU to the node's layer-2 network MTU minus the 80 bytes of WireGuard overhead, so overlay traffic fragments at the same point the underlying network does. When [`network.external`](network.md#networkexternal) is configured, the endpoint filters allow every address a node owns and then remove the external network's `cidr` and `anchor_cidr`, so nodes advertise their cluster-network addresses as WireGuard peer endpoints but never an external one.

The overlay is what lets one cluster span layer-2 networks — VMs on the provider's network and bare metal on its own — because pod traffic between nodes is encrypted and routed over it, wherever an IP route connects the two sides, and peers find each other through Talos's discovery service. KubeSpan does not carry the Kubernetes API VIP: the kubelet on every node still reaches [`network.cluster.kubeapi_vip`](network.md#networkclusterkubeapi_vip) directly, so where the VM provider's network is an overlay the metal side cannot see (Proxmox SDN, OpenStack), the VIP must be made reachable from the metal L2 with a floating IP routed there or exit-node routing. The discovery service is reached on the internet (TCP 443); on a network with no direct egress, every node must get its proxy settings through `machine.env`, for example via [`talos.config_patches`](#talosconfig_patches), so the discovery connection traverses the proxy:

```yaml
talos:
  config_patches:
    - |
      machine:
        env:
          HTTPS_PROXY: http://proxy.example.edu:3128
```

The overlay is off unless you enable it here, and upgrading to a new release never turns it on on an existing cluster: converge applies it only after you set it, so nodes keep their plain routing until you opt in. The load refuses to leave it off — `false` or unset — while a [`metal`](metal.md) group's [`network`](metal.md#metalgroupnetwork) — or a single server's override of it — differs from [`network.cluster`](network.md#networkcluster), because the overlay is what carries their pod traffic to the rest of the cluster.

## `kubernetes`

### `kubernetes.version`

Required · `vMAJOR.MINOR.PATCH`

Kubernetes release to run; use the canonical `vMAJOR.MINOR.PATCH` form. A missing `v` prefix is normalized to the canonical form during load, so an unprefixed pin is never compared verbatim against the running cluster's version or rendered into a component image tag that lacks the leading `v`. Upgrade one minor at a time; converge steps through skipped minors itself with `talosctl upgrade-k8s`. A version older than what the cluster runs is refused. The pin must also be inside the range of Kubernetes minors the pinned [`talos.version`](#talosversion) supports (the [support matrix](https://docs.siderolabs.com/talos/latest/getting-started/support-matrix)); a pairing outside it is refused when the configuration loads. When bumping Talos and Kubernetes together, converge upgrades Talos first.

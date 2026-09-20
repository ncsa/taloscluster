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

Extra YAML files, named relative to the cluster directory, that are merged into `cluster.yaml` before it is validated. [`secrets.yaml`](../configuration.md#secretsyaml) is always merged first, ahead of this list, whenever it exists. They carry the same keys as `cluster.yaml`, so where a value lives is your choice: the schema, the error messages and the result are the same either way. Mappings merge key by key; a value set in two files is refused, naming the key and both files, rather than one file quietly winning. An included file may not include further files, may not be listed twice, and the paths stay inside the cluster directory (symlinks included). Once the files are merged there is one tree and one schema, so an unknown or misplaced key deeper inside a section is reported against `cluster.yaml` whichever file supplied it; only an included file's top-level keys are reported against that file. The motivating case is a long list that would drown the main file, such as the bare-metal machines:

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

Talos release to run; use the canonical `vMAJOR.MINOR.PATCH` form. A missing `v` prefix is normalized to the canonical form during load, and prerelease/build suffixes are accepted, but upstream lookup and upgrade behavior is designed around release versions. Must be v1.13.0 or newer because the generated machine configuration uses multi-document network kinds that older releases reject. Bumping it builds a new boot image from factory.talos.dev and rolls the upgrade over existing nodes on the next converge. Nothing auto-upgrades.

### `talos.extensions`

Optional · list of strings · default empty

Extra Talos system extensions merged with the QEMU guest agent, Tailscale when enabled, and pool-level `extensions`. An explicit `siderolabs/tailscale` entry keeps that extension even without a `tailscale` section. Proxmox installs the resolved image on first boot; OpenStack initially boots the shared volume image, so a node converges onto those extensions through a Talos upgrade rather than at first boot. Extensions activate during installation or upgrade, not merely when a machine configuration is applied. Converge detects an extension-only change by comparing a node's running schematic (the Image Factory's `schematic` extension reported by `talosctl get extensions`) against cluster.yaml, and after a bootstrap or scale-up asks any node that came up short of its configured extensions to reinstall, so adding or removing an extension reliably takes effect.

### `talos.config_patches`

Optional · list of YAML documents as strings · default empty

Freeform machine-config patches applied to every node. Pool-level `config_patches` are applied after these, so a pool patch wins on conflict.

### `talos.kubespan`

Optional · boolean · default `true`

Enables Talos KubeSpan on every node: the machine configuration turns the WireGuard overlay on and sizes its MTU to the node's layer-2 network MTU minus the 80 bytes of WireGuard overhead, so overlay traffic fragments at the same point the underlying network does. When [`network.external`](network.md#networkexternal) is configured, its `anchor_cidr` and `cidr` are excluded from KubeSpan endpoint discovery, so nodes never advertise or pick an external address as a peer endpoint. Set it to `false` to emit no KubeSpan settings and leave the generated configuration without a kubespan section.

## `kubernetes`

### `kubernetes.version`

Required · `vMAJOR.MINOR.PATCH`

Kubernetes release to run; use the canonical `vMAJOR.MINOR.PATCH` form. A missing `v` prefix is normalized to the canonical form during load, so an unprefixed pin is never compared verbatim against the running cluster's version or rendered into a component image tag that lacks the leading `v`. Upgrade one minor at a time; converge steps through skipped minors itself with `talosctl upgrade-k8s`. A version older than what the cluster runs is refused. When bumping Talos and Kubernetes together, converge upgrades Talos first.

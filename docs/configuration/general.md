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

kubernetes:
  version: v1.36.1
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

Talos release to run; use the canonical `vMAJOR.MINOR.PATCH` form. The loader also accepts a missing `v` prefix and prerelease/build suffixes, but upstream lookup and upgrade behavior is designed around release versions. Must be v1.13.0 or newer because the generated machine configuration uses multi-document network kinds that older releases reject. Bumping it builds a new boot image from factory.talos.dev and rolls the upgrade over existing nodes on the next converge. Nothing auto-upgrades.

### `talos.extensions`

Optional · list of strings · default empty

Extra Talos system extensions merged with the QEMU guest agent, Tailscale when enabled, and pool-level `extensions`. An explicit `siderolabs/tailscale` entry keeps that extension even without a `tailscale` section. Proxmox installs the resolved image on first boot; OpenStack initially boots the shared volume image and needs a Talos upgrade after bootstrap for a different set. Extensions activate during installation or upgrade, not merely when a machine configuration is applied. Converge compares the node’s version and machine-config installer reference rather than inspecting installed extensions, so an extension-only edit is not a guarantee that an upgrade occurs.

### `talos.config_patches`

Optional · list of YAML documents as strings · default empty

Freeform machine-config patches applied to every node. Pool-level `config_patches` are applied after these, so a pool patch wins on conflict.

## `kubernetes`

### `kubernetes.version`

Required · `vMAJOR.MINOR.PATCH`

Kubernetes release to run. Upgrade one minor at a time; converge steps through skipped minors itself with `talosctl upgrade-k8s`. A version older than what the cluster runs is refused. When bumping Talos and Kubernetes together, converge upgrades Talos first.

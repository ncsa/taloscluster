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

The cluster name. Lowercase letters, digits and internal hyphens only. It prefixes every hostname (`<name>-controlplane-01`, `<name>-<pool>-01`), the boot image, the Proxmox resource pool and, by default, the managed SDN zone id. Together with the longest pool name it must keep hostnames under 63 characters.

## `tags`

Optional · mapping of label to value

Extra Kubernetes node labels applied to every node through Talos `machine.nodeLabels`. Keys and values are stringified. Every node always gets `ncsa/role`, `ncsa/pool` and `ncsa/project` (the provider project or pool name with spaces replaced by `_`), and a tag here may override those defaults. Per-pool `tags` win over cluster-wide tags on the same key.

## `talos`

### `talos.version`

Required · `vMAJOR.MINOR.PATCH`

Talos release to run. Must be v1.13.0 or newer because the generated machine configuration uses multi-document network kinds that older releases reject. Bumping it builds a new boot image from factory.talos.dev and rolls the upgrade over existing nodes on the next converge. Nothing auto-upgrades.

### `talos.extensions`

Optional · list of strings · default empty

Extra Talos system extensions added to every node's installer image on top of the base set (`siderolabs/tailscale` and `siderolabs/qemu-guest-agent`). They take effect on the node's first upgrade pass, which converge handles. Pool-level `extensions` are merged in as well.

### `talos.config_patches`

Optional · list of YAML documents as strings · default empty

Freeform machine-config patches applied to every node. Pool-level `config_patches` are applied after these, so a pool patch wins on conflict.

## `kubernetes`

### `kubernetes.version`

Required · `vMAJOR.MINOR.PATCH`

Kubernetes release to run. Upgrade one minor at a time; converge steps through skipped minors itself with `talosctl upgrade-k8s`. A version older than what the cluster runs is refused. When bumping Talos and Kubernetes together, converge upgrades Talos first.

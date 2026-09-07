# Node pools

Back to the [configuration index](../configuration.md).

`controlplane` is a single pool. `workers` is a mapping of pool name to pool. Both take the same keys.

```yaml
controlplane:
  count: 3
  cores: 4        # Proxmox
  memory: 8       # Proxmox, GB
  disk: 40

workers:
  worker:
    count: 3
    flavor: gp.xlarge   # OpenStack
    disk: 100
  gpu:
    count: 2
    cores: 16
    memory: 64
    disk: 200
    node: pve3
    extensions:
      - siderolabs/nonfree-kmod-nvidia
      - siderolabs/nvidia-container-toolkit
    config_patches:
      - |
        machine:
          kernel:
            modules:
              - name: nvidia
    tags:
      workload: gpu
```

## `controlplane`

Required · pool

Hostnames are `<name>-controlplane-NN`. `count` must be at least 1. An even count warns that etcd needs a majority, and a count of 1 warns that there is no HA.

## `workers`

Optional · mapping of pool name to pool

Hostnames are `<name>-<pool>-NN`. A pool name must be a valid hostname component (lowercase letters, digits, internal hyphens); `controlplane` is reserved. Raise a `count` to add nodes, lower it to drain and remove the highest-numbered ones. On a managed Proxmox SDN each pool owns a static address block in file order, so reordering or removing a pool renumbers the pools after it, and converge refuses to renumber a running node.

## Pool keys

### `count`

Required · integer

Number of machines. Zero or more for a worker pool, at least 1 for `controlplane`.

### `disk`

Required · integer, GB

Boot volume size. On Proxmox it may only grow: the disk is resized online and Talos extends its `EPHEMERAL` partition on the next reboot. Shrinking is refused.

### `flavor`

Required on OpenStack · string

OpenStack flavor for the pool's servers.

### `cores`

Required on Proxmox · integer greater than zero

Virtual CPU count. Changed in place and applied on the next VM restart; `converge --reboot` performs the restart one node at a time.

### `memory`

Required on Proxmox · integer, GB, greater than zero

VM memory. Same restart semantics as `cores`.

### `node`

Optional, Proxmox only · string

Pin every machine of this pool to one Proxmox node. Must be listed in `proxmox.nodes` when that is set. Without it, placement spreads the pool across the online nodes.

### `extensions`

Optional · list of strings

Additional Talos extensions for this pool only, merged with the cluster-wide and base sets. Each distinct resolved set produces one installer image.

### `config_patches`

Optional · list of YAML documents as strings

Machine-config patches for this pool, applied after the cluster-wide patches.

### `tags`

Optional · mapping of label to value

Extra node labels for this pool. They override cluster-wide `tags` on the same key.

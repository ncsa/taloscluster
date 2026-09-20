# Metal

Back to the [configuration index](../configuration.md).

A `metal:` section brings bare-metal machines into the cluster. It may sit beside the one [OpenStack](openstack.md) or [Proxmox](proxmox.md) section — VMs and bare metal sharing one cluster — or stand alone when every machine is bare metal; at most one VM provider may be set, with or without `metal`.

## `metal`

Optional · mapping of group names to groups

Each key names a group of bare-metal machines and maps to that group's settings, which must be a mapping; group names follow the [pool name](pools.md#workers) rules.

```yaml
metal:
  worker: {}
```

Groups are carried through as written — the schema and the commands that join the machines come with the metal provider.

On a cluster with no VM provider, the [pools](pools.md) carry only `count` and `disk`: neither VM provider's sizing keys apply, and the [`controlplane` pool](pools.md#controlplane) is still required.

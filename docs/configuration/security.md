# Security allowlists

Back to the [configuration index](../configuration.md).

`security` holds named ingress rules. Each rule is one tcp port plus the source CIDRs allowed to reach it. The same rules become the OpenStack security group, the Proxmox per-VM firewall, and the Talos host firewall on every node. Rule names and the labels under them are free-form; the labels only document where an address comes from. Removing a label removes that allowance on the next converge; another rule for the same port or the always-allowed cluster traffic may still permit the source.

```yaml
security:
  kubernetes:                       # tcp/6443
    office vpn: 198.51.100.0/24
    tailscale: 100.64.0.0/10
  talos:                            # tcp/50000
    office vpn: 198.51.100.0/24
  https:                            # restrict tcp/443; omit to leave it open
    hosts:
      office vpn: 198.51.100.0/24
  metrics:                          # any other port needs `port`
    port: 9100
    hosts:
      office vpn: 198.51.100.0/24
```

## Rule shapes

**Short form**: the rule maps labels straight to CIDRs and uses the rule name's default port. This is what every pre-0.5 `cluster.yaml` uses for `kubernetes` and `talos`.

**Long form**: the rule has a `hosts` mapping and an optional `port`. Use it for any port without a default.

## Default ports

| Rule name | Port | Open when the rule is absent |
| --- | --- | --- |
| `kubernetes` | 6443 | no |
| `talos` | 50000 | no |
| `http` | 80 | yes |
| `https` | 443 | yes |

Any other rule name requires an explicit `port` between 1 and 65535. `http` and `https` may not change their port; use a differently named rule for another port.

## Open-by-default ports

Ports 80 and 443 accept traffic from every source until some rule claims that port, either an `http`/`https` rule or another rule with `port: 80` or `port: 443`. Claiming a port with an empty `hosts` map removes its default public allowance. Other rules for the same port and the always-allowed traffic still apply.

## Always allowed

The Talos host firewall allows TCP and UDP from `network.cluster.cidr`, from every [`metal`](metal.md) group's own L2, and from a server's own L2 when its entry overrides the group's `network`, plus DHCP replies on UDP/68 and UDP/41641 when the `tailscale` section is present. When the cluster's nodes sit on more than one L2, KubeSpan's WireGuard port UDP/51820 is also opened from the other L2s, whose handshakes the intra-cluster rules would otherwise drop. Talos also has built-in allowances for loopback, established connections, ICMP, and pod/service traffic. The Proxmox per-VM firewall permits ICMP and the same intra-cluster TCP/UDP from those L2s; OpenStack permits ICMP and TCP/UDP between members of the cluster security group and, by CIDR, from the metal L2s — a metal machine is not a security-group member. Metal nodes run the same Talos firewall keyed on their own L2 — the group's, or the one a server override replaces it with. Provider firewalls do not add the same explicit UDP/41641 allowance as the Talos firewall, so direct Tailscale connectivity also depends on the surrounding network. Ports no rule mentions, such as tcp/22, are left to you on Proxmox and stay closed by the Talos firewall's default deny.

## Proxmox firewall enablement

Enable the datacenter firewall yourself. taloscluster configures per-VM firewall options and sets `firewall=1` on NICs it creates, but refuses to run while the datacenter switch is off — the per-VM deny-in policy and the allowlist rules would never take effect — and warns if an existing NIC lacks that flag.

## CIDR format

Every source must be an IPv4 network written with its network address (`198.51.100.0/24`). A single host is `/32`. A bare address is accepted and read as the `/32` network the firewalls store it as, so `198.51.100.7` and `198.51.100.7/32` name the same allowance.

Writing a rule's `0.0.0.0/0` is equivalent to omitting it: on OpenStack the wildcard prefix is reconciled as the rule without a `remote_ip_prefix`, so a rule scoped to `0.0.0.0/0` and the open-by-default allowance for the same port are the same rule, not two competing ones.

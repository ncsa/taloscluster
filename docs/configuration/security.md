# Security allowlists

Back to the [configuration index](../configuration.md).

`security` holds named ingress rules. Each rule is one tcp port plus the source CIDRs allowed to reach it. The same rules become the OpenStack security group, the Proxmox per-VM firewall, and the Talos host firewall on every node. Rule names and the labels under them are free-form; the labels only document where an address comes from. Removing a label revokes that source on the next converge.

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

Ports 80 and 443 accept traffic from every source until some rule claims that port, either an `http`/`https` rule or another rule with `port: 80` or `port: 443`. Claiming a port with an empty `hosts` map closes it to everyone.

## Always allowed

The Talos host firewall accepts traffic from `network.cidr`, DHCP replies and tailscale's direct-connection port. The Proxmox per-VM firewall also accepts ICMP and intra-cluster traffic. Ports no rule mentions, such as tcp/22, are left to you on Proxmox and stay closed by the Talos firewall's default deny.

## CIDR format

Every source must be an IPv4 network written with its network address (`198.51.100.0/24`). A single host is `/32`.

# Tailscale

Back to the [configuration index](../configuration.md).

Every boot image carries the tailscale extension. Whether the installed system keeps it, and whether it authenticates, is decided by these two sections.

## cluster.yaml

```yaml
tailscale:
  login_server: https://headscale.example.edu
```

### `tailscale`

Optional · mapping, may be empty

The presence of this section, even as `tailscale: {}`, keeps the tailscale extension in the installer image. Without it the installer extension set omits Tailscale unless you explicitly add `siderolabs/tailscale` through `talos.extensions` or a pool's `extensions`. Management talks to the first control plane by its MagicDNS name only when the section also carries an [`auth_key`](#tailscaleauth_key): a keyless section leaves the extension idle, so its hostnames never resolve and management falls back to the node's real address, exactly as in a cluster without the section. Adding only the extension does not enable that address selection either.

### `tailscale.login_server`

Optional in the loader · URL · default unset

Set the Tailscale control-server URL explicitly when supplying an auth key, for example your Headscale server. When set, the generator emits `--login-server=<value>`. When omitted, no `--login-server` argument is emitted and the public Tailscale control plane is selected by default.

## secrets.yaml

```yaml
tailscale:
  auth_key: "CHANGE-ME"
```

### `tailscale.auth_key`

Optional · string

A reusable, ideally ephemeral, pre-auth key every node registers with. Omit it (or leave it `null`) to leave the extension idle: the nodes still boot, but they never join the tailnet, so management reaches the first control plane on its real address instead of its MagicDNS name. Use a valid key when registering new or recreated nodes; an ephemeral node setting does not make an expired or single-use key reusable. A non-string, empty, or still-scaffolded `CHANGE-ME` value is refused at secrets load time. The value is redacted from the machine-config diff that `plan` prints.

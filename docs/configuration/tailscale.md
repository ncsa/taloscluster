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

The presence of this section, even as `tailscale: {}`, keeps the tailscale extension in the installer image. Without it the installer extension set omits Tailscale unless you explicitly add `siderolabs/tailscale` through `talos.extensions` or a pool's `extensions`. The section also selects Tailscale hostnames for management; adding only the extension does not enable that address selection.

### `tailscale.login_server`

Optional in the loader · URL · default unset

Set the Tailscale control-server URL explicitly when supplying an auth key, for example your Headscale server. The current generator always emits `--login-server=<value>` and emits `--login-server=None` when this key is omitted; omission does not reliably select the public Tailscale control plane.

## secrets.yaml

```yaml
tailscale:
  auth_key: "CHANGE-ME"
```

### `tailscale.auth_key`

Optional · string

A reusable, ideally ephemeral, pre-auth key every node registers with. Omit it to leave the extension idle. Use a valid key when registering new or recreated nodes; an ephemeral node setting does not make an expired or single-use key reusable. The value is redacted from the machine-config diff that `plan` prints.

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

The presence of this section, even as `tailscale: {}`, keeps the tailscale extension in the installer image. Without it the installed system drops the extension, so nodes run no dormant service.

### `tailscale.login_server`

Optional · URL

A Headscale or self-hosted control server. Omit to use the public Tailscale control plane.

## secrets.yaml

```yaml
tailscale:
  auth_key: "CHANGE-ME"
```

### `tailscale.auth_key`

Optional · string

A reusable, ideally ephemeral, pre-auth key every node registers with. Omit it to leave the extension idle. Replace it before it expires when it is ephemeral. The value is redacted from the machine-config diff that `plan` prints.

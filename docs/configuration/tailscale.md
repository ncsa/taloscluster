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

The presence of this section, even as `tailscale: {}`, keeps the tailscale extension in the installer image; a `tailscale:` key left without a value — a comment-only block — counts as absent, as an explicit null does everywhere in the loader. Without it the installer extension set omits Tailscale unless you explicitly add `siderolabs/tailscale` through `talos.extensions` or a pool's `extensions`. Management talks to the first control plane by its MagicDNS name only when the section also carries an [`auth_key`](#tailscaleauth_key): a keyless section leaves the extension idle, so its hostnames never resolve and management falls back to the node's real address, exactly as in a cluster without the section. Adding only the extension does not enable that address selection either.

Decide the section and its `auth_key` before the first converge: switching either on a cluster that already runs is refused. Adding a keyed section, or removing one whose nodes registered, changes every node's schematic, and the reinstall that follows would deadlock the rollout — the upgraded nodes have not yet joined (or have just left) the tailnet the rollout is reached through. Adding the key to a keyless section, or removing it from a keyed one, reinstalls nothing but moves management between the MagicDNS name and the real address mid-life: forward, the name does not resolve until the nodes register under the new key, and back, discovery keeps reporting the tailnet addresses the nodes are about to lose. Restore the previous settings, or destroy the cluster and converge it fresh with the new ones. A keyless section can still be added on a live cluster, and one whose nodes never registered can still be removed: the extension merely starts idling (or is dropped) and management stays on the real addresses. Removing a keyless section is still refused while discovery reports a tailnet address — residue left by a key that was removed on a live cluster under an earlier release, which the nodes keep until they are reinstalled.

### `tailscale.login_server`

Optional in the loader · https:// URL · default unset

Set the Tailscale control-server URL explicitly when supplying an auth key, for example your Headscale server. When set, the generator emits `--login-server=<value>`. When omitted, no `--login-server` argument is emitted and the public Tailscale control plane is selected by default. Only `https://` URLs are accepted — the pre-auth key travels to the login server during registration, so a plain-http one would send it in the clear, and the loader refuses the configuration.

## secrets.yaml

```yaml
tailscale:
  auth_key: "CHANGE-ME"
```

### `tailscale.auth_key`

Optional · string

A reusable, ideally ephemeral, pre-auth key every node registers with. Omit it (or leave it `null`) to leave the extension idle: the nodes still boot, but they never join the tailnet, so management reaches the first control plane on its real address instead of its MagicDNS name. Use a valid key when registering new or recreated nodes; an ephemeral node setting does not make an expired or single-use key reusable. A non-string, empty, or still-scaffolded `CHANGE-ME` value is refused when a command needs the key rather than letting every node fail to register. The value is redacted from the machine-config diff that `plan` prints.

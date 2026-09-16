# Rancher plugin

Back to the [configuration index](../configuration.md).

The rancher plugin imports the cluster into a Rancher server, installs the cluster agent, and reconciles the members listed here. It is skipped, and shown as `not configured` by `taloscluster plugin list`, unless `cluster.yaml` has a `rancher` section and `secrets.yaml` has both `url` and `token`. See [Plugins](../concepts/plugins.md#how-plugins-run) for what converge, destroy and check do.

During converge's validate phase, before any core change, the plugin refuses a malformed or contradictory `rancher:` section in either file: a non-mapping section, a key the plugin does not understand, `admins`/`users` that are not lists of usernames, a member under both tiers, or `url`/`token` values that are not non-empty strings (including null or empty) stops the run while the cluster is still untouched. Validation runs whether or not the plugin is active, so a supplied-but-malformed `rancher:` section is rejected even though it would otherwise be silently discarded by activation; an entirely absent section is a no-op. On top of the literal overlap check, converge and check also refuse two netids that resolve to the same Rancher principal across different tiers (for example `alice` and `alice@example.com`, whose email suffix is stripped during resolution) before changing any binding, since that membership would flap between the two roles on alternating runs.

## cluster.yaml

```yaml
rancher:
  admins: [alice, bob]
  users: [carol]
```

### `rancher.admins`

Optional · list of usernames · default empty

Members granted the Rancher `cluster-owner` role. Usernames are resolved through Rancher's configured auth providers on an exact id match only; a short or misspelled netid cannot be resolved and is skipped with a warning. A name listed under both `admins` and `users` is ambiguous and refused during converge's validate phase, before any core change; so is a netid alongside an alias in the other tier that resolves to the same principal (for example `alice` in `admins` and `alice@example.com` in `users`), which is refused by converge and check before any binding changes. Removing a name removes stale individual user bindings on the next converge. The creator-owner binding and group bindings are preserved, so access through those bindings remains.

### `rancher.users`

Optional · list of usernames · default empty

Members granted the `cluster-member` role. Same resolution and removal behavior as `admins`.

## secrets.yaml

```yaml
rancher:
  url: https://rancher.example.edu
  token: token-xxxxx:yyyyyyyyyyyy
```

### `rancher.url`

Required · URL

The Rancher server. TLS verification is on.

### `rancher.token`

Required · string

A Rancher API bearer token with permission to create clusters and manage their role bindings.

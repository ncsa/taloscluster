# Rancher plugin

Back to the [configuration index](../configuration.md).

The rancher plugin imports the cluster into a Rancher server, installs the cluster agent, and reconciles the members listed here. It is skipped, and shown as `not configured` by `taloscluster plugin list`, unless `cluster.yaml` has a `rancher` section and `secrets.yaml` has both `url` and `token`. See `plugins/rancher/README.md` for what converge, destroy and check do.

## cluster.yaml

```yaml
rancher:
  admins: [alice, bob]
  users: [carol]
```

### `rancher.admins`

Optional · list of usernames · default empty

Members granted the Rancher `cluster-owner` role. Usernames are resolved through Rancher's configured auth providers; one that cannot be resolved is skipped with a warning. Removing a name revokes that user's binding on the next converge.

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

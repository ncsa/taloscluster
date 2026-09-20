# OpenStack provider

Back to the [configuration index](../configuration.md).

Pools on OpenStack size their servers with `flavor` (see [Pools](pools.md)). Converge creates a tenant network from `network.cluster.cidr`, a router on `external_net`, a security group from `security`, and separate reserved ports and floating IPs for the API and ingress. The provider connects in the configured `region` (default `RegionOne`). The session, the reported status/`env` output, and the region ArgoCD emits into the cluster-apps values all follow it.

## cluster.yaml

```yaml
openstack:
  url: https://openstack.example.edu:5000/v3/
  availability_zone: nova
  external_net: ext-net
  region: RegionOne
```

### `openstack.url`

Required · URL

Keystone identity endpoint.

### `openstack.availability_zone`

Required · string

Availability zone used when creating servers. Servers are create-only: changing it (or a pool's `flavor` or `disk`) does not resize existing servers, so converge refuses such a change with recreation guidance and `plan` reports it instead of silently ignoring it.

### `openstack.external_net`

Required · string

Name of the external (provider) network the router and floating IP attach to.

### `openstack.region`

Optional · string · default `RegionOne`

OpenStack region the session connects to and the one reported in status / `env` and emitted into the ArgoCD cluster-apps values. Omit it for the common `RegionOne` default.

## secrets.yaml

```yaml
openstack:
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
```

### `openstack.credential_id`

Required · string

Id of an OpenStack application credential, created with `openstack application credential create taloscluster`. The credential's project name becomes the `ncsa/project` node label. A null, non-string, empty, or still-scaffolded `CHANGE-ME` value is refused at secrets load time instead of failing later as an opaque 401.

### `openstack.credential_secret`

Required · string

The application credential secret. Must be a real, non-empty string that is not the scaffolded `CHANGE-ME` placeholder.

# OpenStack provider

Back to the [configuration index](../configuration.md).

Pools on OpenStack size their servers with `flavor` (see [Pools](pools.md)). Converge creates a tenant network from `network.cidr`, a router on `external_net`, a security group from `security`, and separate reserved ports and floating IPs for the API and ingress. The provider region is fixed to `RegionOne`; there is no core `region` setting.

## cluster.yaml

```yaml
openstack:
  url: https://openstack.example.edu:5000/v3/
  availability_zone: nova
  external_net: ext-net
```

### `openstack.url`

Required · URL

Keystone identity endpoint.

### `openstack.availability_zone`

Required · string

Availability zone used when creating servers. Changing it does not move existing servers.

### `openstack.external_net`

Required · string

Name of the external (provider) network the router and floating IP attach to.

## secrets.yaml

```yaml
openstack:
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
```

### `openstack.credential_id`

Required · string

Id of an OpenStack application credential, created with `openstack application credential create taloscluster`. The credential's project name becomes the `ncsa/project` node label.

### `openstack.credential_secret`

Required · string

The application credential secret.

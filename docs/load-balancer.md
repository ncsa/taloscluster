# Load balancing and ingress

This page shows how a running cluster exposes workloads to clients: which addresses core `taloscluster` allocates or configures for load balancing, and which MetalLB resources you (or your GitOps repository) must supply on top of them. It assumes the [ArgoCD plugin](configuration/argocd.md) registers the cluster and turns `metallb` and `ingress` on; the same MetalLB objects work if you install MetalLB by hand instead.

## The division of responsibility

Talos Linux provides no built-in load balancer, so the cluster uses **MetalLB** to hand addresses to `LoadBalancer` Services and a **Traefik** ingress controller to terminate `Ingress` routes on top of them. Two parties cooperate:

- **Core `taloscluster`** picks the addresses. On OpenStack it *allocates* a fixed ingress VIP plus a floating IP during the network phase. On Proxmox it *reserves* an `ingress_pool` range from your address plan and configures the routing/connection-marking that lets that network announce them — but it never creates a MetalLB address pool.
- **You or your GitOps repository** install MetalLB, create the `IPAddressPool` and `L2Advertisement` that announce the addresses core chose, point the ingress controller's `LoadBalancer` Service at those addresses, and write the `Ingress` objects that route traffic to your applications.

The [ArgoCD plugin](configuration/argocd.md) bridges the two: when you set `argocd.metallb.enabled: true` and `argocd.ingress.enabled: true`, the plugin renders the provider's addresses into the infra chart's `metallb.addresses` and the ingress controller's `publicIP`/`privateIP` values. It hands the chart the addresses, but the `IPAddressPool`, `L2Advertisement` and `Service`/`Ingress` objects are downstream resources the chart (or you) must realize.

## OpenStack

Converge creates the tenant network from `network.cluster.cidr`, a router and security group, one port per machine, and two extra reserved ports: `<cluster>-kubeapi` and `<cluster>-ingress`. Each reserved port gets a fixed IP on the tenant network and a floating IP on `external_net`. The ingress fixed IP becomes the MetalLB address; the ingress floating IP is the single externally reachable address the ingress controller advertises. Worker ports carry the ingress VIP in their `allowed_address_pairs`, so MetalLB can announce it from any worker.

Converge reports its ingress allocation through the [ArgoCD plugin](configuration/argocd.md) cluster-apps values:

```yaml
metallb:
  enabled: true
  addresses:
    - 192.0.2.10/32     # the ingress VIP (fixed IP on the tenant network), as a /32
ingresscontroller:
  enabled: true
  class: traefik
  publicIP: "203.0.113.80"   # the floating IP clients use
  privateIP: "192.0.2.10"    # the fixed VIP MetalLB announces
```

You then supply the MetalLB pool and announcement that match that `/32`:

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: ingress
  namespace: metallb-system
spec:
  addresses:
    - 192.0.2.10/32
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: ingress
  namespace: metallb-system
spec:
  ipAddressPools:
    - ingress
```

Start the ingress controller as a `LoadBalancer` Service so MetalLB gives it the pool address, then route it with an `Ingress`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: traefik
  namespace: ingress
spec:
  type: LoadBalancer
  loadBalancerIP: 192.0.2.10
  selector:
    app.kubernetes.io/name: traefik
  ports:
    - name: http
      port: 80
      targetPort: http
    - name: https
      port: 443
      targetPort: https
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app
  namespace: app
spec:
  ingressClassName: traefik
  rules:
    - host: app.example.edu
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app
                port:
                  number: 80
```

Clients reach `app.example.edu` through the floating IP `203.0.113.80`; MetalLB announces the private VIP `192.0.2.10` on the tenant network and Traefik forwards from there. The OpenStack security group carries your [`security`](configuration/security.md) allowlists on the ingress port too; ports 80 and 443 accept traffic from any source unless an [`http:`/`https:`](configuration/security.md) rule restricts them.

## Proxmox

There is no single ingress VIP. Beyond the two physical NICs (the private cluster link, and the optional `external` NIC), you reserve a range for MetalLB in `network.external.ingress_pool`. Core configures the routing and the connection-marking static pod so announcements on the external NIC work, but a MetalLB address pool is *yours* to create:

```yaml
proxmox:
  network:
    external:
      bridge: vmbr1

network:
  external:
    vlan: 100
    cidr: 203.0.113.0/25
    gateway: 203.0.113.1
    anchor_cidr: 169.254.32.0/20
    kubeapi_vip: 203.0.113.79
    ingress_pool: 203.0.113.75-203.0.113.78
```

The plugin passes the range through verbatim (MetalLB accepts `start-end`); with no single VIP, `publicIP`/`privateIP` stay empty:

```yaml
metallb:
  enabled: true
  addresses:
    - 203.0.113.75-203.0.113.78
ingresscontroller:
  enabled: true
  class: traefik
  publicIP: ""     # no single VIP on Proxmox
  privateIP: ""
```

Supply the matching pool and announcement:

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: ingress
  namespace: metallb-system
spec:
  addresses:
    - 203.0.113.75-203.0.113.78
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: ingress
  namespace: metallb-system
spec:
  ipAddressPools:
    - ingress
```

Traefik's `LoadBalancer` Service picks an address from the range (or you pin it with `loadBalancerIP`), and an `Ingress` as in the [OpenStack example](#openstack) routes to your app. On the `external` NIC, ports 80 and 443 are accepted from any source unless you add an [`http:`/`https:`](configuration/security.md) rule, so the pool is already reachable from the routed subnet.

## Which address is which

| Provider | Core allocates / configures | What MetalLB advertises | What clients use |
| --- | --- | --- | --- |
| OpenStack | fixed ingress VIP on the tenant network + a floating IP on `external_net` | the fixed VIP (`192.0.2.10/32`) | the floating IP (`203.0.113.80`) |
| Proxmox | the `ingress_pool` range in your address plan + routing/connection-marking | any address in the range (`203.0.113.75-203.0.113.78`) | an address from the pool |

On OpenStack the floating IP is the stable public entry point and the VIP is movable between workers behind it. On Proxmox the announced address *is* the public address — there is no NAT — so pick pool addresses that are already routable and reserved for your cluster.
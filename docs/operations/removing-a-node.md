# Removing a compute node

`ansible/07-remove-compute-node.yml` removes one compute node from an
already-running cluster: cordon, drain, delete the node object, destroy and
undefine the libvirt domain, remove its DHCP reservation, and rebuild the
haproxy backend list.

## Usage

```bash
# Required: the exact node name as shown by `oc get nodes`. No default,
# no "remove the last one".
./ansible/07-remove-compute-node.yml -e remove_node_name=compute-2

# --check reports exactly what would be drained and destroyed, changes nothing:
./ansible/07-remove-compute-node.yml -e remove_node_name=compute-2 --check
```

## Scope: compute nodes only

**Refuses outright on any node carrying a master/control-plane label** -
checked via the node's actual labels, not its name, since this cluster has
a worker registered as `localhost.localdomain` (see
[Adding a node](adding-a-node.md) for how that happens) - name-based
assumptions don't hold here. This refusal is **not** overridable by
`remove_node_force`. Control-plane removal means etcd member management and
quorum risk; on a 3-node control plane that's a footgun not worth
automating, so it's simply not something this playbook will do.

It also refuses on a node name that doesn't match the `compute-<id>` scheme
this repo uses (which is exactly what an orphaned/irregular node looks
like) - its libvirt domain can't be reliably derived from a name like
`localhost.localdomain`. Clean those up manually: confirm which VM it
actually is, then `oc delete node`, `virsh destroy`, `virsh undefine`, and
remove its DHCP reservation by hand.

## Safety

- Refuses if removal would drop the cluster below
  `remove_node_min_remaining_workers` (default 1) workers - override with
  `-e remove_node_force=true`.
- Drain uses `--ignore-daemonsets --delete-emptydir-data` with a
  configurable `remove_node_drain_timeout` (default `300s`).
- **Storage is left in place by default.** `virsh undefine` runs without
  `--remove-all-storage` unless you explicitly set
  `-e remove_node_destroy_storage=true` - deleting a node's disk is the one
  step here that isn't reversible, so it isn't the default.
- The DHCP reservation is removed via
  `virsh net-update ... delete ip-dhcp-host ... --live --config`, using the
  exact `<host .../>` entry read back from the live network config, not a
  reconstructed one - avoids any drift between the deterministic
  name/MAC/IP formula and what's actually configured.
- The haproxy backend list is rebuilt from observed VM state (post-removal)
  and the load balancer only restarts if the rendered config actually
  changed (a `notify`-driven handler shared with
  [Adding a node](adding-a-node.md)) - briefly drops in-flight connections
  through 80/443/6443, expected and brief for a lab setup.

## Limits

Same as [Adding a node](adding-a-node.md): **IPv4 only** - a cluster with
`IPv6` in `ip_families` is refused outright rather than leaving a stale
IPv6 DHCP reservation behind.

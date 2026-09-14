# Adding a compute node

`ansible/06-add-compute-node.yml` adds one compute node to an already-running
cluster - or "adopts" one that already exists as a libvirt domain but was
never wired up correctly.

## This does not work: bumping `compute_count` and re-running create

It's tempting to just raise `compute_count` in `cluster.yml` and re-run
`02-create-cluster.yml`. **Don't** - two things break:

1. **The worker ignition embeds a short-lived bootstrap certificate.**
   `openshift_install_dir/worker.ign` is only valid for 24 hours after the
   initial install. Months later, a new node booting from it fails to fetch
   its real config.
2. **`create-network.yml` doesn't add DHCP reservations for the new nodes.**
   `create-vm.yml` happily creates the new VMs, but the libvirt network's
   DHCP host-reservation list is only built from a loop over
   `master_count`/`compute_count` inside `create-network.yml`, and nothing
   re-runs that incrementally. A VM with no reservation gets a dynamic
   lease and **no hostname**, so RHCOS falls back to `localhost.localdomain`.

This happened for real on this cluster: `compute_count` was raised from 2 to
4 and `02-create-cluster.yml` re-run. Both new VMs (`compute-2`,
`compute-3`) came up with no DHCP reservation, both registered as
`localhost.localdomain`, collided on that name in Kubernetes, and only one
of the two could actually join - the other never appeared in the cluster at
all. `06-add-compute-node.yml` exists specifically to do this correctly.

## Usage

```bash
# Add the next available compute node:
./ansible/06-add-compute-node.yml

# Target (or adopt) a specific id:
./ansible/06-add-compute-node.yml -e add_node_id=2

# Pre-flight only - reports the derived name/MAC/IP, changes nothing:
./ansible/06-add-compute-node.yml --check
```

Node identity (name, MAC, IPv4) is always **derived from observed state** -
existing libvirt domains plus existing DHCP reservations - never from
`compute_count`. That's a deliberate response to the incident above: a
stale or wrong `compute_count` is exactly what caused it.

## Adopt mode

If the target VM already exists as a libvirt domain (like `compute-2` and
`compute-3` above), the playbook **skips VM creation entirely** - no new
disk, no `virsh define` - but still adds the missing DHCP reservation,
rebuilds the haproxy backend list, and runs CSR approval / waits for Ready.
This is the same code path as a plain idempotent re-run: "the VM already
exists" is detected from `virsh dominfo`, not from a separate flag.

One wrinkle: if the orphaned node already registered under
`localhost.localdomain` (as `compute-3` did here), fixing its DHCP
reservation only fixes the hostname on its **next boot** - DHCP hostname is
assigned at boot time. Adopting that node means: reservation added → reboot
the VM (`virsh reboot <domain>`) → it rejoins under its correct name → the
stale `localhost.localdomain` node object needs deleting
(`oc delete node localhost.localdomain`) and its CSRs re-approving. The
playbook does not currently automate the reboot or the stale-object
cleanup - do those two steps manually after it reports the reservation is
in place.

A second wrinkle, specific to these orphaned nodes: without a reservation
they picked up a **dynamic** address from the DHCP pool (`.46`/`.47` on this
cluster), not the deterministic one the formula above derives (`.15`/`.16`
for those same ids). Adopting them changes their IP, not just their
hostname, once they reboot and pick up the new reservation - that's
expected and correct (it's what brings them in line with every other node's
addressing), but plan for a brief address change, not just a rename.

## Pre-flight

Refuses to start unless: `ClusterVersion` is `Available=True`, no
ClusterOperator is `Degraded`/`Available=False`, all existing nodes are
`Ready`, and the host has at least `add_node_min_free_disk_gb` (default 50)
GB free on `coreos_path` and enough free memory for `compute_memory_size`.
Override with `-e add_node_force=true` - prefer fixing the underlying issue
first.

## What happens, in order

1. Fresh worker pointer ignition is pulled from the running cluster
   (`oc extract -n openshift-machine-api secret/worker-user-data-managed`),
   **not** the install-time `worker.ign` - the pointer config carries the
   cluster's current CA and doesn't expire the way the install-time one
   does.
2. VM created (skipped in adopt mode).
3. DHCP reservation added via `virsh net-update ... --live --config` -
   `--live` is the important part; `--config` alone only updates the
   persistent XML and the running dnsmasq never learns the new host, which
   is exactly how the original incident happened. The playbook verifies the
   reservation actually appears in the live network config afterwards.
4. The haproxy backend list is rebuilt from observed VM state (not
   `compute_count`) and the load balancer service is restarted **only if
   the rendered config actually changed** (a `notify`-driven handler, not an
   unconditional restart). This briefly drops in-flight connections through
   80/443/6443 - expected and brief for a lab setup.
5. Client CSR, then serving CSR are polled for and approved as they appear
   (never a blanket sleep) until the node reports `Ready`.

## Limits

- **IPv4 only.** A cluster with `IPv6` in `ip_families` is refused outright
  rather than given a silently-incomplete IPv6 setup - the IPv6 side needs
  DUID-based DHCP reservations (see `network.xml.j2`), which this playbook
  doesn't implement yet.
- **Idempotent**, but only in the sense of "safe to re-run" - running it
  twice doesn't create a second VM, a duplicate DHCP reservation, or a
  duplicate haproxy backend.

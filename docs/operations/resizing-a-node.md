# Resizing a node

`ansible/08-resize-node.yml` changes CPU and/or memory for one or more
existing nodes (master or compute).

## Usage

```bash
# Memory only:
./ansible/08-resize-node.yml -e '{"resize_node_names": ["compute-0"], "resize_node_memory_size": 16384}'

# vCPUs only:
./ansible/08-resize-node.yml -e '{"resize_node_names": ["compute-0"], "resize_node_vcpu": 4}'

# Both, and more than one node - processed one at a time, never in parallel:
./ansible/08-resize-node.yml -e '{"resize_node_names": ["compute-0", "compute-1"], "resize_node_vcpu": 4, "resize_node_memory_size": 16384}'

# --check reports the plan, changes nothing:
./ansible/08-resize-node.yml -e '{"resize_node_names": ["compute-0"], "resize_node_vcpu": 4}' --check
```

`resize_node_memory_size` is interpreted in `resize_node_memory_unit`
(default `MiB`, matching `compute_memory_unit`/`master_memory_unit`
elsewhere in this repo).

## What happens, per node, in order

Serial - one node at a time, always waiting for the previous one to be back
and `Ready` before touching the next, whether you list one node or several:

1. Cordon, then drain (`--ignore-daemonsets --delete-emptydir-data`). The
   emptyDir flag is mandatory rather than a choice: `oc adm drain` refuses
   outright if any pod has an emptyDir volume, and OpenShift's own platform
   pods — prometheus, alertmanager, image-registry, metrics-server — all use
   them. Draining reschedules those pods onto another node and an emptyDir
   never follows its pod, so the data is discarded either way; omitting the
   flag blocks the drain rather than preserving anything.
2. `virsh shutdown` (graceful ACPI shutdown), polled until the domain
   actually reaches `shutdown` state.
3. Memory and/or vCPU count are changed on the offline domain via
   `virsh setmaxmem`/`setmem`/`setvcpus --config`. Growing sets the maximum
   before the current value; shrinking sets the current value before the
   maximum - in both directions, current never exceeds max at any single
   step, which is what `--config` on an offline domain requires.
4. `virsh start`, polled until running.
5. Polled until the node's `bootID` has **changed** *and* it reports `Ready`.
   Both halves are needed. Waiting on `Ready` alone is racy: for about the
   node-monitor grace period (~40s) after a node disappears, its object still
   advertises the `Ready=True` it had before shutting down. A naive poll
   therefore succeeds immediately, uncordons a node that has not actually
   come back, and lets the loop start draining the next one - briefly taking
   two nodes out at once. `bootID` is rewritten by the kernel on every boot,
   so a change in it cannot be stale.
6. Uncordon.

If any step between the cordon and the uncordon fails, a `rescue` uncordons
the node before re-raising the error. The run still fails loudly - but it
fails with the node able to take work again, rather than silently leaving the
cluster one worker short until someone notices and runs `oc adm uncordon` by
hand.

Each run reminds you to update the matching `cluster.yml` variable
(`master_vcpu`/`master_memory_size`/`master_memory_unit` or
`compute_vcpu`/`compute_memory_size`/`compute_memory_unit`) once you're
happy with the new size - `virsh setmaxmem`/`setmem`/`setvcpus` only ever
touch the live libvirt domain, never `cluster.yml` or the VM template, so
a future node add or recreate would otherwise use the old size again.

## Pre-flight

Same shape as [adding](adding-a-node.md)/[removing](removing-a-node.md) a
node: refuses to start unless `ClusterVersion` is `Available=True`, no
ClusterOperator is `Degraded`/`Available=False`, and all nodes are
currently `Ready` - you're about to take one more node offline, however
briefly, so starting from a known-healthy cluster matters more here than
anywhere else in this repo. Override with `-e resize_node_force=true`.

## Out of scope: disk resize

Deliberately not handled here. RHCOS grows its root partition to fill the
disk **on first boot only** - expanding the qcow2 afterwards doesn't yield
any usable space without manual intervention inside the guest. If you need
more storage on a node, attach a second disk and use the Local Storage
Operator instead; see [Disk management](../guides/disk-management.md)
(moved there by the docs restructure, PR2).

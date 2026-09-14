# Cluster maintenance

`ansible/09-maintenance.yml` does the host-side housekeeping that nothing
else in this repo does, and that RHCOS does not do for itself.

It never cordons, drains or reboots anything, so it is safe to run against a
live cluster at any time.

## Usage

```bash
# fstrim + orphaned CSR cleanup (the defaults):
./ansible/09-maintenance.yml

# Also prune unused container images on every node:
./ansible/09-maintenance.yml -e maintenance_prune_images=true

# Report current disk usage and what would run, change nothing:
./ansible/09-maintenance.yml --check
```

## What it does, and why each one exists

### 1. `fstrim` inside every running guest (default: on)

The libvirt domains are defined with `discard='unmap'`, so a trim inside the
guest propagates through to the host and punches holes in the backing qcow2.
**RHCOS does not enable `fstrim.timer`**, so on a cluster built by this repo
nothing has ever run it.

The effect is not subtle. On 2026-09-14 this cluster's seven qcow2 files had
each grown to 116–118G against a 120G virtual size, filling
`/var/lib/libvirt/images` to **93%** — while the guests were using only
around a quarter of that internally. A single pass took the host to **44%**
and returned roughly **360G**, with no downtime and no workload disruption.

Left unattended the files only ever grow, because a deleted file inside the
guest is still an allocated block on the host until something trims it. On a
host that also stores the NFS PV data, that eventually becomes a real outage
rather than a tidiness problem.

A node that is unreachable (mid-reboot, say) is reported and skipped rather
than failing the run — the other nodes are still worth trimming.

### 2. Prune unused container images (default: **off**)

Kubelet garbage-collects images only once a node crosses its
`image-gc-high-threshold`, 85% by default. A node can therefore sit
indefinitely at 82% holding tens of gigabytes of superseded release images,
never quite triggering cleanup but leaving no headroom for an upgrade that is
about to pull a whole new payload. One master here held **192 images / 79G**;
pruning took it to 61 images and freed 62G.

Off by default because it is the more intrusive of the two: `crictl rmi
--prune` removes every image not referenced by a running container, so
anything not currently running is re-pulled next time it is needed. That is
the right trade before an upgrade, and unnecessary noise most other days.

`crictl` returns non-zero when CRI-O is busy and a delete request times out,
even though the deletions it did make still took effect, so a non-zero exit
here is not treated as failure.

### 3. Delete Pending CSRs for nodes that no longer exist (default: on)

A node that is broken for a long time keeps requesting serving certificates.
This cluster had accumulated **96** Pending CSRs from a single node that
could never register. They are harmless in themselves, but they bury the
CSRs you actually need to see when adding a node, and make the two-round
approval in [adding a node](adding-a-node.md) much harder to follow.

Only Pending CSRs whose `system:node:<name>` subject names a node that is
**not** in the cluster are deleted. Approved CSRs are historical records the
controller cleans up on its own, and a Pending CSR for a node that *does*
exist may simply be waiting for approval right now — deleting either would
be wrong.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `maintenance_fstrim` | `true` | Run `fstrim -a` inside each running node |
| `maintenance_prune_images` | `false` | Run `crictl rmi --prune` on each node |
| `maintenance_clean_csrs` | `true` | Delete Pending CSRs for absent nodes |
| `maintenance_ssh_key` | `~/.ssh/id_rsa` | Key used to reach nodes as `core` |
| `maintenance_ssh_timeout` | `10` | Per-node SSH connect timeout, seconds |

## When to run it

- **Before any upgrade.** An upgrade pulls a full release payload onto every
  node and grows every qcow2; `05-upgrade-cluster.yml` refuses to start below
  `openshift_upgrade_min_free_disk_gb` and this is the cheapest way to get
  back above it.
- **After removing a node**, to return its freed blocks to the host.
- **On a schedule.** There is no timer for this yet — a systemd timer or cron
  entry calling this playbook weekly would remove the whole class of problem.
  Installing `qemu-guest-agent` on the nodes via MachineConfig would be the
  tidier long-term answer, since `virsh domfstrim` could then do the trim
  natively without SSH; the agent is not currently installed.

## Why it uses SSH

The tasks reach each node over SSH as `core` rather than going through
libvirt, because `virsh domfstrim` requires the QEMU guest agent and these
domains do not have it configured (`virsh domtime` reports "QEMU guest agent
is not configured"). SSH is how the trim was first performed by hand here,
and it needs no change to the guests.

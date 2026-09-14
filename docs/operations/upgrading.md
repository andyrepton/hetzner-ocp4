# Upgrading the cluster

`ansible/05-upgrade-cluster.yml` upgrades an already-running cluster in
place, driven by `oc adm upgrade` against the cluster's own update graph.

It does **not** touch libvirt, RHCOS images or ignition files, and does not
manage the Machine Config Operator directly — once triggered, the Cluster
Version Operator and MCO roll the new release out to the existing
control-plane and compute nodes in place. Nothing about the VM layer is
involved in an OpenShift minor/patch upgrade.

## Usage

```bash
# Pre-flight checks only, no changes made:
./ansible/05-upgrade-cluster.yml --check

# Actually upgrade:
./ansible/05-upgrade-cluster.yml
```

## Pre-flight checks

The playbook refuses to start unless all of the following hold:

- `ClusterVersion` reports `Available=True`.
- `ClusterVersion` reports `Progressing=False` (refuses to start a second
  upgrade on top of one already in flight).
- No `ClusterOperator` reports `Degraded=True` or `Available=False`.
- `etcd` specifically is not in the degraded list (checked explicitly, in
  addition to the general operator check above).
- All `Node` objects report `Ready`.
- At least `openshift_upgrade_min_free_disk_gb` (default `50`) GB free on
  `coreos_path` (default `/var/lib/libvirt/images`) on the KVM host — the
  most common real-world failure mode on this setup given 120G qcow2 roots
  per node plus a new RHCOS image being pulled during the upgrade.

If any check fails, the playbook stops and reports exactly what's wrong. You
can override with `-e openshift_upgrade_force=true`, but that is a deliberate
escape hatch, not a default — an upgrade started against an unhealthy
cluster can make recovery significantly harder.

Run with `--check` to execute only the pre-flight checks and see the report
without touching anything.

## Choosing a target version

Two modes, set via `openshift_upgrade_mode` in `cluster.yml`:

- `pinned` (default) — upgrades to `openshift_upgrade_target_version`
  (defaults to `openshift_version`, so bumping that one variable is usually
  enough).
- `latest` — upgrades to the latest recommended update on the cluster's
  current channel (`oc adm upgrade --to-latest=true`).

`openshift_upgrade_channel` is left empty by default, which leaves the
cluster on whatever channel it's already subscribed to. Set it explicitly
(e.g. `stable-4.19`) only when you deliberately want to move channels as
part of the upgrade.

`openshift_upgrade_allow_explicit_upgrade` and `openshift_upgrade_force` map
directly to `oc adm upgrade --allow-explicit-upgrade` / `--force`, for the
(uncommon) case of upgrading to a version outside the cluster's current
recommended graph.

## What happens after the upgrade completes

- ClusterOperators and Nodes are re-checked and a status summary is printed.
  A few operators settling for a couple of minutes after the CVO reports
  `Completed` is normal and not treated as a failure by this playbook; if it
  doesn't clear, investigate with `oc get co` / `oc get nodes`.
- **The local `oc`/`openshift-install`/`kubectl` binaries under
  `/usr/local/bin` are re-synced to match the version the cluster actually
  ended up on** (read back from `ClusterVersion.status.desired.version`,
  not guessed up front — this matters for `latest` mode). This is the
  repo-specific trap this playbook exists partly to avoid: those binaries
  are pinned to `openshift_version` at install time by
  `download-openshift-artifacts.yml`, and if nothing re-syncs them after an
  in-cluster upgrade they go stale, which then silently breaks certificate
  renewal (`ansible/renewal-certificate.yml`) and any future node addition
  that shells out to `oc`.
- Update `openshift_version` (and, when you next provision new nodes,
  `coreos_version`) in `cluster.yml` to match the version you upgraded to,
  so future runs of this repo stay consistent with the cluster's actual
  state. The playbook prints a reminder with the resolved version number.

## Why not a community collection?

`community.okd` / `redhat.openshift` (the maintained Ansible collections
for OpenShift) only cover object CRUD (auth, builds, image pruning,
templates, routes) — neither has any module for `ClusterVersion` or
`oc adm upgrade`. The only Ansible content that specifically targets
"upgrade an OpenShift cluster" targets the OCP 3.9/3.11 in-place-upgrade
model, which OCP4 replaced entirely with the in-cluster Cluster Version
Operator; none of it applies here. See
[`docs/decisions/001-day2-tooling.md`](../decisions/001-day2-tooling.md)
(Spike B) for the full research behind this.

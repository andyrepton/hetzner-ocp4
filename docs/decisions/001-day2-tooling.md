# ADR 001: Tooling decisions for day-2 playbooks

Status: accepted. Backs the day-2 operations work (certificate DNS-wait fix,
cluster upgrade playbook, node add/remove). Each spike was researched by a
separate agent in parallel; findings are summarized per spike below.

## Spike A — DNS propagation waiting

**Recommendation: replace the fixed `pause` with `community.dns.wait_for_txt`,
looped per record with `mode: subset` and a bounded `timeout` as a safety
net (not a floor).**

`ansible/roles/letsencrypt/tasks/main.yml` currently has a flat
`ansible.builtin.pause: seconds: 120` (not 300) gated on
`sample_com_challenge is changed`, applied regardless of DNS provider or how
fast it actually propagates.

- `challenge_data_dns` is a dict of `{record_name: [txt_value, ...]}`
  (built via `dict2items | subelements('value')` elsewhere in the same
  file). `wait_for_txt`'s `records` param wants `list of
  dict{name, values, mode}` — compatible in substance, needs a
  `dict2items | json_query('[].{name: key, values: value}')` transform.
  Trivial.
- `community.dns` is actively maintained (commits within the last day,
  4.1.1 released 2026-09-06) and **already an EE dependency**
  (`ee-requirements.yml`, pulled in for `hetzner_dns_record`), so this adds
  no new collection. It does add a new Python dependency: `dnspython >= 2.0`
  is required by `wait_for_txt` and is not currently in
  `ee-python-requirements.txt` (only `libvirt-python`, `jmespath` are).
- `wait_for_txt` queries all authoritative nameservers directly, supports
  `mode: subset` (only the challenge TXT values need be present — other TXT
  records on the same name are ignored, which matters since apex/wildcard
  names can carry unrelated TXT records), and has configurable
  timeout/backoff.
- `community.crypto.acme_certificate` has no built-in propagation wait; the
  module's own dns-01 example relies on the DNS provider module's own
  `wait: true` for provider-side confirmation, not public-resolver
  propagation. This repo's task names (`sample_com_challenge`,
  `challenge_data_dns`) are copied directly from that example, which has no
  equivalent of the current `pause` at all — an explicit wait step is
  necessary either way.
- Fallback (`dig`-based `until` loop): viable but strictly worse here —
  needs `bind-utils` added to `ee-bindep.txt` (not present), needs custom
  per-record authoritative-NS resolution, and reimplements what
  `wait_for_txt` already does. Only worth it if `community.dns` were
  rejected outright, which it isn't given it's already a pulled-in
  dependency.

Implementation: PR 1.

## Spike B — OpenShift upgrade automation

**Recommendation: no maintained collection covers `oc adm upgrade` /
ClusterVersion orchestration — write a thin wrapper playbook.**

- `community.okd` (openshift/community.okd) is actively maintained (v6.0.0,
  2026-08-17, ~880K downloads) but its module surface — `k8s`,
  `openshift_adm_groups_sync`, `openshift_adm_migrate_template_instances`,
  `openshift_adm_prune_*`, `openshift_auth`, `openshift_build`,
  `openshift_import_image`, `openshift_process`, `openshift_registry_info`,
  `openshift_route` — is entirely object CRUD / day-2 resource management.
  Nothing addresses `ClusterVersion` or update orchestration.
- `redhat.openshift` (the certified/Automation-Hub counterpart) shares the
  same codebase lineage and the same gap.
- Nothing else maintained turned up on Galaxy/GitHub. The only
  upgrade-flavored Ansible content found (`openshift-ansible`,
  `RedHatOfficial/ansible-redhat_openshift_utils`, `redhat-cop/casl-ansible`)
  targets the OCP 3.9/3.11 in-place-upgrade model, which OCP4 replaced
  entirely with the in-cluster Cluster Version Operator. None of it applies
  to a UPI/libvirt OCP4 cluster.
- This confirms the underlying premise: OpenShift 4's actual supported
  upgrade mechanism already **is** `oc adm upgrade` / patching the
  `ClusterVersion` CR directly — there's no separate operator layer for
  Ansible tooling to wrap. Polling is naturally `oc get clusterversion -o
  json` (or `kubernetes.core.k8s_info`) in an `until` loop.

Implementation: PR 3 (`ansible/05-upgrade-cluster.yml`), which also owns the
pre-flight health gates (ClusterOperators, nodes, etcd, disk space) that no
collection provides either.

## Spike C — UPI node lifecycle

**Recommendation: no purpose-built collection for this — build a thin
custom role using `oc extract` and `oc adm certificate approve` /
`oc get csr` shelled out via `command`.**

- `community.okd` has no CSR module: CSR approval is a subresource action
  (`POST .../certificatesigningrequests/{name}/approval`) that the generic
  `k8s` module can't address. A `oc_adm_csr` module existed only in the
  deprecated OCP 3.11-era `openshift-ansible` and was never carried into
  `community.okd`.
- What exists elsewhere (HPE's `hpe-solutions-openshift` worker-add
  playbooks, `sa-ne/openshift4-vmware-upi`, `sa-ne/openshift4-rhv-upi`,
  CentOS Infra's `ocp4-docs`) all reimplement the same manual-doc procedure
  as raw shell tasks — `oc get csr -o json | jq ... | xargs oc adm
  certificate approve` — rather than depending on a shared module. None
  treat "fetch fresh pointer ignition" as a reusable role either.
- **Correction to the plan's assumed command**: the non-expiring worker
  pointer ignition comes from `secret/worker-user-data-managed`, not
  `secret/worker-user-data`:
  ```
  oc extract -n openshift-machine-api secret/worker-user-data-managed --keys=userData --to=-
  ```
  Confirmed against current docs.redhat.com (4.15 and 4.21, "Adding
  compute machines to bare metal", §11.4.2.1). `worker-user-data` (no
  `-managed` suffix) is the Machine-API-managed copy used internally by
  cloud MachineSets, not the one documented for manual UPI node addition.
  The `-managed` secret's pointer ignition references
  `https://api-int.<cluster>.<domain>:22623/config/worker` with the
  cluster's *current* MCS CA — unlike the static `worker.ign` from the
  install directory, which pins the install-time CA and breaks after CA
  rotation (Red Hat KB: "Adding new nodes to UPI cluster fails after
  upgrading to OpenShift 4.6+").
- CSR approval is two rounds — client cert, then serving cert — with a
  **1-hour window** before an unapproved CSR auto-rotates and needs
  re-approving. The add-node playbook's polling loop needs to account for
  this.

Implementation: PR 4, blocked on Spike D's finding that `create-vm.yml` is
already reusable for a single host.

## Spike D — repo's own VM provisioning

**Bottom line: `create-vm.yml` needs no changes to add one node. The
network/haproxy side is not incremental and needs new logic before PR 4/5
can safely touch a live cluster.**

- `create-vm.yml` is already a clean single-host task file: it takes
  `vm_instance_name`, `vm_network`, `vm_ignition_file`, `vm_mac_address`,
  `vm_vcpu`, `vm_special_cpu`, `vm_memory_size`, `vm_memory_unit`,
  `vm_root_disk_size` as task-level vars, with no internal loop and no
  reference to `master_count`/`compute_count`. All looping and MAC
  derivation live one level up in `create.yml`. The compute MAC is a pure
  deterministic function: `52:54:00:{subnet[1]}:{subnet[2]}:{10 +
  master_count + item}` — directly reusable for a new index (e.g.
  `item = current compute_count`) without touching the loop.
  `create-vm.yml`/`vm.xml.j2` never receive an IP, only a MAC — IP
  assignment is entirely a network-side concern.
- No dnsmasq role exists. DHCP/DNS is static `<host mac= name= ip=/>`
  entries in the libvirt network XML (`network.xml.j2`), served by
  libvirt's embedded dnsmasq. All entries come from one `nodes` fact built
  entirely inside `create-network.yml` by rendering a hand-built YAML blob
  (`range(0, master_count)` / `range(0, compute_count)`) and parsing it
  with `from_yaml` — i.e. always rebuilt from the full host count, not
  incremental.
- haproxy config is the same shape: `create.yml` renders the *entire*
  `haproxy.conf.j2` from the same full `nodes` list into one string,
  written wholesale into a systemd `EnvironmentFile` consumed by a podman
  container. There is no per-backend granularity, and critically **no
  handler restarts the load balancer when that file's content changes** —
  confirmed against `openshift-4-cluster/handlers/main.yml` (NFS/firewalld
  handlers only).
- Concrete risk this creates for PR 4/5: redefining an already-active
  libvirt network updates only the persistent XML — the live dnsmasq
  won't necessarily pick up a new static DHCP host entry without a
  destroy/start or `virsh net-update --live`, which nothing in this repo
  does today. A naive "add one node" that reuses `create-network.yml`
  as-is risks either silently not taking effect or an unnecessary full
  network restart. Same shape of risk on the haproxy side, plus the
  missing restart handler.

Implementation: **PR 4 must add** (a) a `virsh net-update --live` (or
equivalent) step for the one new DHCP host entry instead of a full
redefine, and (b) a restart/reload handler for the load balancer, before
node add/remove can be considered safe against a live cluster. Track as
explicit sub-tasks inside PR 4/5 rather than a separate refactor PR, since
Spike D found no other change needed to `create-vm.yml` itself.

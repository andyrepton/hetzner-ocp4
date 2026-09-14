# Preparing the host

`ansible/01-prepare-host.yml` gets a bare Linux server ready to run the
cluster. It is idempotent and safe to re-run - worth doing after any large
package update, since it re-asserts configuration a distro upgrade may have
reset.

## What it does

1. **Installs and starts libvirt/KVM**, then verifies the `kvm` kernel module
   is actually loaded. On modern distributions the monolithic `libvirtd` is
   superseded by modular daemons (`virtqemud`, `virtnetworkd`, …) reached
   through socket activation, so `systemctl is-active libvirtd` reporting
   `inactive` is normal and not a fault - check `virtqemud` instead.
2. **Creates an SSH key for root** if one does not already exist, used to
   reach the cluster VMs as `core`.
3. **Enables IPv4 forwarding** in `/etc/systemd/network/10-mainif.network`,
   so traffic can route between the VMs and the host.
4. **Prefers IPv4 if the host's IPv6 route is dead** - see below.
5. **Reboots** if a new kernel was installed and the play is running
   remotely.

## Why it may rewrite `/etc/gai.conf`

A server can have a global IPv6 address and an IPv6 default route whose
gateway never answers. Nothing reports this as broken. `getaddrinfo()` still
returns AAAA records ahead of A per RFC 3484, so every outbound connection to
a dual-stack host tries IPv6 first and blocks until the socket times out
before falling back to IPv4. It presents as slowness, never as an error, and
it is consequently very hard to attribute.

Measured on the Hetzner host this repo was developed against, where
`mirror.openshift.com` publishes 4 A records and 8 AAAA records:

| | Result |
|---|---|
| Forced IPv4 (`curl -4`) | `HTTP 200` in **0.35s** |
| Forced IPv6 (`curl -6`) | no response, **34.9s** timeout |

Eight AAAA addresses at roughly 30 seconds each produced **240 seconds per
URL**. The effects were large and looked unrelated to each other:

| Operation | Before | After |
|---|---|---|
| Checking 8 artifact download URLs | ~32 min | seconds |
| Deleting 2 DNS records (cluster destroy) | 1446s | 2.9s |
| Whole `99-destroy-cluster.yml` run | 25m 48s | **20s** |

`prepare-host.yml` therefore checks for an IPv6 default route, pings its
gateway, and - only if the gateway does not answer - writes an
`/etc/gai.conf` that raises the precedence of IPv4-mapped addresses so A
records are returned first.

Two things worth knowing about that file:

- **It does not disable IPv6.** It only reorders resolver results. If the
  gateway starts answering, delete `/etc/gai.conf` (or set
  `prefer_ipv4_when_ipv6_gateway_dead: false`) and IPv6 is preferred again on
  the next resolution.
- **glibc replaces its entire built-in table** as soon as any `precedence`
  line is present, so the template restates the full RFC 3484 default table
  with one value changed, rather than adding a single line. A single-line
  `gai.conf` would silently discard every other default.

A host whose IPv6 gateway responds normally is left untouched.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `prefer_ipv4_when_ipv6_gateway_dead` | `true` | Write `/etc/gai.conf` when the IPv6 default route's gateway does not answer |
| `ssh_public_key_location` | `~/.ssh/id_rsa` | Key created for root and injected into the VMs |

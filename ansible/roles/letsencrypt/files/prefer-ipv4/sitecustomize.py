"""Make outbound connections from Ansible-invoked Python modules try IPv4
before IPv6.

Some hosts have a broken IPv6 default route (a gateway is configured but
never answers) while their interface still carries a global IPv6 address.
glibc's getaddrinfo() then still returns AAAA records for dual-stack API
endpoints (Cloudflare, Route53, Let's Encrypt, ...), and socket.create_connection()
tries whatever getaddrinfo() returns first - if that's the dead IPv6 route,
every DNS-provider/ACME task in the letsencrypt role hangs indefinitely with
no usable timeout, since the TCP handshake is silently black-holed rather
than actively refused.

This file is placed on PYTHONPATH for the letsencrypt role (see
le_prefer_ipv4 in defaults/main.yml) purely to be auto-imported by Python's
site machinery in every module process it covers. It does not disable IPv6:
it only reorders getaddrinfo() results so IPv4 addresses sort first, which
is enough for socket.create_connection() (used by Ansible's fetch_url/
open_url, and by most other Python HTTP clients that fall through to it) to
try IPv4 first and only fall back to IPv6 if no IPv4 address is offered or
works. Nothing changes on hosts where IPv6 actually works, or has none to
offer in the first place.
"""
import socket

_orig_getaddrinfo = socket.getaddrinfo


def _prefer_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _orig_getaddrinfo(host, port, family, type, proto, flags)
    return sorted(results, key=lambda r: 0 if r[0] == socket.AF_INET else 1)


socket.getaddrinfo = _prefer_ipv4_getaddrinfo

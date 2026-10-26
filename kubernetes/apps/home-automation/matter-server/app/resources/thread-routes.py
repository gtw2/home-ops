"""Install the routes Thread border routers advertise in RA Route Information Options.

The kernel does this itself when accept_ra_rt_info_max_plen is set, but that
sysctl cannot be set in this pod (see the HelmRelease), so this listens for
Router Advertisements and mirrors each RIO into the routing table via the
advertising router, with the advertised lifetime. A border router that changes
address or is replaced is picked up from its next RA.
"""

import ipaddress
import select
import socket
import struct
import subprocess
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "net1"

ND_ROUTER_SOLICIT = 133
ND_ROUTER_ADVERT = 134
OPT_ROUTE_INFO = 24
ICMP6_FILTER = 1
INFINITY = 0xFFFFFFFF
# The kernel's ceiling once the sysctl is set to 64; Thread mesh and OMR
# prefixes are /64, and ::/0 is left to the VLAN gateway.
MAX_PLEN = 64


def log(msg):
    print(msg, flush=True)


def open_socket(ifindex):
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
    # A set bit blocks that ICMPv6 type; pass Router Advertisements only.
    blocked = [0xFFFFFFFF] * 8
    blocked[ND_ROUTER_ADVERT >> 5] &= ~(1 << (ND_ROUTER_ADVERT & 31))
    s.setsockopt(socket.IPPROTO_ICMPV6, ICMP6_FILTER, struct.pack("8I", *blocked))
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, ifindex)
    return s


def route_infos(ra):
    """Yield (network, lifetime) for each well-formed RIO in an RA body."""
    opts, i = ra[16:], 0
    while i + 2 <= len(opts):
        kind, size = opts[i], opts[i + 1] * 8
        if size == 0 or i + size > len(opts):
            return
        opt = opts[i : i + size]
        i += size
        if kind != OPT_ROUTE_INFO or size < 8:
            continue
        plen = opt[2]
        # RFC 4191 2.3: the option is only as long as the prefix needs.
        if plen > 128 or (plen > 64 and size < 24) or (plen > 0 and size < 16):
            continue
        lifetime = struct.unpack("!I", opt[4:8])[0]
        addr = ipaddress.IPv6Address(opt[8:24].ljust(16, b"\0"))
        yield ipaddress.IPv6Network((addr, plen), strict=False), lifetime


def ip_route(*args):
    result = subprocess.run(["ip", "-6", "route", *args], capture_output=True, text=True)
    if result.returncode != 0:
        log(f"ip -6 route {' '.join(args)}: {result.stderr.strip()}")
    return result.returncode == 0


def main():
    ifindex = socket.if_nametoindex(IFACE)
    s = open_socket(ifindex)
    s.sendto(struct.pack("!BBHI", ND_ROUTER_SOLICIT, 0, 0, 0), ("ff02::2", 0, 0, ifindex))
    log(f"listening for Route Information Options on {IFACE}")

    # network -> (router, deadline); only used to log changes, not refreshes.
    routes = {}
    while True:
        select.select([s], [], [])
        data, ancdata, _, addr = s.recvmsg(4096, socket.CMSG_SPACE(4))
        router = addr[0].split("%")[0]
        hoplimit = next(
            (struct.unpack("i", d)[0] for lvl, typ, d in ancdata
             if lvl == socket.IPPROTO_IPV6 and typ == socket.IPV6_HOPLIMIT),
            None,
        )
        # RFC 4861 6.1.2: only accept RAs that cannot have been forwarded.
        if (
            len(data) < 16
            or data[0] != ND_ROUTER_ADVERT
            or data[1] != 0
            or hoplimit != 255
            or not ipaddress.IPv6Address(router).is_link_local
        ):
            continue

        now = time.monotonic()
        for net, lifetime in route_infos(data):
            if not 0 < net.prefixlen <= MAX_PLEN:
                continue
            target = [str(net), "via", router, "dev", IFACE, "proto", "ra"]
            if lifetime == 0:
                # A router can only withdraw the route it is the next hop for.
                if routes.get(net, (None,))[0] == router:
                    del routes[net]
                    if ip_route("del", *target):
                        log(f"withdrew {net} via {router}")
                continue

            expires = [] if lifetime == INFINITY else ["expires", str(lifetime)]
            if not ip_route("replace", *target, *expires):
                continue
            previous, deadline = routes.get(net, (None, 0))
            if previous != router or deadline < now:
                log(f"installed {net} via {router} lifetime {lifetime}s")
            routes[net] = (router, now + lifetime)


if __name__ == "__main__":
    main()

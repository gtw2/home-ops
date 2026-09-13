#!/usr/bin/env python3
"""Assert every tunnelled port declared in kubernetes/ is published on the hub.

A Service opts into towonel with towonel.io/tunnel and declares, per named port,
towonel.io/<name>.udp|.tcp plus towonel.io/<name>.public-port. The edge can only
receive that traffic if docker/sentinel/01-towonel/docker-compose.yaml also
publishes the port, and nothing at runtime complains when it does not - the game
server is simply unreachable through the tunnel.

ufw cannot be the safety net: Docker's published ports bypass it entirely (see
the comment in ansible/sentinel/playbook.yaml), so this check is the safety net.
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker/sentinel/01-towonel/docker-compose.yaml"
K8S = ROOT / "kubernetes"

PUBLIC_PORT = re.compile(r"^\s*towonel\.io/([A-Za-z0-9._-]+)\.public-port:\s*['\"]?(\d+)['\"]?\s*$")
PROTO = re.compile(r"^\s*towonel\.io/([A-Za-z0-9._-]+)\.(udp|tcp):\s*['\"]?(\w+)['\"]?\s*$")
TRUTHY = {"true", "yes", "enable", "enabled"}


def declared():
    """(port, proto, source) for every tunnelled port annotation in kubernetes/."""
    out = []
    for path in sorted(K8S.rglob("*.yaml")):
        text = path.read_text(errors="replace")
        if "towonel.io/tunnel" not in text:
            continue
        ports, protos = {}, {}
        for line in text.splitlines():
            m = PUBLIC_PORT.match(line)
            if m:
                ports[m.group(1)] = int(m.group(2))
            m = PROTO.match(line)
            if m and m.group(3).lower() in TRUTHY:
                protos[m.group(1)] = m.group(2)
        for name, port in ports.items():
            proto = protos.get(name)
            if proto is None:
                out.append((port, None, f"{path.relative_to(ROOT)} ({name})"))
            else:
                out.append((port, proto, f"{path.relative_to(ROOT)} ({name})"))
    return out


def published():
    doc = yaml.safe_load(COMPOSE.read_text())
    svc = doc["services"]["towonel-hub"]
    out = set()
    for entry in svc.get("ports", []) or []:
        spec = entry if isinstance(entry, str) else entry.get("published")
        host = str(spec).split(":")[0]
        proto = "udp" if str(spec).endswith("/udp") else "tcp"
        out.add((int(host), proto))
    return out


def main():
    have, problems = published(), []
    for port, proto, src in declared():
        if proto is None:
            problems.append(f"  {src}: public-port {port} has no .udp/.tcp annotation")
        elif (port, proto) not in have:
            problems.append(
                f"  {src}: declares {port}/{proto} but {COMPOSE.relative_to(ROOT)} "
                f"does not publish it"
            )
    if problems:
        print("towonel port check FAILED:\n" + "\n".join(problems))
        print(f"\npublished on the hub: {sorted(have)}")
        return 1
    print(f"towonel port check OK: {len(declared())} declared port(s), all published")
    return 0


if __name__ == "__main__":
    sys.exit(main())

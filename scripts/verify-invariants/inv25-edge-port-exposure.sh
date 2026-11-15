#!/usr/bin/env bash
# Invariant (Rider A / decision-talus-setup.md D4) — the LAN sees only the edge.
#
# No service in this stack may publish a port on a public interface (0.0.0.0 / ::)
# except the edge doors: caddy 80+443, plus 8443 ONLY when the IP-mode overlay is
# present (./docker-compose.override.yml — the appliance's bare-IP auth door).
# Loopback publishes (127.0.0.1) are allowed everywhere: dev + on-box diagnostics.
#
# Mode-aware and honest on any box: SKIPs when the stack is not up. bash 3.2-safe.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT" || { echo "inv25: FAIL — cd"; exit 2; }

command -v docker >/dev/null 2>&1 || { echo "inv25: SKIP — docker not available on this box"; exit 3; }
IDS="$(docker compose ps -q 2>/dev/null || true)"
[ -n "$IDS" ] || { echo "inv25: SKIP — stack not up (docker compose ps returned nothing)"; exit 3; }

ALLOW_8443=0
[ -f ./docker-compose.override.yml ] && ALLOW_8443=1
echo "  IP-mode overlay present: $([ "$ALLOW_8443" -eq 1 ] && echo yes — 8443 allowed || echo no — 8443 forbidden)"

fail=0
checked=0
for id in $IDS; do
  checked=$((checked+1))
  out="$(docker inspect -f '{{.Name}}|{{json .NetworkSettings.Ports}}' "$id" 2>/dev/null | python3 -c '
import sys, json
line = sys.stdin.read().strip()
if not line: sys.exit(0)
name, _, ports_json = line.partition("|")
name = name.lstrip("/")
allow_8443 = sys.argv[1] == "1"
ports = json.loads(ports_json) if ports_json and ports_json != "null" else {}
is_caddy = "caddy" in name
for cport, binds in (ports or {}).items():
    for b in (binds or []):
        hip, hport = b.get("HostIp", ""), b.get("HostPort", "")
        if hip in ("0.0.0.0", "", "::"):
            allowed = is_caddy and (hport in ("80", "443") or (allow_8443 and hport == "8443"))
            if not allowed:
                print("%s publishes %s:%s -> %s on a public interface" % (name, hip or "0.0.0.0", hport, cport))
' "$ALLOW_8443")"
  if [ -n "$out" ]; then
    echo "$out" | sed 's/^/      FAIL: /'
    fail=1
  fi
done

echo "  containers checked: $checked"
if [ "$fail" -eq 0 ]; then echo "inv25: PASS — no non-edge public publishes"; else echo "inv25: FAIL"; fi
exit "$fail"

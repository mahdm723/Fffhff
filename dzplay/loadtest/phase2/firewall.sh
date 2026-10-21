#!/usr/bin/env bash
# V6 phase 21 (phase 2, option 1: a separate load-generator VPS): only that machine may reach staging's ports —
# 8443 (staging), 8086 (the k6 sink) and 9000 (the mock mailbox).
#   sudo loadtest/phase2/firewall.sh on <GENERATOR_IP>   |   off   |   status
# Docker's published ports bypass ufw, so the rules go into the DOCKER-USER chain (like deploy/cloudflare-ips.sh),
# matched on the original destination port. SSH and production's 80/443 are never touched.
set -euo pipefail
CHAIN=DZ-STAGING
PORTS="8443 8086 9000"
valid_ip() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
off() {
    for port in $PORTS; do
        while iptables -C DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -j $CHAIN 2>/dev/null; do
            iptables -D DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -j $CHAIN
        done
    done
    iptables -F $CHAIN 2>/dev/null || true
    iptables -X $CHAIN 2>/dev/null || true
}
case "${1:-status}" in
    on)
        ip="${2:-}"
        valid_ip "$ip" || { echo "usage: $0 on <GENERATOR_IPv4>" >&2; exit 2; }
        off
        iptables -N $CHAIN
        iptables -A $CHAIN -s "$ip" -j RETURN
        iptables -A $CHAIN -j DROP
        for port in $PORTS; do
            iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -j $CHAIN
        done
        echo "✓ staging ports ($PORTS) open to $ip only"
        ;;
    off) off; echo "✓ staging firewall rule removed" ;;
    status) iptables -S $CHAIN 2>/dev/null || echo "no staging rule" ;;
    *) echo "usage: $0 on <GENERATOR_IP> | off | status" >&2; exit 2 ;;
esac

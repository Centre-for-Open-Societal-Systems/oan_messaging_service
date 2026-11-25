#!/bin/sh
# Allow outbound only to the internal stack subnet (plus replies to inbound connections
# and Docker's DNS), then run the forwarder as 10001 with every capability dropped.
set -eu
for t in iptables ip6tables; do
  $t -P OUTPUT ACCEPT
  $t -F OUTPUT
  $t -A OUTPUT -o lo -j ACCEPT
  $t -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
done
iptables -A OUTPUT -d "${EDGE_ALLOW_CIDR:?}" -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -j ACCEPT
iptables -A OUTPUT -j REJECT
ip6tables -A OUTPUT -j REJECT
exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all --bounding-set=-all \
     --no-new-privs python /edge/edge.py

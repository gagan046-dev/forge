#!/bin/sh
# Runs at every node start: k3d executes /bin/k3d-entrypoint-*.sh in alphabetical
# order, so "zz" runs after k3d's own DNS fix.
#
# Why: Docker Desktop's internal DNS (192.168.65.254) sometimes answers
# "Try again" to containerd's Go resolver, so image pulls fail with
# "lookup nvcr.io: Try again". A public fallback resolver fixes that.
# CoreDNS forwards to this same file, so pods get the fallback too.
set -o errexit
if ! grep -q "nameserver 1.1.1.1" /etc/resolv.conf; then
  echo "nameserver 1.1.1.1" >> /etc/resolv.conf
  echo "[dns-fallback] added 1.1.1.1 to /etc/resolv.conf"
fi

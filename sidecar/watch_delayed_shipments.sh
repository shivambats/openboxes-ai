#!/bin/sh
set -eu
cd "$(dirname "$0")"
while :; do
  ./trigger_delayed_shipments.sh || echo 'Delayed shipment sweep requires review' >&2
  sleep 1800
done

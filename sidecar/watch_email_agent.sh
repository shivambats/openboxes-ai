#!/bin/sh
set -eu
cd "$(dirname "$0")"
interval="${EMAIL_POLL_INTERVAL_SECONDS:-60}"
case "$interval" in
  *[!0-9]*|'') echo 'EMAIL_POLL_INTERVAL_SECONDS must be an integer' >&2; exit 2 ;;
esac
if [ "$interval" -lt 15 ] || [ "$interval" -gt 3600 ]; then
  echo 'EMAIL_POLL_INTERVAL_SECONDS must be between 15 and 3600' >&2
  exit 2
fi
while :; do
  ./trigger_email_agent.sh || echo 'Email poll requires review; retrying next interval' >&2
  sleep "$interval"
done

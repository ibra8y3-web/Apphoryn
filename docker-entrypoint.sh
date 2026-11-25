#!/bin/sh
set -eu

# Railway mounts volumes after the image is built. The mounted directory is
# initially root-owned, so fix ownership at container startup before dropping
# to the non-root application account.
mkdir -p /srv/apphoryn/storage/artifacts
chown -R apphoryn:apphoryn /srv/apphoryn/storage

exec gosu apphoryn "$@"

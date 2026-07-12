#!/bin/sh
# traefik-certs-dumper post-hook — runs after each successful dump.
#
# The relay runs as uid 1000 and reads the certificate + key read-only, so make
# the freshly dumped files traversable (dirs) and readable (files) by it. The
# dumper writes keys 0600 by default, which uid 1000 could not read — that makes
# the relay's TLS listeners silently stay off (see ../../../docs/DEPLOY-EDGE.md
# §2). The keys stay inside the private `certs` volume, so a+r does not expose
# them outside the stack.
set -eu
chmod -R a+rX /certs

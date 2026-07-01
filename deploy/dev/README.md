# Dev stack (Traefik 3.6 edge, Docker Desktop)

A local mirror of the internet-facing production topology (see
[../../docs/DEPLOY-EDGE.md](../../docs/DEPLOY-EDGE.md)) so the whole path can be
tested end-to-end without a public domain or real ACME.

| Service | Role |
|---------|------|
| `certgen` | one-shot: writes a self-signed cert for `*.smtp-relay.localhost` into the `dev-certs` volume |
| `traefik` | v3.6 edge — terminates HTTPS for the panel, TCP-passes-through 465/587 to the relay |
| `ui` | FastAPI panel, reached by Traefik **directly** (Option A trusted-proxy: no nginx) |
| `relay` | terminates SMTP TLS itself on 465 (implicit) + 587 (STARTTLS) |

## Run

```sh
cp deploy/dev/.env.example deploy/dev/.env
# fill ENCRYPTION_KEY and SECRET_KEY (commands are in the file)
docker compose --env-file deploy/dev/.env -f deploy/dev/docker-compose.dev.yml up -d --build
```

Host ports (loopback only): `8443` HTTPS panel, `8587` STARTTLS, `8465` SMTPS,
`2525` plain SMTP, `18080` Traefik dashboard.

## Smoke-test

```sh
# Panel via Traefik TLS (self-signed):
curl -k --resolve panel.smtp-relay.localhost:8443:127.0.0.1 \
     https://panel.smtp-relay.localhost:8443/healthz          # -> {"status":"ok"}

# SMTPS (implicit TLS) through Traefik passthrough:
python -c "import smtplib,ssl; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; s=smtplib.SMTP_SSL('127.0.0.1',8465,context=c); print(s.ehlo()); s.quit()"

# STARTTLS through Traefik passthrough:
python -c "import smtplib,ssl; s=smtplib.SMTP('127.0.0.1',8587); s.ehlo(); print('starttls', s.starttls(context=ssl._create_unverified_context())); print(s.ehlo()); s.quit()"
```

> TLS verification is disabled in the snippets above only because this is a
> throwaway self-signed cert on loopback. To verify instead, copy the cert out
> of the volume (`docker compose -f deploy/dev/docker-compose.dev.yml cp
> certgen:/certs/fullchain.pem ./dev-ca.pem`) and pass it as the CA.

The admin's first-run password is printed by the ui container:

```sh
docker compose -f deploy/dev/docker-compose.dev.yml logs ui | grep -i "temporary password"
```

Browse to `https://panel.smtp-relay.localhost:8443/` (accept the self-signed
warning). Because the panel sits behind Traefik with `FORWARDED_ALLOW_IPS` set to
the Traefik container IP, the audit log records your real client IP, not the
proxy's.

## Tear down

```sh
docker compose -f deploy/dev/docker-compose.dev.yml down -v
```

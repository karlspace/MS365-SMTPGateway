# Production deployment

Internet-facing production stack for the MS365 SMTP Gateway, behind a **Traefik**
or **Coolify** edge. Both variants pull the pre-built images published to GHCR by
CI — they never build locally.

| File | Platform | Edge / TLS |
|------|----------|------------|
| [`docker-compose.traefik.yml`](docker-compose.traefik.yml) | Standalone Traefik v3 | Your Traefik (ACME), attached via `traefik.*` labels |
| [`docker-compose.coolify.yml`](docker-compose.coolify.yml) | Coolify PaaS | Coolify's managed proxy (routing + TLS from the dashboard) |

Both follow **Option A** of [`../../docs/DEPLOY-EDGE.md`](../../docs/DEPLOY-EDGE.md):
the edge terminates HTTPS for the panel and proxies plain HTTP to `ui:8000` — no
internal nginx. The **relay terminates SMTP TLS itself** on 465/587, so those
ports are published straight to the host and do not traverse the HTTP proxy.

```
 client ── 443 (HTTPS) ──▶ edge (Traefik/Coolify) ──http──▶ ui:8000   (panel)
 client ── 587 (STARTTLS)─▶ relay        (published to host; relay presents cert)
 client ── 465 (SMTPS) ───▶ relay
 LAN    ── 2525 (plain) ──▶ relay        (opt-in, bind to a LAN interface only)
```

## Images

```
ghcr.io/karlspace/ms365-smtpgateway/ui:${IMAGE_TAG}
ghcr.io/karlspace/ms365-smtpgateway/relay:${IMAGE_TAG}
```

`IMAGE_TAG` defaults to `latest` (HEAD of the `workspace` branch). **Pin an
immutable tag in production** — a short SHA (`db4c9be`) or date-time build
(`20260702-210755`) — so redeploys are reproducible. `pull_policy: always`
ensures moving tags actually refresh on redeploy.

> nginx is intentionally **not** deployed here (Option A). If you must keep the
> bundled nginx in front of the panel instead, see Option B in DEPLOY-EDGE.md.

---

## Deploy with Traefik

**Prerequisites**
- Traefik v3 running on an external Docker network (default name `EDGEPROXY`)
  with a `web` (:80) and `web-secure` (:443) entrypoint and an ACME
  `certResolver` (default name `letsencrypt`).
- DNS record for `PANEL_DOMAIN` → this host.
- Host ports 465 + 587 free.

```sh
cd deploy/production
cp .env.production.example .env
#   fill ENCRYPTION_KEY, SECRET_KEY, PANEL_DOMAIN, FORWARDED_ALLOW_IPS, PROXY_NETWORK
docker compose -f docker-compose.traefik.yml --env-file .env up -d
```

If your Traefik entrypoints are not named `web` / `web-secure`, adjust the
`entrypoints=` lines in the `ui` service labels.

---

## Deploy with Coolify

1. **New Resource → Docker Compose**, and point it at `docker-compose.coolify.yml`.
2. Set the environment variables (at least `ENCRYPTION_KEY`, `SECRET_KEY`,
   `FORWARDED_ALLOW_IPS`) in the Coolify dashboard.
3. On the **`ui`** service set the **Domain** to `https://panel.smtp.example.com`
   — Coolify provisions the certificate and routes it to container port `8000`.
4. Ensure host ports **465** and **587** are reachable (open the firewall; Coolify
   honours the `ports:` mappings on the `relay` service).

Coolify generates the HTTP router and TLS itself, so this file carries no
`traefik.*` labels.

---

## TLS for the SMTP listeners

The relay starts the **465/587** listeners only once **both** files exist in the
`certs` volume, readable by the relay user (**uid 1000**):

```
/etc/smtp-relay/certs/fullchain.pem
/etc/smtp-relay/certs/privkey.pem
```

Until then the relay serves **2525 plaintext only** — the stack still comes up
cleanly; wire the cert, then `restart` the relay (it also hot-reloads on change).

> ⚠ The private key must be **readable by uid 1000**. A `600` key owned by root
> or another uid makes `load_cert_chain` fail with *Permission denied* and TLS
> silently stays off (logged at ERROR). `chmod 644` the key, or have the writer
> run with a matching uid. See DEPLOY-EDGE.md §2.

Pick one provisioning method — the volume is just the drop-off point:

- **traefik-certs-dumper** (Traefik ACME): run
  [`ldez/traefik-certs-dumper`](https://github.com/ldez/traefik-certs-dumper) as a
  sidecar that watches Traefik's `acme.json` and writes `fullchain.pem` /
  `privkey.pem` into the `certs` volume. Point `SMTP_TLS_CERT` / `SMTP_TLS_KEY`
  at whatever filenames it emits if they differ.
- **certbot / host-managed cert**: replace the named `certs` volume with a
  bind-mount of the directory holding your PEM pair (e.g. a certbot
  `live/<domain>/` dir), mounted read-only at `/etc/smtp-relay/certs`.
- **Caddy**: copy `<domain>.crt` → `fullchain.pem` and `<domain>.key` →
  `privkey.pem` from Caddy's data dir into the volume.

The SMTP certificate should cover the **mail hostname** clients connect to
(e.g. `mail.example.com`), which may differ from `PANEL_DOMAIN`.

---

## First boot

The admin's temporary password is printed once by the `ui` container:

```sh
docker compose -f docker-compose.traefik.yml logs ui | grep -i "temporary password"
```

Log in at `https://${PANEL_DOMAIN}/`, complete TOTP enrolment, then add the Entra
ID application credentials and SMTP accounts through the panel.

## Verify the trusted-proxy chain

Per-IP bans and rate-limits depend on the panel seeing the **real** client IP.
After first login, log in from two different public IPs and confirm the audit log
shows those IPs — **not** the edge address. If it shows the edge IP, narrow
`FORWARDED_ALLOW_IPS` to the edge subnet (never `*`) and confirm your uvicorn
build honours it. See DEPLOY-EDGE.md §1.

## Smoke test

```sh
# Panel health via the edge:
curl -fsS https://${PANEL_DOMAIN}/healthz            # -> {"status":"ok"}

# SMTPS (implicit TLS):
python -c "import smtplib; s=smtplib.SMTP_SSL('mail.example.com',465); print(s.ehlo()); s.quit()"

# Submission (STARTTLS):
python -c "import smtplib; s=smtplib.SMTP('mail.example.com',587); s.ehlo(); print(s.starttls()); s.quit()"
```

## Operations

```sh
# Update to a new image tag (edit IMAGE_TAG in .env), then:
docker compose -f docker-compose.traefik.yml --env-file .env pull
docker compose -f docker-compose.traefik.yml --env-file .env up -d

# Logs / status:
docker compose -f docker-compose.traefik.yml ps
docker compose -f docker-compose.traefik.yml logs -f relay
```

Persistent state lives in the `data` volume (SQLite DB + message archive) and the
`certs` volume. Back both up. See [`../../docs/DEPLOY-EDGE.md`](../../docs/DEPLOY-EDGE.md)
§5 for internet hardening guidance (AUTH-over-TLS only, tight ban thresholds,
`LOG_LEVEL=INFO`).

# Internet-facing deployment behind Traefik / Caddy

This fork is run **internet-facing** with the SMTP submission ports and the web
admin panel reachable from the public internet, always **behind a Traefik or
Caddy edge proxy**. The edge owns TLS termination for HTTP, ACME/Let's Encrypt,
HTTP rate-limiting and HTTP→HTTPS redirects. The relay terminates SMTP TLS
itself (465 + 587) and the edge only passes the TCP stream through.

> The upstream project states the panel is *not designed to be internet-facing*.
> Treat everything below as the minimum hardening for exposing it anyway, and
> keep the panel behind an IP allow-list or VPN whenever you can.

---

## Topology

```
                          ┌─────────────── Traefik / Caddy (edge) ───────────────┐
 client ── 443 (HTTPS) ──▶│ TLS terminate + ACME + rate-limit ─▶ ui:8000 (http)  │
 client ── 587 (STARTTLS)▶│ TCP passthrough (SNI) ────────────▶ relay:587        │
 client ── 465 (SMTPS) ──▶│ TCP passthrough (SNI) ────────────▶ relay:465        │
                          └───────────────────────────────────────────────────── ┘
 LAN devices ── 2525 (plain) ─────────────────────────────────▶ relay:2525 (bind to LAN only)
```

- **Web UI:** edge terminates TLS, proxies plain HTTP to the UI.
- **SMTP 465/587:** edge does **TCP passthrough** (Traefik/Caddy cannot speak
  STARTTLS themselves); the relay presents the certificate and enforces
  `auth_require_tls`, so credentials are never sent in clear.
- **SMTP 2525:** plaintext, for trusted LAN devices only — bind it to a LAN
  interface with `SMTP_BIND_HOST=<lan-ip>` and never publish it to the internet.

---

## 1. The trusted-proxy chain (do this first — bans depend on it)

The UI derives the client IP for **login bans** and the **slowapi rate-limit**
from `X-Forwarded-For`, trusting it only from `FORWARDED_ALLOW_IPS`. The bundled
`nginx.conf` *overwrites* `X-Forwarded-For` with the socket peer:

```nginx
proxy_set_header X-Forwarded-For $remote_addr;
```

Behind an additional edge proxy that peer is the **edge**, not the real client —
so every client would look like the edge and per-IP bans/rate-limits collapse.
Pick one:

**Option A — edge → UI directly (recommended).** Drop the internal nginx from the
UI path and point the edge at `ui:8000`. Set `FORWARDED_ALLOW_IPS` to the edge's
container IP/subnet so uvicorn trusts (only) the edge's `X-Forwarded-For`:

```yaml
# ui service
environment:
  FORWARDED_ALLOW_IPS: "10.0.0.0/8"   # the edge network only, never "*"
```

Configure the edge to **replace** any client-supplied `X-Forwarded-For` with the
real peer (Traefik `forwardedHeaders.trustedIPs`, Caddy sets it automatically).

**Option B — keep nginx behind the edge.** Change nginx to forward the client IP
the edge already put in the header instead of overwriting it:

```nginx
# trust the edge; the edge must strip client-supplied XFF
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

and keep `FORWARDED_ALLOW_IPS` pointed at the nginx static IP as today. Only safe
if the edge sanitises inbound `X-Forwarded-For`.

Verify: log in from two different public IPs and confirm the audit log shows the
**real** client IPs, not the edge IP.

---

## 2. Certificate for the SMTP listeners

The relay reads `SMTP_TLS_CERT` / `SMTP_TLS_KEY` (default
`/etc/smtp-relay/certs/fullchain.pem` and `privkey.pem`) from the shared `certs`
volume and starts the 465/587 listeners only when both exist. It watches the
cert file and rebuilds the listeners on change (ACME renewal). **The cert must
exist before the relay starts** — otherwise start the relay, provision the cert,
then `docker compose restart relay` once.

> **The private key must be readable by the relay's user (uid 1000).** The relay
> runs unprivileged, so a `privkey.pem` written `600` and owned by a different
> uid (e.g. nginx's 101, or an ACME dumper running as root) makes
> `load_cert_chain` fail with *Permission denied* and TLS silently stays off
> (logged at ERROR: `Failed to load SMTP TLS cert`). Ensure the writer chmods the
> key so uid 1000 can read it, or run the dumper with a matching uid.

**Traefik** stores ACME certs in `acme.json`; extract them to PEM into the volume
with [`traefik-certs-dumper`](https://github.com/ldez/traefik-certs-dumper) as a
sidecar writing `fullchain.pem` / `privkey.pem` into `smtp-relay-certs`.

**Caddy** stores PEMs under its data dir
(`/data/caddy/certificates/acme-.../<domain>/`); mount or copy `<domain>.crt` →
`fullchain.pem` and `<domain>.key` → `privkey.pem` into the volume.

---

## 3. Traefik reference (dynamic config)

```yaml
tcp:
  routers:
    smtp-submission:
      entryPoints: ["smtp587"]
      rule: "HostSNI(`mail.example.com`)"
      tls: { passthrough: true }
      service: relay-587
    smtp-smtps:
      entryPoints: ["smtp465"]
      rule: "HostSNI(`mail.example.com`)"
      tls: { passthrough: true }
      service: relay-465
  services:
    relay-587:
      loadBalancer:
        servers: [{ address: "relay:587" }]
    relay-465:
      loadBalancer:
        servers: [{ address: "relay:465" }]

http:
  routers:
    panel:
      entryPoints: ["websecure"]
      rule: "Host(`panel.example.com`)"
      tls: { certResolver: le }
      middlewares: ["panel-ratelimit", "panel-allowlist"]
      service: panel
  services:
    panel:
      loadBalancer:
        servers: [{ url: "http://ui:8000" }]
  middlewares:
    panel-ratelimit:
      rateLimit: { average: 20, burst: 40, period: 1m }
    panel-allowlist:              # optional but recommended
      ipAllowList: { sourceRange: ["203.0.113.0/24"] }
```

Entry points `smtp587`/`smtp465` map host ports 587/465 in the static config;
`forwardedHeaders.trustedIPs` on `websecure` should list the edge network.

---

## 4. Caddy reference (with the `layer4` app)

```caddyfile
{
    layer4 {
        :587 { route { proxy relay:587 } }   # TCP passthrough; relay does STARTTLS
        :465 { route { proxy relay:465 } }   # TCP passthrough; relay does TLS
    }
}

panel.example.com {
    rate_limit { zone login { key {remote_host} events 20 window 1m } }
    reverse_proxy ui:8000
}
```

Caddy obtains and renews the panel cert automatically; sync it to the relay cert
volume as in section 2. (`rate_limit` requires the caddy-ratelimit plugin.)

---

## 5. Internet guard-rails (relay configuration)

- **Disable IP-whitelist auth** — over the internet require **AUTH over TLS**
  only. IP whitelisting is brittle and dangerous once the source is the edge/NAT.
- **Keep the authorised-senders check ON**; never use the "disable sender check"
  toggle on an internet-facing relay.
- Use **strong, unique SMTP account passwords** (bcrypt cost 12 is enforced).
- Keep the **SMTP AUTH ban** thresholds tight; bans are enforced on every message
  path (EHLO, AUTH, and MAIL FROM).
- Edge **rate-limit the login route** (sections 3/4); the app adds per-IP bans and
  mandatory TOTP on top.
- Leave `LOG_LEVEL=INFO` in production (DEBUG makes aiosmtpd log SMTP commands).
```

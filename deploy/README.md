# Current domain deployment — updated 2026-10-01

Primary site: https://sonrazbor.ru/bridge . The Cyrillic domain
https://сонразбор.рф/bridge and both new `www` names redirect with HTTP 308 to
`https://sonrazbor.ru`. Nginx and the certificate use the ASCII form
`xn--80acrweekeb.xn--p1ai`. The previous sslip test origin remains an explicit
allowed alias; guest cookies remain host-only and are not shared across origins.

All four new DNS A records point to 212.116.115.150. The active source for the
new vhost is `deploy/nginx-domain-https.conf`, installed as
`/etc/nginx/sites-available/sonnic-domain`. Certificate `sonnic-public` covers
the four names. The old `sonnic-test` certificate/vhost remains available.
The certificate renewal dry run with deploy hooks succeeded:

```sh
certbot renew --cert-name sonnic-public --dry-run --run-deploy-hooks
```

The canonical `SONNIC_BRIDGE_ORIGIN` is now `https://sonrazbor.ru`. Host/Origin
validation was updated and api, web, gateway were recreated together.
Rollback files are at `/opt/sonnic/rollback/domain-20261001`; previous images
have `before-domain-20261001` tags. Deployed API image:
`4bc0c9b9e6b314f6ceb5b156a63b48c1c93d7800b6d2f7a71852b16c29b26dfa`.

Strict TLS checks on the VPS passed for all five hostnames, with final HTTP 200
and the expected canonical redirects. Nine CSS/JS resources returned 200.
Guest cookies have Secure, HttpOnly, SameSite=Strict and host-only scope;
two requests with an invalid Origin returned 403. The server test suite before
subsequent CDP diagnostics passed 247 tests with 3 skipped.

After the owner reconnected Happ, the canonical address also passed strict TLS
on the Windows PC. The Cyrillic names returned 308 then 200; the old sslip
address returned 200. A certificate mismatch remained for `www.sonrazbor.ru`
on that PC only. Its VPS check passes and both public and Windows DNS already
resolve to the new IP, so this is a local network-path symptom through Happ,
likely its remaining name cache or routing; its internal mechanism is not yet
confirmed. Do not weaken TLS verification or change the correct nginx vhost
to accommodate this symptom.

Pre-deployment backup `scheduled-20260930T211558Z` passed both DPAPI and portable
age restore checks. The owner confirmed independent storage of key and archive;
dated local backup archives have a 90-day retention policy.

This verifies the domain deployment, not readiness of the complete dream
processing service. Current config reports worker offline and personal input
disabled. The latest four-visitor live acceptance run and observation remain
unfinished. New jobs use isolated temporary chats with no personalization when
the worker passes its guards; the shared-project workflow below is historical.
No OpenAI API key is used. See `docs/DOMAIN_SETUP.md` and the current prefix of
`docs/STATUS_AND_COMPLETION_PLAN.md` before operating the service.

The remainder is historical deployment documentation from 2026-09-09.

# Sonnic server test deployment — 2026-09-09

Server: 212.116.115.150 (existing SSH alias `sprintbox-itp`, port 2222).
Public test page: https://sonnic.212.116.115.150.sslip.io/bridge
MCP: https://sonnic.212.116.115.150.sslip.io/mcp

This deploys the automatic synthetic-only bridge, not a public personal-data
product. No OpenAI API key is present. A dedicated Windows worker pulls a queued
job over the Sonnic service API, opens a new chat in the fixed ChatGPT Project,
reads the final correlated JSON and submits it back to the VPS. The current live
config is `automatic_dispatch=true`, `synthetic_only=true`. The worker state is
shown on the site; new submissions are disabled while it is unavailable.

## Isolation and operations

- Source: `/opt/sonnic/source`; compose file: `deploy/compose.yaml`.
- Compose project `sonnic`: api, web, gateway; restart unless stopped.
- Only host binding: `127.0.0.1:3180`. Nginx serves the dedicated Sonnic hostname.
- Containers share the API container network namespace. API8000 and web3000
  bind its loopback. Only the narrow gateway3010 is mapped to the host.
- Named volume `sonnic_data` contains beta SQLite data. Do not run down -v.
- API provider is disabled. Gateway blocks main diary API, docs and source files.
- `SONNIC_AUTOMATIC_DISPATCH=true` and `SONNIC_WORKER_ALLOW_PERSONAL=false` keep
  the service queue automatic but limited to synthetic inputs. The worker token
  is shared only by the VPS environment and the dedicated local worker.
- Runtime memory limits total 1088 MiB. Builds may need additional memory.
- Environment file `/opt/sonnic/source/deploy/.env` is mode600 and is not bundled.
- HTTPS certificate `sonnic-test`, expires 2026-12-06 initially. Certbot timer is
  active; `/etc/letsencrypt/renewal-hooks/deploy/sonnic-reload` reloads nginx only
  for this certificate. ACME webroot `/opt/sonnic/acme` must be nginx-readable.
- Nginx vhost `/etc/nginx/sites-available/sonnic`, symlink in sites-enabled.
  Existing unrelated services and vhosts were retained.

From `/opt/sonnic/source/deploy`, deploy the complete Compose set:

```sh
docker compose up -d --build --force-recreate api web gateway
docker compose ps
docker compose logs --tail 30
```

Do not update only `web` or `gateway`: both use `network_mode: service:api`, so an
API recreation changes the shared network namespace and all three services must
be recreated together. To stop only Sonnic use `docker compose stop`; do not stop
system nginx or Docker.

For a fresh install build the allowlisted bundle with
`scripts/build_server_bundle.py`, transfer over SSH, verify the printed SHA256,
extract under the dedicated source directory, create `.env`, build, then start.
The `.env` includes `SONNIC_BRIDGE_ORIGIN`, `SONNIC_BRIDGE_CLIENT_ID`,
`SONNIC_BRIDGE_REDIRECT_URI`, `SONNIC_WORKER_TOKEN`,
`SONNIC_AUTOMATIC_DISPATCH=true` and `SONNIC_WORKER_ALLOW_PERSONAL=false`.
Callback verified in ChatGPT UI:
`https://chatgpt.com/connector_platform_oauth_redirect`, public client
`sonnic-chatgpt`, token authentication `none`, S256 PKCE, scope `sonnic:process`.

Initial install uses nginx-http.conf, obtains certbot webroot certificate, then
switches to nginx-https.conf after `nginx -t`. The hostname uses sslip.io for
testing and can later be replaced with an owned domain plus a new certificate
and matching origin/OAuth registration.

## Verified

Linux builds succeeded; all three containers started, API healthy. Public smoke
verification passed for the root redirect, `/bridge`, `/icon.svg`, CSP and the
other gateway security headers. The live config reports automatic synthetic-only
dispatch. Twelve different synthetic jobs completed the full site → ChatGPT → site
path. Project navigation was recovered through the sidebar. The latest checks
covered repeated spaces in dictated input and keeping Chrome alive after delivery.
These individual successes do not establish continuous availability.

`DELETE /api/v1/bridge/jobs/{id}` physically deletes the owner's server-side job
row and its dispatch-claim row, including while the job is active. It does not
delete the corresponding ChatGPT conversation or copies already captured by a
backup. Verified encrypted offsite snapshots now exist on the owner's Windows PC.
A daily Codex automation at 03:30 runs `scripts/backup-cycle.ps1`; it requires the PC,
Codex and SSH to be available. DPAPI recovery is bound to that Windows account.
Retention and portable recovery if that PC is lost remain unconfigured.

Do not enable personal input: the shared ChatGPT Project does not provide verified
per-client memory isolation, ChatGPT history has a separate deletion lifecycle,
and the SQLite volume is not encrypted. The local Windows worker was resumed after
the latest fixes; persistent pause markers still prevent automatic retries after
ChatGPT limits, changed UI or a full-page load failure.

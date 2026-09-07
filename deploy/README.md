# Sonnic server test deployment — 2026-09-07

Server: 212.116.115.150 (existing SSH alias `sprintbox-itp`, port 2222).
Public test page: https://sonnic.212.116.115.150.sslip.io/bridge
MCP: https://sonnic.212.116.115.150.sslip.io/mcp

This deploys an owner-only personal beta, not a public multiuser product. No
OpenAI API key is present. ChatGPT must initiate the tool calls; submitting a
website job does not wake a ChatGPT Plus Project.

## Isolation and operations

- Source: `/opt/sonnic/source`; compose file: `deploy/compose.yaml`.
- Compose project `sonnic`: api, web, gateway; restart unless stopped.
- Only host binding: `127.0.0.1:3180`. Nginx serves the dedicated Sonnic hostname.
- Containers share the API container network namespace. API8000 and web3000
  bind its loopback. Only the narrow gateway3010 is mapped to the host.
- Named volume `sonnic_data` contains beta SQLite data. Do not run down -v.
- API provider is disabled. Gateway blocks main diary API, docs and source files.
- Runtime memory limits total 1088 MiB. Builds may need additional memory.
- Environment file `/opt/sonnic/source/deploy/.env` is mode600 and is not bundled.
- HTTPS certificate `sonnic-test`, expires 2026-12-06 initially. Certbot timer is
  active; `/etc/letsencrypt/renewal-hooks/deploy/sonnic-reload` reloads nginx only
  for this certificate. ACME webroot `/opt/sonnic/acme` must be nginx-readable.
- Nginx vhost `/etc/nginx/sites-available/sonnic`, symlink in sites-enabled.
  Existing unrelated services and vhosts were retained.

From `/opt/sonnic/source/deploy`: `docker compose ps`, `docker compose logs
--tail 30`, `docker compose up -d --build`. To stop only Sonnic use `docker
compose stop`; do not stop system nginx or Docker.

For a fresh install build the allowlisted bundle with
`scripts/build_server_bundle.py`, transfer over SSH, verify the printed SHA256,
extract under the dedicated source directory, create `.env`, build, then start.
The `.env` values are SONNIC_BRIDGE_ORIGIN, SONNIC_BRIDGE_CLIENT_ID and
SONNIC_BRIDGE_REDIRECT_URI. Callback verified in ChatGPT UI:
`https://chatgpt.com/connector_platform_oauth_redirect`, public client
`sonnic-chatgpt`, token authentication `none`, S256 PKCE, scope `sonnic:process`.

Initial install uses nginx-http.conf, obtains certbot webroot certificate, then
switches to nginx-https.conf after `nginx -t`. The hostname uses sslip.io for
testing and can later be replaced with an owned domain plus a new certificate
and matching origin/OAuth registration.

## Verified

Linux builds succeeded; all three containers started, API healthy. External TLS
verification passed: `/bridge` and authorization metadata200, unauthenticated
MCP401, main dreams API/docs/.env404. OAuth client installed in ChatGPT and both
claim_job and submit_result discovered after Refresh. End-to-end job result is
recorded separately in docs/HANDOFF.md.

The 2026-09-07 beta accepts an owner-provided DreamInput after explicit consent,
marks it `data_class=personal`, binds browser and MCP access to the same owner,
requires review before completion, and offers logical deletion. Do not open this
flow to unrelated clients: the shared ChatGPT Project does not provide verified
per-client memory isolation, and the SQLite volume is not encrypted.

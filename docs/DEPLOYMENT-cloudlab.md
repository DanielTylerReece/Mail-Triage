# Deployment — Self-Hosted with Cloudflare Tunnel

A reference deployment using Cloudflare Tunnel as the public entry point and Docker on a small Linux VM. General setup is in [SETUP.md](SETUP.md); this doc covers only the network plumbing for one common pattern.

## Topology

```
internet ──► Cloudflare Tunnel ──► reverse-proxy host
                                        │
                                        ▼
                                  app host:8088
                                        │
                                        ▼
                              Mail-Triage container
                                        │
                                        ▼
                       Microsoft Graph + LLM provider
```

If your Cloudflare Tunnel runs on the same host as the container, the routing is a single hop. If they're on separate hosts (often the case for a multi-VM lab), the tunnel host needs network access to the app host on port 8088.

## Cloudflare Tunnel routing

On the host running `cloudflared`, edit `/etc/cloudflared/config.yml`:

```yaml
ingress:
  # ... existing rules above ...
  - hostname: mailtriage.example.com
    service: http://<app-host-ip>:8088
  - service: http_status:404
```

Restart `cloudflared`:

```bash
sudo systemctl restart cloudflared
```

In Cloudflare's dashboard, add a DNS record:
`mailtriage.example.com → CNAME → <tunnel-id>.cfargotunnel.com` (proxied).

## Firewall

Open `app-host:8088` to traffic from the tunnel host's IP only (the tunnel terminates TLS and forwards plain HTTP; you do not want this port reachable from the world). On a typical Linux host:

```bash
sudo ufw allow from <tunnel-host-ip> to any port 8088 proto tcp
```

Confirm from the tunnel host:

```bash
curl http://<app-host-ip>:8088/healthz
# {"status":"ok"}
```

## Restricting outbound from the app host

Optional but recommended. The container only needs to reach:

- `*.microsoft.com`, `*.microsoftonline.com`, `*.office.com`
- `api.anthropic.com` (if using Anthropic)
- `api.openai.com` (if using OpenAI)

Use whatever your environment supports — host iptables rules, cloud NSGs, etc.

## Compose deployment

```bash
ssh <app-host>
git clone https://github.com/<owner>/Mail-Triage.git ~/mail-triage
cd ~/mail-triage

cp .env.example .env
# Edit: TENANT_ID, CLIENT_ID, CLIENT_SECRET,
#       WEBHOOK_URL=https://mailtriage.example.com/graph-webhook,
#       WEBHOOK_CLIENT_STATE=$(openssl rand -hex 32),
#       LLM_PROVIDER=anthropic, LLM_MODEL=claude-haiku-4-5-20251001,
#       ANTHROPIC_API_KEY=sk-ant-...

cp config/mailboxes.example.txt  config/mailboxes.txt
cp config/rules.example.txt      config/rules.txt
cp config/categories.example.txt config/categories.txt
# Edit all three for your mailboxes, rules, and categories.

docker compose build
docker compose up -d
docker compose exec mailtriage python -m mailtriage.subscriptions create
```

## Cohabitation

The container does not share a network with anything else by default. It exposes only port 8088 (bound to `127.0.0.1`) and writes only to its own named docker volume (`mailtriage-data`). It can comfortably cohabit with other containers on the same host.

## Storage growth

The SQLite audit DB is the only mailtriage-side growth. Expect ~1KB per processed message. At ~50 messages/day, that's ~18MB/year.

## Backups (recommended)

```bash
# Audit DB and subscription state
docker run --rm -v mailtriage-data:/d -v /backup:/b alpine \
  tar czf /b/mailtriage-data-$(date +%F).tgz /d
```

If you also want to back up your config, include `config/mailboxes.txt`, `config/rules.txt`, and `.env` in your usual encrypted host backup.

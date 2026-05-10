# Setup Guide

End-to-end instructions to get Mail-Triage running on a fresh host.

## 1. Microsoft 365 prerequisites

You need:

- An M365 tenant where you can create an Entra app registration.
- Tenant admin (or Application Administrator + Exchange Administrator) to grant admin consent and apply the Application Access Policy.
- Exchange Online PowerShell installed locally (`Install-Module -Name ExchangeOnlineManagement`).

## 2. Create the Entra app registration

1. Go to **Entra admin center → Identity → Applications → App registrations → New registration**.
2. Name: `mailtriage`. Supported account types: *Single tenant*. No redirect URI needed.
3. After creation, capture the **Application (client) ID** and **Directory (tenant) ID**.

### Add Application API permissions

Under **API permissions → Add a permission → Microsoft Graph → Application permissions**:

| Permission | Why |
|---|---|
| `Mail.ReadWrite` | Read messages, move between folders |

Click **Grant admin consent for <tenant>**. The permission should show "Granted".

> Mail-Triage does **not** request `Mail.Send`. The service is read+move only and has no ability to send mail.

### Generate a client secret

Under **Certificates & secrets → Client secrets → New client secret**. Description: `mailtriage`, expiry: 24 months. **Copy the value immediately** — you cannot retrieve it later. This goes into `.env` as `CLIENT_SECRET`. Calendar a reminder to rotate it about a month before expiry.

## 3. Restrict the app to specific mailboxes

This is the security boundary. Without it, your app has read/write access across every mailbox in the tenant.

Microsoft's `ExchangeOnlineManagement` PowerShell module is the only supported way to create an Application Access Policy. **You don't need a Windows host** — PowerShell Core runs in Microsoft's official Linux Docker image, and the `./scripts/exo.sh` wrapper handles everything.

### One-time setup

1. Set the Exchange-related env vars in your `.env` (see `.env.example`):
   ```
   TENANT_ID=<from step 2 above>
   EXO_GROUP_ADDRESS=mailtriage-scope@your-tenant.onmicrosoft.com
   EXO_APP_ID=<client_id from step 2>
   EXO_CONTROL_MAILBOX=<some-other-real-mailbox-in-your-tenant>
   ```
2. Run the one-time setup. It creates an empty scope group and attaches the Application Access Policy to it. Pwsh prints a device-code URL the first time it runs; sign in as a tenant admin in your browser.
   ```bash
   ./scripts/exo.sh setup
   ```
3. Wait up to 60 minutes for policy propagation.

### Add or change mailboxes

`config/mailboxes.txt` is the canonical list. After editing it, reconcile the security group with one command — it computes the diff and adds/removes members to match:

```bash
./scripts/exo.sh sync
```

Add `-DryRun` to preview the diff without making changes. Add `-NoConfirm` to skip the per-removal prompt:

```bash
./scripts/exo.sh sync -DryRun
./scripts/exo.sh sync -NoConfirm
```

So **onboarding a new mailbox is two steps**: append a line to `config/mailboxes.txt`, then run `./scripts/exo.sh sync`. Offboarding is the same in reverse.

### Verify the policy

After initial setup AND after each sync, confirm the access boundary holds:

```bash
./scripts/exo.sh verify
```

Must report `Granted` for every member of `config/mailboxes.txt` and `Denied` for the `EXO_CONTROL_MAILBOX`. If the control is granted, the policy is not in effect — **stop and fix before continuing**.

### Notes on the pwsh wrapper

- The first invocation pulls Microsoft's `mcr.microsoft.com/powershell` image (~250MB). Subsequent runs reuse it.
- The `ExchangeOnlineManagement` module is installed once into a persistent docker volume (`mailtriage-exo-modules`), so subsequent runs don't reinstall.
- Sign-in is via OAuth device code on every run. No persistent credentials are stored.
- If you want an interactive pwsh session for ad-hoc work: `./scripts/exo.sh shell`

## 4. Create the destination folders

`config/categories.txt` defines which folders Mail-Triage will move messages into. The defaults expect these folders to exist **at the top level of each mailbox** (not inside Inbox):

- `Newsletters`
- `Notifications`
- `Receipts`
- `Triage`
- `Action Items`

The built-in `Junk Email` folder is used for the `spam` category — no need to create it.

The simplest way to create the folders: open Outlook on the web for each mailbox (or **Open another mailbox** for shared/delegated mailboxes), right-click the mailbox name, **Create new folder**.

You can configure M365 retention policies per folder if you want auto-deletion (e.g. 30-day retention on `Newsletters`).

If you change `config/categories.txt` to add or rename folders, remember to create the matching folders in each mailbox before restarting the service.

## 5. Get an LLM API key

Pick one provider:

- **Anthropic** — sign up at console.anthropic.com → API Keys → Create Key. Recommended; the Haiku 4.5 model is cheap and fast for classification.
- **OpenAI** — sign up at platform.openai.com → API Keys → Create new secret key. `gpt-4o-mini` (or a newer mini/nano on your account) is a good fit.

Paste the key into `.env` as either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Only the chosen provider's key is required.

## 6. Set up the public webhook URL

Microsoft Graph needs a publicly reachable HTTPS URL to deliver notifications. Common options:

- **Cloudflare Tunnel** — free for personal use, no inbound port required.
- **ngrok** — fine for testing.
- **Caddy / nginx reverse proxy** with a real cert in front of your container.

The URL must end in `/graph-webhook`. Example: `https://mailtriage.example.com/graph-webhook`.

> If you cannot expose any inbound HTTP at all, the only alternative is to switch the service to polling Graph on a timer. That is not implemented in v0.3.x — webhook is the only supported path.

## 7. Configure Mail-Triage

```bash
git clone https://github.com/<owner>/Mail-Triage.git
cd Mail-Triage

cp .env.example .env
cp config/mailboxes.example.txt  config/mailboxes.txt
cp config/rules.example.txt      config/rules.txt
cp config/categories.example.txt config/categories.txt
```

Edit `.env`:

- `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` from step 2.
- `WEBHOOK_URL` to your public HTTPS URL ending in `/graph-webhook`.
- `WEBHOOK_CLIENT_STATE` — generate a long random string: `openssl rand -hex 32`.
- `LLM_PROVIDER`, `LLM_MODEL`, and the matching API key.
- `CONCURRENCY` if you want more than one parallel worker (default 1).
- **Leave `DRY_RUN=true` for the first week.** The service will classify everything but not actually move messages. After you've reviewed the audit DB and tuned `config/rules.txt`, set `DRY_RUN=false` and run `docker compose up -d` (NOT `docker compose restart` — restart keeps the container's baked-in env vars; only `up -d` re-reads `.env`).

Edit `config/mailboxes.txt` — one address per line.

Edit `config/rules.txt` — start with the examples and add your own. Order matters; first match wins.

Edit `config/categories.txt` — only if you want to add or change categories.

## 8. Build and run

```bash
docker compose build
docker compose up -d
docker compose logs -f mailtriage
```

You should see:

- `loaded N categories: [...]`
- `monitoring N unique mailboxes: [...]`
- `started N worker(s)`
- `Tier 2 classifier: provider=... model=...`
- `Application startup complete.`

## 9. Create the Graph subscriptions

```bash
docker compose exec mailtriage python -m mailtriage.subscriptions create
```

You should see one `created subscription` line per unique underlying mailbox. State is written to `/var/lib/mailtriage/subscriptions.json`.

## 10. Send a test email and verify

Send yourself an email. Within a few seconds:

```bash
# Read-only status report
docker compose exec mailtriage python -m mailtriage.status

# Or look at raw rows
docker compose exec mailtriage sqlite3 /var/lib/mailtriage/audit.db \
  "SELECT timestamp, mailbox, sender, subject, source, category, action FROM decisions ORDER BY id DESC LIMIT 5"
```

## You're done

Subscription auto-renewal happens every 12 hours inside the container. Run `python -m mailtriage.status` whenever you want a digest. Watch what shows up in `/Triage` for the first few days, tune `config/rules.txt` accordingly, and you're set.

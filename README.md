<p align="center">
  <img src="branding/modern-3-monogram-mt.svg" alt="Mail-Triage" width="200">
</p>

<h1 align="center">Mail-Triage</h1>

Headless inbox triage for Microsoft 365 mailboxes. Classifies every new message with a deterministic rules file (Tier 1) and an LLM (Tier 2 — Anthropic, OpenAI, or any OpenAI-compatible server, including local models like Ollama), then moves it into the matching folder. Built for self-hosting and configured entirely through plain-text files.

> Mail never moves between mailboxes. Mail-Triage is a folder sorter, not a router.
>
> The service has **no ability to send email**. The Entra app uses `Mail.ReadWrite` and `MailboxSettings.Read` only — `Mail.Send` is intentionally not granted.

## Why

Defender / Exchange Online Protection catches volumetric spam well, but is bad at the gray-mail problem: newsletters, receipts, machine notifications, and the occasional buried "this needs your attention." Outlook rules are tedious to maintain and can't reason about content. Modern LLMs are excellent at semantic classification.

Mail-Triage bolts a thin classification layer onto your existing mailbox: every new email runs through a fast deterministic rules file first; only the leftovers go to an LLM. Each classified message moves into a folder of your choosing, recorded in a local audit log.

## Features

- **Dry-run mode.** Set `DRY_RUN=true` to validate behavior against real mail without anything being moved. Audit rows are written as normal; only the actual `move_message` Graph call is skipped. Recommended for the first week of operation.
- **Multi-mailbox.** One service handles many mailboxes (your personal, a partner's, a shared mailbox, etc.) within a single Microsoft 365 tenant.
- **Plain-text configuration.** Mailboxes, rules, and categories live in `config/*.txt` files you edit by hand. No DB schema, no admin UI.
- **Categories are configurable.** Add a new category (e.g. `promotions` → `/Promotions`) by adding a line to `config/categories.txt` and restarting. No code changes.
- **Pluggable LLM provider.** Anthropic Claude, OpenAI GPT, or any OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM, LocalAI, OpenRouter, Groq, …). Hosted models use prompt caching for low cost; local models are free.
- **Pluggable backends.** Storage (`sqlite` today; protocol-ready for `postgres`) and queue (`memory` today; protocol-ready for `redis`) sit behind interfaces, so you can scale up without rewriting.
- **Configurable parallelism.** `CONCURRENCY=N` runs N parallel Tier-2 workers reading from the same fair queue.
- **Hard cross-mailbox guarantee.** A safety check in the dispatcher refuses to move a message into a folder belonging to a different mailbox. Tested adversarially. Runs even in dry-run mode.
- **Move-only.** No replies, forwards, deletes, or composes. Use M365 retention policies for lifecycle.
- **Locked-down LLM.** The classifier's output is a JSON enum, schema-validated. The model has no tools and no network access of its own. Worst-case from a successful prompt injection: wrong folder.
- **Idempotent.** Duplicate webhook deliveries are detected and skipped via the audit log.
- **Read-only status command.** `python -m mailtriage.status` prints a digest of recent activity from the audit DB, on demand. No email is sent.

## Architecture

```
Microsoft Graph webhook ──► FastAPI receiver ──► JobQueue (per-mailbox fair)
                                                       │
                                                       ▼
                                       worker(s) — N in parallel
                                                       │
                                                       ▼
                                           Tier 1 rules (rules.txt)
                                                       │
                                                       ▼ (if no match)
                                            Tier 2: LLM classifier
                                                       │
                                                       ▼
                                       Dispatcher (move within mailbox)
                                                       │
                                                       ▼
                                          Storage (audit log)
```

A single Python process runs the webhook receiver, N parallel workers, and an internal scheduler for subscription renewal. Everything else is a library.

### Components

| Concern | Implementation |
|---|---|
| Audit storage | `SqliteStorage` (`src/mailtriage/storage.py`) — single-host SQLite in WAL mode |
| Work queue | `FairQueue` (`src/mailtriage/queue.py`) — in-memory, per-mailbox round-robin |
| LLM provider | `AnthropicProvider` and `OpenAIProvider` (`src/mailtriage/classifier.py`) |
| Categories | `config/categories.txt` — plain text, parsed at startup |

## Requirements

- A Microsoft 365 tenant where you can register an Entra app and apply an Application Access Policy.
- A host with Docker and a publicly reachable HTTPS URL (Cloudflare Tunnel, ngrok, a reverse proxy, etc.).
- An API key for either Anthropic or OpenAI.

## Quick start

```bash
git clone https://github.com/<owner>/Mail-Triage.git
cd Mail-Triage

# 1. Set up the Entra app and Application Access Policy (see docs/SETUP.md)

# 2. Configure
cp .env.example .env
cp config/mailboxes.example.txt config/mailboxes.txt
cp config/rules.example.txt      config/rules.txt
cp config/categories.example.txt config/categories.txt
# Edit those four files. Leave DRY_RUN=true in .env for the first week.

# 3. Create the destination folders manually in each mailbox (Outlook web).
#    The default categories.txt expects:
#      /Newsletters, /Notifications, /Receipts, /Triage, /Action Items
#    Plus the built-in /Junk Email folder is used for spam.

# 4. Build and start
docker compose build
docker compose up -d

# 5. Create Graph subscriptions for each mailbox
docker compose exec mailtriage python -m mailtriage.subscriptions create

# 6. Send a test email; check status
docker compose exec mailtriage python -m mailtriage.status
```

Detailed setup is in [docs/SETUP.md](docs/SETUP.md).

## Configuration

### `config/mailboxes.txt`

One email address per line. Aliases of the same underlying mailbox are deduplicated automatically (each address is resolved to its primary UPN via Microsoft Graph; only one subscription is created per unique mailbox). Comments start with `#`.

```
# Primary mailboxes
alice@example.com
bob@example.com

# Shared mailbox
team@example.com
```

### `config/categories.txt`

Defines what categories exist, what folder each one moves to, and how the LLM should think about each one. Format:

```
<category-name>  <folder-name>  | <description>
```

- `<category-name>` is what rules and the LLM emit. Lowercase, alphanumeric + hyphen + underscore. Must be unique per file.
- `<folder-name>` is the M365 folder display name. The literal value `Inbox` means "leave the message in Inbox" (no move).
- `<description>` is shown to the LLM in the system prompt.

A `triage` category is **required** — it's the fallback when classification fails or returns an unknown value.

To add a new category:

1. Add a line to `config/categories.txt`.
2. Create the matching folder in each monitored mailbox.
3. `docker compose restart mailtriage`.

No code change. No rebuild.

### `config/rules.txt`

One rule per line. First match wins. Rules apply to all mailboxes unless prefixed with `mailbox:<address>`.

```
# Format: [mailbox:<address>] <type> <pattern> -> <category>
#
# Match types:
#   from               <full-email>           exact sender match
#   from-domain        <domain>               sender's domain
#   subject-contains   <substring>            case-insensitive substring in subject
#   subject-regex      <python regex>         regex against subject
#   header-exists      <header-name>          message has this header
#   to                 <full-email>           exact recipient (useful for aliases)
#   self-loop                                 from this mailbox to itself

# Universal rules
header-exists List-Unsubscribe -> newsletter
from-domain noreply.github.com -> notification

# Receipts
from-domain amazon.com         -> receipt
subject-regex ^Order #\d+      -> receipt

# Mailbox-specific
mailbox:alice@example.com from-domain bank.com -> action-item
```

### `.env`

```
TENANT_ID=...
CLIENT_ID=...
CLIENT_SECRET=...

WEBHOOK_URL=https://your-public-host/graph-webhook
WEBHOOK_CLIENT_STATE=<long random string>

LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-4-5-20251001
ANTHROPIC_API_KEY=sk-ant-...

CONCURRENCY=1
DRY_RUN=true                                 # set to false once you've validated behavior
AUDIT_RETENTION_DAYS=90
```

## LLM provider selection

| `LLM_PROVIDER` | Use for | Recommended model | Pricing notes |
|---|---|---|---|
| `anthropic` | Claude API | `claude-haiku-4-5-20251001` | System prompt explicitly cached (~90% off cached input). |
| `openai` | OpenAI API | `gpt-4o-mini` | System prompt auto-cached above 1024 tokens (~50% off). |
| `openai-compatible` | Any OpenAI-compatible server: Ollama, LM Studio, llama.cpp, vLLM, LocalAI, LiteLLM proxy, OpenRouter, Together, Groq, … | depends on server (e.g. `llama3.1:8b` for Ollama) | Local models = $0/request. Hosted gateways bill normally. |

Switch by editing `LLM_PROVIDER` and `LLM_MODEL` in `.env` and restarting (`docker compose up -d`, not `restart`). No code change.

### Using a local model (or any OpenAI-compatible server)

Set `LLM_PROVIDER=openai-compatible` and point `LLM_BASE_URL` at your server's `/v1` endpoint:

```env
LLM_PROVIDER=openai-compatible
LLM_MODEL=llama3.1:8b
LLM_BASE_URL=http://host.docker.internal:11434/v1   # Ollama on the Docker host
OPENAI_API_KEY=                                     # leave empty for local servers
LLM_RESPONSE_FORMAT_JSON=false                      # toggle off if your server doesn't support JSON mode
```

Common base URLs:

| Server | `LLM_BASE_URL` (from inside the container) |
|---|---|
| Ollama (Linux host) | `http://host.docker.internal:11434/v1` |
| LM Studio | `http://host.docker.internal:1234/v1` |
| llama.cpp `./server` | `http://host.docker.internal:8080/v1` |
| vLLM | `http://your-vllm-host:8000/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` (set `OPENAI_API_KEY`) |
| Groq | `https://api.groq.com/openai/v1` (set `OPENAI_API_KEY`) |

The classifier's parser tolerates JSON-in-text output, so smaller local models that don't honor `response_format={"type":"json_object"}` still work — just set `LLM_RESPONSE_FORMAT_JSON=false`.

## First-time setup (DRY-RUN)

When `DRY_RUN=true` (the default in `.env.example`), Mail-Triage runs the full pipeline — webhook → queue → Tier 1 rules → Tier 2 LLM → cross-mailbox guard — but does **not** call `graph.move_message`. Every classification is recorded in the audit DB with `action='dry-run'`.

Recommended sequence:

1. Set `DRY_RUN=true` in `.env`. Start the service and let real mail flow through it for several days.
2. Review what the service would have done:
   ```bash
   docker compose exec mailtriage python -m mailtriage.status
   ```
   The output highlights how many decisions were dry-run. Anything in `/Triage` with `category='triage'` is a row the LLM was unsure about — review it manually and either adjust `config/rules.txt` to handle similar mail deterministically or accept that triage is the right answer for that pattern.
3. When you're satisfied, set `DRY_RUN=false` (or remove the line) and **`docker compose up -d`** to recreate the container. Live action begins.

> **Important:** use `docker compose up -d`, NOT `docker compose restart`, when changing values in `.env`. `restart` keeps the container's original environment variables (baked in at first `up`); only `up -d` re-reads `env_file`. Volume-mounted config files (`config/*.txt`) are re-read by either command, but env-var changes need recreation.

Mail that was previously processed in dry-run mode stays in the Inbox — it isn't re-classified after you flip the switch. Only new mail (next webhook delivery) is affected.

## Scaling

| Workload size | What to do |
|---|---|
| 1–5 mailboxes, normal personal volume | Default config (`CONCURRENCY=1`) |
| 10+ mailboxes, or higher volume | `CONCURRENCY=4` (or higher) — runs N parallel workers behind the same fair queue |

The webhook receiver is stateless and round-trips a 202 in milliseconds. The limiting factor at scale is the worker pool size and your LLM provider rate limits. Storage and queue are intentionally single-process (SQLite + in-memory `FairQueue`); if you outgrow that, the right move is a different service rather than swapping backends.

## Cross-mailbox safety

The dispatcher refuses to move a message into a folder belonging to a different mailbox than the source. Enforced in `src/mailtriage/dispatcher.py` and tested in `tests/test_dispatcher_guards.py`. If a misconfiguration tries to route mail across mailboxes, the move is aborted, the message stays in the inbox, and an audit row is written with `action=aborted`.

The Tier 2 prompt does not see other mailboxes; it only sees the current message's content and returns a category from a fixed enum.

## Subscription renewal

Microsoft Graph subscriptions expire roughly every 70 hours. The container's internal scheduler renews them every 12 hours. If renewal fails, the next run will attempt re-creation.

## Audit DB retention

The audit log grows by ~1KB per processed message. By default a daily job at 03:00 local time deletes rows older than `AUDIT_RETENTION_DAYS` (default 90). Set to `0` to disable purging entirely (the audit DB will grow monotonically).

The retention job does not run `VACUUM` — that briefly takes an exclusive lock and would block writers. After a large purge, run it manually if you want to reclaim disk space:

```bash
docker compose exec mailtriage sqlite3 /var/lib/mailtriage/audit.db "VACUUM"
```

Note: a row's presence in the audit DB is also what blocks duplicate webhook deliveries from being re-processed. Retention shorter than your worst-case Graph webhook retry window (minutes) could allow a redelivered notification to replay across the boundary. The 90-day default is comfortably safe.

## Folder-rename auto-recovery

If a target folder is deleted and recreated server-side (different folder_id, same display name), the dispatcher's cached folder_map will hold the dead id. On the next move attempt Graph returns 404, the dispatcher refreshes the folder map, and retries once with the new id. The cross-mailbox guard runs against the refreshed map on the retry.

Renaming a folder (without deleting) does not require any recovery — folder ids are stable across rename, so moves continue to land in the same (renamed) folder. If you want to point a category at a different folder name, edit `config/categories.txt` and restart.

## Status / auditing

`mailtriage status` is a read-only CLI that summarizes recent activity from the audit DB. No email is sent.

```bash
# Last 24h, all mailboxes
docker compose exec mailtriage python -m mailtriage.status

# Last week, single mailbox
docker compose exec mailtriage python -m mailtriage.status --hours 168 --mailbox alice@example.com

# Direct SQL (advanced)
docker compose exec mailtriage sqlite3 /var/lib/mailtriage/audit.db \
  "SELECT timestamp, mailbox, sender, category, source, action FROM decisions ORDER BY id DESC LIMIT 50"
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Test count: 130+. Areas covered: rules parser, categories parser/validator, cross-mailbox guard (adversarial), classifier output parser, header sanitization (prompt-injection defenses), webhook resource parser, audit DB connection lifecycle and idempotency semantics, queue dedup and fairness, subscription state stale-detection and atomic save, retention purge, stale-folder retry, config validators, BOM handling, lone-surrogate handling.

## Security

- App registration uses **Application permissions** — `Mail.ReadWrite` only. **`Mail.Send` is intentionally not granted.** The service has no ability to send mail.
- Permissions are scoped by an **Application Access Policy** in Exchange Online. Without that policy, the app could read/write across every mailbox in the tenant. Setup includes a verification step.
- API keys live in `.env`, which is gitignored.
- Container runs as a non-root user with no host filesystem mounts except a named data volume.
- LLM output is constrained to a fixed enum. The model has no tools.
- Header fields (From name/address, To, Subject) are sanitized before being shown to the model — Unicode-normalized, control characters stripped, wrapper-tag literals defanged, length-capped.
- Mail destination decisions are verified against the source mailbox before each move.

See [docs/SECURITY.md](docs/SECURITY.md) for the full threat model.

## Project status

Active development. Tested on Debian 13 with Docker. Should run anywhere Python 3.11+ and Docker run.

Future enhancements:

- Auto-seed allowlist from Sent Items + Contacts
- Drift detection — observe manual moves and propose new rules
- Web UI for the `/Triage` queue
- Per-mailbox classifier prompts

Pull requests welcome.

## License

[MIT](LICENSE).

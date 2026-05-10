# Security Model

## Threat model

Mail-Triage runs as a long-lived process with three sensitive credentials:

- An Entra app's client secret with `Mail.ReadWrite`, scoped via Application Access Policy to specific mailboxes. **`Mail.Send` is intentionally not granted** — the service has no ability to send email.
- An LLM API key (Anthropic or OpenAI), which can run inference and incur cost on the configured account.
- The webhook `clientState` secret, used to authenticate inbound notifications from Microsoft Graph.

It exposes one public HTTPS endpoint: the Graph webhook receiver.

## What the LLM can and cannot do

- The Tier 2 classifier sends each ambiguous message to the chosen provider's chat completion API. The model has no tool / function-calling configured. It cannot invoke external code, read files, or make network requests of its own.
- The classifier system prompt requires a strict JSON output. The output is parsed and validated against the categories defined in `config/categories.txt`.
- If the model returns anything outside that set (including invented categories or attempts to obey instructions inside the email body), the result is coerced to `triage` and the message is moved into the human-review folder.
- The dispatcher does not consult the LLM. It only reads the validated category enum and looks up a folder by name. Even a successful prompt injection cannot do anything other than choose a different category from a fixed list.

## Header-based prompt-injection defense

Every header-derived field (From display name, From address, every recipient, Subject) is sanitized before being shown to the model:

- NFKC-normalized, so lookalike characters cannot bypass case-insensitive defang.
- Control characters (newlines, tabs, zero-width, bidi-override) are replaced with a single space — replacing rather than dropping prevents distinct tokens from being silently merged.
- Any literal occurrence of the wrapper tags (`<untrusted_email>` / `</untrusted_email>`, case-insensitive) is replaced with `[redacted-tag]` so attacker content cannot pretend to close the wrapped block.
- Whitespace runs are collapsed.
- Each field is truncated to a per-field maximum (RFC 5321 for addresses, 100 for names, 500 for subject and combined recipient list).
- Recipient count is capped at 10 with a `(+N more)` marker.

The body sanitizer applies the same tag-defang and Unicode normalization but preserves newlines and tabs.

The system prompt itself instructs the model to treat every part of the wrapped block as data — From, To, Subject, and body — and to flag any field that looks like it is trying to escape the wrapper.

## Cross-mailbox guarantee

`src/mailtriage/dispatcher.py` verifies that every move's destination folder belongs to the same mailbox as the source message. Two checks:

1. The destination folder ID must be in the cached folder map for `user_id` (built from `list_folders(user_id)`).
2. The message's `parentFolderId` (when present) must also belong to `user_id`.

Violation raises `CrossMailboxError`, the move is aborted, an audit row is written with `action=aborted`, and the message stays in the inbox. Tested in `tests/test_dispatcher_guards.py`.

## Webhook authentication

- Microsoft Graph echoes a per-subscription `clientState` secret with every notification. The receiver compares it to the value stored in `.env`.
- A spoofed notification without the correct `clientState` is silently dropped and logged.
- The webhook should be reached only over HTTPS (Cloudflare Tunnel, reverse proxy with TLS, etc.). The container's port is published to `127.0.0.1:8088` per `docker-compose.yml`, so the listening socket inside the container's network namespace is not reachable from anywhere outside the host.

## Idempotency

Each notification is checked against the audit log before processing — if the message ID has already been recorded with a successful action, the processing path is skipped and a `skipped-duplicate` row is recorded. This prevents re-processing on Graph webhook retries and on multi-worker race conditions.

## Application Access Policy

This is the most important security boundary. Without it, the app's `Mail.ReadWrite` permission applies to every mailbox in the tenant. With it, the app can only read / write for the mailboxes named in the scope group.

The setup guide forces verification with `Test-ApplicationAccessPolicy`. The verification script (`scripts/test-app-access-policy.ps1`) must show `Denied` for a control non-target mailbox before deployment proceeds. Re-run the verification quarterly.

## Container hardening

- Runs as non-root (`mailtriage`, UID 1000).
- No host filesystem mounts except a named docker volume for the audit DB.
- Only port 8088 exposed, bound to `127.0.0.1` per `docker-compose.yml`.
- For additional defense, restrict outbound rules at the host firewall to:
  - `*.microsoft.com`, `*.microsoftonline.com`, `*.office.com` (Graph + Entra)
  - `api.anthropic.com` (if using Anthropic) or `api.openai.com` (if using OpenAI)

## Secrets handling

- `.env` is gitignored. Never commit it.
- `config/mailboxes.txt`, `config/rules.txt`, `config/categories.txt` are gitignored — they may contain personal addresses and routing logic. Runtime state (`audit.db`, `subscriptions.json`) lives in the `mailtriage-data` named docker volume at `/var/lib/mailtriage/`, not in `config/`.
- API keys live only in `.env` and the container's environment. They are not written to logs or the audit DB.

## Backups

Recommended (not provided in v1):

- Nightly backup of `/var/lib/mailtriage/` to encrypted blob storage.
- Calendar reminder for client secret rotation about a month before its 24-month expiry.

## Incident response

If the host is suspected compromised:

1. Revoke the Entra client secret in the app registration. Mail access stops within minutes (no new tokens issuable).
2. Rotate the LLM API key in the provider's console.
3. Delete Graph subscriptions: `docker compose exec mailtriage python -m mailtriage.subscriptions delete`.
4. Inspect the audit DB for any abnormal moves before and during the suspected window.

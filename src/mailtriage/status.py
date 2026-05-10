"""On-demand status report — read-only view of the audit DB.

Replaces the old daily-summary email. Run from the host:

  docker compose exec mailtriage python -m mailtriage.status
  docker compose exec mailtriage python -m mailtriage.status --hours 168
  docker compose exec mailtriage python -m mailtriage.status --mailbox alice@example.com
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from .config import get_settings, load_mailboxes
from .graph import GraphClient
from .storage import SqliteStorage


def _print_for_mailbox(storage, mailbox_addr: str, since_iso: str) -> None:
    rows = list(storage.recent(mailbox_addr, since_iso))
    cat_counts: Counter[str] = Counter(r.category for r in rows)
    action_counts: Counter[str] = Counter(r.action for r in rows)
    sender_counts: Counter[str] = Counter(r.sender or "(unknown)" for r in rows)
    triage_items = [r for r in rows if r.category == "triage" and r.action != "skipped-duplicate"]
    action_items = [r for r in rows if r.category == "action-item"]
    dry_runs = [r for r in rows if r.action == "dry-run"]
    errors = [r for r in rows if r.error]

    print(f"\n=== {mailbox_addr} ===")
    print(f"Window: since {since_iso}  ({len(rows)} decisions)")
    if dry_runs:
        print(f"  [{len(dry_runs)} of these were DRY-RUN — no messages were actually moved]")

    if not rows:
        print("  (no activity)")
        return

    print("\nCounts by category:")
    for cat, n in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cat:<14} {n}")

    print("\nCounts by action:")
    for act, n in sorted(action_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {act:<22} {n}")

    print("\nTop senders:")
    for sender, n in sender_counts.most_common(10):
        print(f"  {n:>4}  {sender}")

    if triage_items:
        print(f"\nIn /Triage ({len(triage_items)}):")
        for r in triage_items[-20:]:
            print(f"  - {r.sender or '(unknown)'}")
            print(f"    Subject: {r.subject or '(none)'}")
            if r.reasoning:
                print(f"    Reason:  {r.reasoning}")

    if action_items:
        print(f"\nIn /Action Items ({len(action_items)}):")
        for r in action_items[-10:]:
            print(f"  - {r.sender}: {r.subject}")

    if errors:
        print(f"\nErrors: {len(errors)}")
        for r in errors[-5:]:
            print(f"  [{r.timestamp}] {r.error}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet
    p = argparse.ArgumentParser(description="Print mailtriage activity status")
    p.add_argument("--hours", type=int, default=24, help="Lookback window (default 24)")
    p.add_argument("--mailbox", help="Limit to one mailbox address (default: all)")
    args = p.parse_args()

    s = get_settings()
    storage = SqliteStorage(s.audit_db)

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    if args.mailbox:
        _print_for_mailbox(storage, args.mailbox.lower(), since_iso)
    else:
        # Resolve all configured mailboxes to their primary UPNs and print one
        # report per mailbox. Read-only Graph access (no send).
        graph = GraphClient()
        mailboxes = graph.resolve_mailboxes(load_mailboxes(s.mailboxes_file))
        for mbx in mailboxes:
            _print_for_mailbox(storage, mbx.primary_upn, since_iso)


if __name__ == "__main__":
    main()

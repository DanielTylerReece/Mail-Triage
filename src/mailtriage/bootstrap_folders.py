"""Idempotently create the destination folders required by categories.txt
in every mailbox listed in mailboxes.txt.

Useful during initial deployment and whenever you onboard a new mailbox —
saves clicking through Outlook web for each one.

Usage:
    docker compose exec mailtriage python -m mailtriage.bootstrap_folders
    docker compose exec mailtriage python -m mailtriage.bootstrap_folders --dry-run

Folders are created at the TOP LEVEL of each mailbox (not under Inbox).
Folders that already exist are left alone. Folders whose value in
categories.txt is the `Inbox` sentinel are skipped (no folder needed —
'keep' just means leave-in-Inbox).

Mailboxes that don't resolve (e.g. a shared mailbox that isn't
provisioned yet) are skipped with a warning.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import categories as cats
from .config import get_settings, load_mailboxes
from .graph import GraphClient, GraphError

log = logging.getLogger("mailtriage.bootstrap_folders")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be created without making any changes.")
    args = p.parse_args()

    s = get_settings()
    cats.load()
    needed = sorted({c.folder for c in cats.load() if not c.is_keep})
    if not needed:
        log.info("no folders to create (every category resolves to Inbox)")
        return 0

    log.info("required folders: %s", needed)
    g = GraphClient()
    addresses = load_mailboxes(s.mailboxes_file)

    total_created = 0
    total_existing = 0
    total_failed = 0
    skipped_mailboxes: list[str] = []

    for addr in addresses:
        try:
            mbx = g.resolve_mailbox(addr)
        except GraphError as e:
            log.warning("skipping %s: resolve failed (%d) -- mailbox may not exist",
                        addr, e.status)
            skipped_mailboxes.append(addr)
            continue

        existing = set(g.list_folders(mbx.user_id).keys())
        log.info("%s — %d existing top-level folders", mbx.primary_upn, len(existing))

        for name in needed:
            if name in existing:
                log.info("  exists  : %s", name)
                total_existing += 1
                continue
            if args.dry_run:
                log.info("  WOULD CREATE: %s", name)
                continue
            try:
                g._post(f"/users/{mbx.user_id}/mailFolders", {"displayName": name})
                log.info("  CREATED : %s", name)
                total_created += 1
            except GraphError as e:
                log.error("  FAIL    : %s status=%d", name, e.status)
                total_failed += 1

    log.info("done — created=%d existed=%d failed=%d skipped_mailboxes=%s",
             total_created, total_existing, total_failed, skipped_mailboxes or "none")
    g.close()
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())

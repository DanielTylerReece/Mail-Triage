"""Create and renew Graph webhook subscriptions for each configured mailbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dateutil import parser as dtparser

from .config import get_settings, load_mailboxes
from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)

# Microsoft Graph caps mail subscriptions at 4230 minutes (~70.5 hours).
SUBSCRIPTION_DURATION_MINUTES = 4000

# Treat a stored subscription as stale (and ignore it for create/renew
# decisions) if it is closer than this many minutes to its stored expiry.
STALE_BUFFER_MINUTES = 30


def _expiration() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=SUBSCRIPTION_DURATION_MINUTES)).isoformat()


def _is_stale(expiration_iso: str | None) -> bool:
    """True if the stored expiration is missing, malformed, or within the
    stale buffer. We must recreate stale subscriptions even if state has them."""
    if not expiration_iso:
        return True
    try:
        # dateutil.parser.isoparse handles Graph's variable fractional-second
        # precision (Graph sometimes returns 7 digits, which stdlib
        # datetime.fromisoformat refuses).
        exp = dtparser.isoparse(expiration_iso)
    except (ValueError, TypeError):
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= datetime.now(timezone.utc) + timedelta(minutes=STALE_BUFFER_MINUTES)


def _load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("subscriptions.json is corrupt (%s); starting fresh", e)
        backup = path.with_suffix(".json.corrupt")
        try:
            path.replace(backup)
            log.error("corrupt state moved aside to %s", backup)
        except OSError:
            pass
        return {}


def _save_state(path: Path, state: dict[str, dict]) -> None:
    """Atomic save: write to .tmp then os.replace (atomic on POSIX)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def _adopt_orphans(graph: GraphClient, state: dict[str, dict],
                   resource_to_user: dict[str, str]) -> dict[str, dict]:
    """Look up live subscriptions on Graph and adopt any whose `resource`
    targets a configured mailbox but isn't in local state. Prevents the
    'process killed mid-create → zombie subscription on next create'
    failure mode."""
    s = get_settings()
    try:
        live = graph.list_subscriptions()
    except GraphError as e:
        log.warning("could not list live subscriptions for adoption: %s", e)
        return state
    for sub in live:
        if sub.get("notificationUrl") != s.webhook_url:
            continue
        resource = sub.get("resource", "")
        user_id = resource_to_user.get(resource)
        if not user_id:
            continue
        if user_id in state and state[user_id].get("subscription_id") == sub["id"]:
            continue
        log.info("adopting existing subscription %s for user %s", sub["id"], user_id)
        state[user_id] = {
            "subscription_id": sub["id"],
            "expiration": sub.get("expirationDateTime", ""),
            "primary_upn": state.get(user_id, {}).get("primary_upn", ""),
        }
    return state


def create() -> None:
    s = get_settings()
    graph = GraphClient()
    mailboxes = graph.resolve_mailboxes(load_mailboxes(s.mailboxes_file))
    state = _load_state(s.subscriptions_state)

    # Adopt any existing server-side subscriptions for our configured
    # mailboxes that aren't in local state (recover from prior crash).
    resource_to_user = {
        f"/users/{m.user_id}/mailFolders('Inbox')/messages": m.user_id
        for m in mailboxes
    }
    state = _adopt_orphans(graph, state, resource_to_user)

    for mbx in mailboxes:
        existing = state.get(mbx.user_id)
        # Always update primary_upn — adoption may not have known it.
        if existing:
            existing.setdefault("primary_upn", mbx.primary_upn)
            existing["primary_upn"] = mbx.primary_upn

        if existing and not _is_stale(existing.get("expiration")):
            log.info("subscription already exists for %s; skipping", mbx.primary_upn)
            continue
        if existing:
            log.info("stale subscription for %s; replacing", mbx.primary_upn)
        try:
            sub = graph.create_inbox_subscription(mbx.user_id, _expiration())
            state[mbx.user_id] = {
                "subscription_id": sub["id"],
                "expiration": sub["expirationDateTime"],
                "primary_upn": mbx.primary_upn,
            }
            _save_state(s.subscriptions_state, state)
            log.info("created subscription %s for %s", sub["id"], mbx.primary_upn)
        except GraphError as e:
            log.error("failed to create subscription for %s: %s", mbx.primary_upn, e)


def renew() -> None:
    s = get_settings()
    graph = GraphClient()
    state = _load_state(s.subscriptions_state)

    for user_id, info in list(state.items()):
        try:
            updated = graph.renew_subscription(info["subscription_id"], _expiration())
            info["expiration"] = updated["expirationDateTime"]
            log.info("renewed subscription for %s until %s",
                     info["primary_upn"], info["expiration"])
            _save_state(s.subscriptions_state, state)
        except GraphError as e:
            log.error("failed to renew subscription for %s: %s -- recreating",
                      info["primary_upn"], e)
            try:
                sub = graph.create_inbox_subscription(user_id, _expiration())
                info["subscription_id"] = sub["id"]
                info["expiration"] = sub["expirationDateTime"]
                _save_state(s.subscriptions_state, state)
                log.info("recreated subscription for %s", info["primary_upn"])
            except GraphError as e2:
                log.error("recreate failed for %s: %s -- removing from state so a "
                          "future `subscriptions create` will rebuild it",
                          info["primary_upn"], e2)
                state.pop(user_id, None)
                _save_state(s.subscriptions_state, state)


def delete_all() -> None:
    s = get_settings()
    graph = GraphClient()
    state = _load_state(s.subscriptions_state)
    for user_id, info in list(state.items()):
        try:
            graph.delete_subscription(info["subscription_id"])
            log.info("deleted subscription for %s", info["primary_upn"])
        except GraphError as e:
            log.error("delete failed for %s: %s -- removing from local state",
                      info["primary_upn"], e)
        state.pop(user_id, None)
        _save_state(s.subscriptions_state, state)


def delete_one(target: str) -> None:
    """Delete one subscription by primary_upn or user_id."""
    s = get_settings()
    graph = GraphClient()
    state = _load_state(s.subscriptions_state)
    target_l = target.lower()
    matched: list[str] = []
    for user_id, info in list(state.items()):
        if user_id == target or info.get("primary_upn", "").lower() == target_l:
            matched.append(user_id)
    if not matched:
        log.error("no subscription found for %r in local state", target)
        return
    for user_id in matched:
        info = state[user_id]
        try:
            graph.delete_subscription(info["subscription_id"])
            log.info("deleted subscription for %s", info.get("primary_upn", user_id))
        except GraphError as e:
            log.error("delete failed for %s: %s", info.get("primary_upn", user_id), e)
        state.pop(user_id, None)
    _save_state(s.subscriptions_state, state)


def prune() -> None:
    """Delete any local-state subscription whose user_id is no longer in
    mailboxes.txt, AND any live Graph subscription pointing at our webhook
    URL whose user_id we don't recognize. Closes the 'I removed a mailbox
    from mailboxes.txt' orphan-leak gap."""
    s = get_settings()
    graph = GraphClient()
    state = _load_state(s.subscriptions_state)
    configured_addrs = load_mailboxes(s.mailboxes_file)
    configured_mbx = graph.resolve_mailboxes(configured_addrs)
    configured_user_ids = {m.user_id for m in configured_mbx}

    # 1. Remove local-state entries no longer in mailboxes.txt.
    for user_id in list(state.keys()):
        if user_id in configured_user_ids:
            continue
        info = state[user_id]
        try:
            graph.delete_subscription(info["subscription_id"])
            log.info("pruned local+remote subscription for %s", info.get("primary_upn", user_id))
        except GraphError as e:
            log.error("prune delete failed for %s: %s -- still removing from local state",
                      info.get("primary_upn", user_id), e)
        state.pop(user_id, None)
    _save_state(s.subscriptions_state, state)

    # 2. Find remote zombies (point at our webhook URL but have no mailbox config).
    try:
        live = graph.list_subscriptions()
    except GraphError as e:
        log.warning("could not list live subscriptions for prune: %s", e)
        return
    for sub in live:
        if sub.get("notificationUrl") != s.webhook_url:
            continue
        resource = sub.get("resource", "")
        # parse user_id from "/users/{id}/mailFolders('Inbox')/messages"
        parts = resource.strip("/").split("/")
        if len(parts) < 2 or parts[0].lower() != "users":
            continue
        user_id = parts[1]
        if user_id in configured_user_ids:
            continue
        if user_id in state:
            continue
        try:
            graph.delete_subscription(sub["id"])
            log.info("pruned remote zombie subscription %s for user %s", sub["id"], user_id)
        except GraphError as e:
            log.error("prune of remote zombie %s failed: %s", sub["id"], e)


def list_subs() -> None:
    """Print live subscription state for the operator. Read-only."""
    s = get_settings()
    graph = GraphClient()
    state = _load_state(s.subscriptions_state)
    print(f"Local state ({s.subscriptions_state}):")
    if not state:
        print("  (empty)")
    for user_id, info in state.items():
        stale = " STALE" if _is_stale(info.get("expiration")) else ""
        print(f"  user_id={user_id} upn={info.get('primary_upn','?')} "
              f"sub={info.get('subscription_id')} expires={info.get('expiration')}{stale}")
    print("\nLive Graph subscriptions for this app:")
    try:
        live = graph.list_subscriptions()
    except GraphError as e:
        print(f"  (graph error: {e})")
        return
    if not live:
        print("  (none)")
    for sub in live:
        marker = ""
        if sub.get("notificationUrl") != s.webhook_url:
            marker = " [different webhook URL]"
        print(f"  sub={sub.get('id')} resource={sub.get('resource')} "
              f"expires={sub.get('expirationDateTime')}{marker}")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Manage Graph webhook subscriptions")
    p.add_argument("action", choices=["create", "renew", "delete", "delete-one", "prune", "list"])
    p.add_argument("--mailbox", help="for delete-one: target mailbox by primary_upn or user_id")
    args = p.parse_args()
    if args.action == "delete-one":
        if not args.mailbox:
            p.error("--mailbox required with delete-one")
        delete_one(args.mailbox)
        return
    {
        "create": create,
        "renew": renew,
        "delete": delete_all,
        "prune": prune,
        "list": list_subs,
    }[args.action]()


if __name__ == "__main__":
    main()

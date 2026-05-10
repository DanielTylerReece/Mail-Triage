"""FastAPI webhook receiver for Microsoft Graph change notifications."""

from __future__ import annotations

import hmac
import logging
import re
import time
from typing import Any, Callable

from fastapi import FastAPI, Query, Request, Response

from .config import get_settings
from .queue import FairQueue, Job, QueueFullError

log = logging.getLogger(__name__)

# Hard cap on incoming webhook body. Real Graph notifications are tens of KB
# at most; anything beyond this is either a buggy upstream or hostile traffic.
MAX_BODY_BYTES = 64 * 1024

# Microsoft Graph message IDs are URL-safe base64 with `=` padding. They
# never contain slashes (path separators) or query separators (?, &, #, ;).
# We validate the parsed message_id against this character class before
# letting it flow into Graph URL construction. This blocks OData query
# injection via a forged or malformed `resource` field while still
# allowing the trailing `=` padding present in real Graph IDs.
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_\-=]+$")


def make_app(
    queue: FairQueue,
    allowed_user_ids: set[str] | None = None,
    health_provider: Callable[[], dict] | None = None,
) -> FastAPI:
    """Construct the webhook receiver.

    `allowed_user_ids`, if provided, is the defense-in-depth scope check.
    Notifications referencing user_ids not in this set are dropped at the
    edge — they never enter the queue, never trigger Graph fetches, and
    never write audit rows. The Application Access Policy in M365 is the
    primary boundary; this is the secondary one in code.

    `health_provider`, if provided, is called by /healthz and its dict is
    merged into the response. Lets callers expose queue depth, last
    decision timestamp, etc. without coupling the webhook module to them.
    """
    from . import __version__
    app = FastAPI(title="mailtriage", version=__version__)
    s = get_settings()
    # Pre-encode the secret to bytes so hmac.compare_digest works for any
    # operator-chosen string, including non-ASCII.
    expected_state_bytes = s.webhook_client_state.encode("utf-8")

    @app.get("/healthz")
    async def healthz() -> dict:
        body: dict = {"status": "ok"}
        if health_provider is not None:
            try:
                body.update(health_provider())
            except Exception:
                log.exception("health_provider failed")
                body["health_provider_error"] = True
        return body

    @app.post("/graph-webhook")
    async def graph_webhook(
        request: Request,
        validationToken: str | None = Query(default=None),
    ) -> Response:
        # Subscription handshake — Microsoft sends ?validationToken=... and expects it echoed.
        if validationToken is not None:
            log.info("webhook validation handshake (token len=%d)", len(validationToken))
            return Response(content=validationToken, media_type="text/plain", status_code=200)

        # Body size guard — read raw bytes first.
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            log.warning("webhook body exceeds %d bytes (got %d); dropping",
                        MAX_BODY_BYTES, len(raw))
            return Response(status_code=413)

        try:
            body = await request.json()
        except Exception:
            log.warning("webhook received non-JSON body; dropping")
            return Response(status_code=400)

        if not isinstance(body, dict):
            log.warning("webhook body is not a JSON object (got %s); dropping",
                        type(body).__name__)
            return Response(status_code=400)

        accepted = 0
        for note in body.get("value", []):
            if not isinstance(note, dict):
                continue

            # Constant-time clientState comparison via bytes — avoids the
            # ASCII-only restriction of hmac.compare_digest on str inputs.
            received_state = note.get("clientState") or ""
            try:
                received_state_bytes = str(received_state).encode("utf-8")
            except Exception:
                log.warning("webhook clientState not encodable; dropping")
                continue
            if not hmac.compare_digest(received_state_bytes, expected_state_bytes):
                log.warning("webhook clientState mismatch; dropping notification")
                continue

            user_id, message_id = _parse_resource(note.get("resource", ""))
            if not user_id or not message_id:
                log.warning("could not parse resource: %s", note.get("resource"))
                continue

            # Defense-in-depth: validate message_id shape before letting it
            # flow into Graph URL construction. Blocks OData query injection
            # via a malformed `resource` field. Allows base64-url charset
            # plus the `=` padding that real Graph IDs include.
            if not _MESSAGE_ID_RE.match(message_id):
                log.warning("rejected malformed message_id: %r", message_id[:80])
                continue

            # Defense-in-depth: drop notifications for user_ids that aren't
            # in our configured mailbox set. The Application Access Policy
            # in M365 is the primary boundary; this prevents the service
            # from operating on out-of-scope mailboxes if that policy is
            # misconfigured or its propagation is delayed.
            if allowed_user_ids is not None and user_id not in allowed_user_ids:
                log.warning("rejected notification for unconfigured user_id: %s", user_id)
                continue

            try:
                await queue.put(Job(user_id=user_id, message_id=message_id, notification=note))
                accepted += 1
                log.info("webhook accepted: user=%s msg=%s", user_id, message_id)
            except QueueFullError:
                log.warning(
                    "queue full; dropping notification user=%s msg=%s. "
                    "Microsoft will retry.",
                    user_id, message_id,
                )
                # 503 tells Microsoft we're temporarily unavailable. They
                # back off and redeliver. Better than 5xx-from-exception
                # which yields opaque 500.
                return Response(status_code=503)

        if accepted == 0 and body.get("value"):
            log.debug("webhook delivered %d notifications, all dropped",
                      len(body.get("value", [])))

        # Microsoft expects a 202 within 30s.
        return Response(status_code=202)

    return app


_RESOURCE_RE = re.compile(r"^/?[Uu]sers/([^/]+)/[Mm]ailFolders\([^)]*\)/[Mm]essages/([^/?]+)$")


def _parse_resource(resource: str) -> tuple[str | None, str | None]:
    """Extract (user_id, message_id) from a Graph notification resource path."""
    m = _RESOURCE_RE.match(resource.strip("/"))
    if m:
        return m.group(1), m.group(2)
    parts = resource.strip("/").split("/")
    if len(parts) >= 4 and parts[0].lower() == "users" and parts[-2].lower() == "messages":
        return parts[1], parts[-1]
    return None, None

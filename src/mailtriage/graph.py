"""Microsoft Graph wrapper. Application permissions only.

The app uses Mail.ReadWrite. There is intentionally no Mail.Send method
here — the service is read+move only and the Entra app should NOT have
Mail.Send granted."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import msal

from .config import get_settings

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

# Retry config for transient Graph failures (throttling, gateway issues).
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_MAX_ATTEMPTS = 3
_RETRY_DEFAULT_BACKOFF_S = 5.0
_RETRY_MAX_BACKOFF_S = 60.0


class GraphError(RuntimeError):
    """Graph API error.

    The full response body is captured but kept off the default str()
    representation to avoid leaking PII / tokens / mailbox content into
    logs and the audit DB. Use `.body` if you really need it.
    """

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}")


@dataclass
class Mailbox:
    """A resolved mailbox: address as listed in mailboxes.txt + the user it maps to."""

    listed_address: str
    user_id: str          # Graph user object id (immutable)
    primary_upn: str      # primary userPrincipalName (the canonical address)

    @property
    def key(self) -> str:
        return self.user_id


class GraphClient:
    def __init__(self) -> None:
        s = get_settings()
        secret = s.client_secret
        # SecretStr support — see config.py. Falls back to plain str for
        # back-compat with non-SecretStr settings.
        if hasattr(secret, "get_secret_value"):
            secret = secret.get_secret_value()
        self._app = msal.ConfidentialClientApplication(
            s.client_id,
            authority=f"https://login.microsoftonline.com/{s.tenant_id}",
            client_credential=secret,
        )
        self._token: str | None = None
        self._token_expiry: float = 0
        self._token_lock = threading.Lock()
        # Bound the connection pool so a thread-pool storm cannot hold
        # 100 sockets open against one host.
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        )

    def close(self) -> None:
        """Best-effort cleanup. Called from the main shutdown path."""
        try:
            self._http.close()
        except Exception:
            log.exception("httpx close failed")

    # ---------- auth ----------

    def _access_token(self) -> str:
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expiry - 60:
                return self._token
            result = self._app.acquire_token_for_client(scopes=SCOPE)
            if "access_token" not in result:
                # Restrict the surface of the error to known-safe keys.
                raise GraphError(
                    "POST", "msal/acquire_token_for_client",
                    500,
                    f"error={result.get('error')!r} desc={result.get('error_description')!r}",
                )
            self._token = result["access_token"]
            self._token_expiry = now + int(result.get("expires_in", 3600))
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}

    # ---------- low-level ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue a Graph request with bounded retry on transient failures
        (429/5xx). Honors the Retry-After header on the LAST attempt's
        response. Does not retry on 4xx (other than 429) — those are
        permanent client errors."""
        attempt = 0
        last_status = 0
        last_body = ""
        while True:
            attempt += 1
            h = self._headers()
            if "json" in kwargs:
                h["Content-Type"] = "application/json"
            try:
                r = self._http.request(method, f"{GRAPH_BASE}{path}", headers=h, **kwargs)
            except httpx.HTTPError as e:
                # Network-level failure: connect/read/pool timeout. Retry a few times.
                if attempt >= _RETRY_MAX_ATTEMPTS:
                    raise GraphError(method, path, 0, f"network: {type(e).__name__}: {e}")
                backoff = min(_RETRY_DEFAULT_BACKOFF_S * (2 ** (attempt - 1)), _RETRY_MAX_BACKOFF_S)
                log.warning("Graph %s %s network error (attempt %d/%d): %s; sleeping %.1fs",
                            method, path, attempt, _RETRY_MAX_ATTEMPTS, type(e).__name__, backoff)
                time.sleep(backoff)
                continue

            if r.status_code < 400:
                return r
            if r.status_code not in _RETRY_STATUSES or attempt >= _RETRY_MAX_ATTEMPTS:
                raise GraphError(method, path, r.status_code, r.text)

            # Honor Retry-After (seconds or HTTP-date); fall back to exponential.
            retry_after = r.headers.get("Retry-After")
            sleep_s = _retry_after_seconds(retry_after) if retry_after else None
            if sleep_s is None:
                sleep_s = min(_RETRY_DEFAULT_BACKOFF_S * (2 ** (attempt - 1)), _RETRY_MAX_BACKOFF_S)
            log.warning(
                "Graph %s %s -> %d (attempt %d/%d); sleeping %.1fs before retry",
                method, path, r.status_code, attempt, _RETRY_MAX_ATTEMPTS, sleep_s,
            )
            last_status = r.status_code
            last_body = r.text
            time.sleep(sleep_s)

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", path, **kwargs).json()

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        r = self._request("POST", path, json=json_body)
        return r.json() if r.text else {}

    def _patch(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        r = self._request("PATCH", path, json=json_body)
        return r.json() if r.text else {}

    def _delete(self, path: str) -> None:
        try:
            self._request("DELETE", path)
        except GraphError as e:
            if e.status == 404:
                return
            raise

    # ---------- mailbox resolution ----------

    def resolve_mailbox(self, address: str) -> Mailbox:
        data = self._get(f"/users/{address}?$select=id,userPrincipalName")
        return Mailbox(
            listed_address=address.lower(),
            user_id=data["id"],
            primary_upn=data["userPrincipalName"].lower(),
        )

    def resolve_mailboxes(self, addresses: list[str]) -> list[Mailbox]:
        seen: dict[str, Mailbox] = {}
        for addr in addresses:
            try:
                mbx = self.resolve_mailbox(addr)
            except GraphError as e:
                log.error("could not resolve mailbox %s: %s", addr, e)
                continue
            if mbx.key not in seen:
                seen[mbx.key] = mbx
            else:
                log.info("alias %s collapses to existing mailbox %s", addr, mbx.primary_upn)
        return list(seen.values())

    # ---------- folders ----------

    def list_folders(self, user_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        url = f"/users/{user_id}/mailFolders?$top=100"
        while url:
            data = self._get(url)
            for f in data.get("value", []):
                out[f["displayName"]] = f["id"]
            next_url = data.get("@odata.nextLink", "")
            if next_url.startswith("https://graph.microsoft.com/v1.0"):
                url = next_url[len("https://graph.microsoft.com/v1.0"):]
            else:
                if next_url:
                    log.warning("dropping nextLink to unexpected host: %s", next_url[:80])
                url = ""
        return out

    def get_message(self, user_id: str, message_id: str) -> dict[str, Any]:
        return self._get(
            f"/users/{user_id}/messages/{message_id}"
            "?$select=id,subject,bodyPreview,body,from,toRecipients,internetMessageHeaders,parentFolderId"
        )

    def move_message(self, user_id: str, message_id: str, destination_folder_id: str) -> dict[str, Any]:
        return self._post(
            f"/users/{user_id}/messages/{message_id}/move",
            {"destinationId": destination_folder_id},
        )

    # ---------- subscriptions ----------

    def list_subscriptions(self) -> list[dict[str, Any]]:
        """Return every subscription owned by this app's credentials.
        Used by `subscriptions create` to adopt orphans on retry."""
        data = self._get("/subscriptions")
        return data.get("value", [])

    def create_inbox_subscription(self, user_id: str, expiration_iso: str) -> dict[str, Any]:
        s = get_settings()
        client_state = s.webhook_client_state
        if hasattr(client_state, "get_secret_value"):
            client_state = client_state.get_secret_value()
        return self._post(
            "/subscriptions",
            {
                "changeType": "created",
                "notificationUrl": s.webhook_url,
                "resource": f"/users/{user_id}/mailFolders('Inbox')/messages",
                "expirationDateTime": expiration_iso,
                "clientState": client_state,
                "latestSupportedTlsVersion": "v1_2",
            },
        )

    def renew_subscription(self, subscription_id: str, expiration_iso: str) -> dict[str, Any]:
        return self._patch(
            f"/subscriptions/{subscription_id}",
            {"expirationDateTime": expiration_iso},
        )

    def delete_subscription(self, subscription_id: str) -> None:
        self._delete(f"/subscriptions/{subscription_id}")


def _retry_after_seconds(value: str) -> float | None:
    """Parse a Retry-After header value (delta-seconds or HTTP-date)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return min(float(value), _RETRY_MAX_BACKOFF_S)
    except ValueError:
        pass
    # HTTP-date fallback
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        delta = (dt.timestamp() - time.time())
        return max(0.0, min(delta, _RETRY_MAX_BACKOFF_S))
    except Exception:
        return None

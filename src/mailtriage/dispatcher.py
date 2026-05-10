"""Action dispatcher. Moves messages within a single mailbox.

CRITICAL: this module enforces the one absolute hard rule of mailtriage —
mail never crosses mailboxes. The CrossMailboxError is raised and the
move aborted if any code path tries to move a message into a folder that
does not belong to the source mailbox.

Category-to-folder mappings are loaded from categories.txt (via the
categories module). Adding a new category does not require code changes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from . import categories as cats
from .graph import GraphClient, GraphError

# Graph status codes for which a stale folder map is the most likely
# explanation. We refresh and retry once on these.
_STALE_MAP_STATUSES = (404, 410)

log = logging.getLogger(__name__)


class CrossMailboxError(RuntimeError):
    """Raised when a move would cross mailboxes. Refused operation, not retryable."""


class UnknownCategoryError(ValueError):
    """The category is not present in categories.txt."""


@dataclass
class FolderMap:
    """Cached lookup of a single mailbox's folders."""

    user_id: str
    by_name: dict[str, str]  # display_name -> folder_id
    folder_ids: set[str]      # all folder_ids belonging to this mailbox

    def folder_id_for(self, name: str) -> str | None:
        return self.by_name.get(name)

    def owns(self, folder_id: str) -> bool:
        return folder_id in self.folder_ids


class Dispatcher:
    def __init__(self, graph: GraphClient, *, dry_run: bool = False) -> None:
        self.graph = graph
        # In dry-run, dispatcher does everything (rule eval, folder lookup,
        # cross-mailbox guard) but skips the actual graph.move_message call.
        # The audit row records action='dry-run' so the operator can review
        # exactly what WOULD have happened.
        self.dry_run = dry_run
        self._maps: dict[str, FolderMap] = {}
        self._maps_lock = threading.Lock()

    def folder_map(self, user_id: str, refresh: bool = False) -> FolderMap:
        if not refresh and user_id in self._maps:
            return self._maps[user_id]
        with self._maps_lock:
            # Re-check inside the lock — another thread may have already
            # populated the cache while we were waiting.
            if not refresh and user_id in self._maps:
                return self._maps[user_id]
            by_name = self.graph.list_folders(user_id)
            fmap = FolderMap(
                user_id=user_id,
                by_name=by_name,
                folder_ids=set(by_name.values()),
            )
            self._maps[user_id] = fmap
            return fmap

    def dispatch(
        self,
        *,
        user_id: str,
        message_id: str,
        message: dict[str, Any],
        category: str,
    ) -> tuple[str, str | None]:
        """Returns (action, destination_folder_or_None).

        action is one of: 'kept' | 'moved' | 'aborted'.
        Raises UnknownCategoryError if the category is not in categories.txt.
        """
        folder_name = cats.folder_for(category)
        if folder_name is None:
            raise UnknownCategoryError(
                f"category {category!r} is not defined in categories.txt"
            )

        # Sentinel meaning "leave the message in Inbox". Case-insensitive.
        if cats.is_keep_folder(folder_name):
            return ("kept", None)

        # Refresh map if folder is missing (folder may have been created since last lookup).
        fmap = self.folder_map(user_id)
        dest_id = fmap.folder_id_for(folder_name)
        if dest_id is None:
            fmap = self.folder_map(user_id, refresh=True)
            dest_id = fmap.folder_id_for(folder_name)
        if dest_id is None:
            raise GraphError(
                "POST", f"/users/{user_id}/messages/{message_id}/move",
                404,
                f"target folder {folder_name!r} does not exist in mailbox {user_id} "
                "(create it in each monitored mailbox: see categories.txt)",
            )

        # === Cross-mailbox guard ===
        # The destination folder MUST belong to the same mailbox as the source.
        if not fmap.owns(dest_id):
            raise CrossMailboxError(
                f"refused to move message {message_id} in mailbox {user_id} to "
                f"folder {dest_id} — destination folder is not in this mailbox"
            )
        parent = message.get("parentFolderId")
        if parent and not fmap.owns(parent):
            raise CrossMailboxError(
                f"refused to move message {message_id} — its parent folder {parent} "
                f"does not belong to mailbox {user_id} (cross-mailbox state corruption?)"
            )

        # Dry-run: skip the actual move. We still ran the cross-mailbox
        # guard so any guard violation is recorded as 'aborted-permanent'
        # even in dry-run. The audit row will say action='dry-run' so the
        # operator can review what would have happened.
        if self.dry_run:
            return ("dry-run", folder_name)

        # Both checks passed; do the move. If the folder_id is stale (folder
        # was deleted+recreated since we cached the map), Graph returns 404 /
        # 410. Refresh the map once, re-resolve the folder by name, re-run
        # the cross-mailbox guard against the FRESH map, and retry. Any other
        # error propagates.
        try:
            self.graph.move_message(user_id, message_id, dest_id)
            return ("moved", folder_name)
        except GraphError as e:
            if e.status not in _STALE_MAP_STATUSES:
                raise
            log.warning(
                "move_message returned %d for folder_id=%s in %s — refreshing "
                "folder map and retrying once",
                e.status, dest_id, user_id,
            )
            fmap2 = self.folder_map(user_id, refresh=True)
            new_dest = fmap2.folder_id_for(folder_name)
            if new_dest is None:
                raise GraphError(
                    "POST", f"/users/{user_id}/messages/{message_id}/move",
                    404,
                    f"folder {folder_name!r} no longer exists in mailbox {user_id} "
                    "after refresh",
                ) from e
            if new_dest == dest_id:
                # Same folder_id resolved again; retry would just hit the
                # same error. Surface the original.
                raise
            if not fmap2.owns(new_dest):
                raise CrossMailboxError(
                    f"refused to move message {message_id} after refresh — "
                    f"folder {new_dest} is not in mailbox {user_id}"
                )
            self.graph.move_message(user_id, message_id, new_dest)
            return ("moved", folder_name)

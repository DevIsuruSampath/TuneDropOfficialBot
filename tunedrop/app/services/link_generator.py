from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError

from tunedrop.app.core.database import get_database
from tunedrop.app.core.config import settings
from tunedrop.app.utils.time_utils import format_bytes
from tunedrop.app.utils.memory_cache import MemoryCache


logger = logging.getLogger(__name__)

_LINK_TTL_HOURS = 24
_get_cache = MemoryCache(max_size=5000, ttl=300.0)  # 5 min TTL for get() results
_ref_cache = MemoryCache(max_size=2000, ttl=300.0)  # 5 min TTL for ref lookups


class LinkStore:
    async def create_ref(self, payload: dict[str, Any]) -> str:
        """Create a persistent download reference (no expiry)."""
        db = get_database()
        for _ in range(3):
            ref = secrets.token_urlsafe(12)
            try:
                doc = {"ref": ref, **payload, "created_at": datetime.now(UTC)}
                await db["download_refs"].insert_one(doc)
                return ref
            except DuplicateKeyError:
                continue
        raise RuntimeError("Failed to generate unique download reference")

    async def create_refs_bulk(self, payloads: list[dict[str, Any]]) -> list[str]:
        """Create multiple persistent download references in a single bulk write.

        Returns list of ref strings in the same order as payloads.
        Falls back to individual inserts if bulk_write fails.
        """
        if not payloads:
            return []
        from pymongo import InsertOne
        db = get_database()
        now = datetime.now(UTC)
        refs: list[str] = []
        docs: list[dict[str, Any]] = []
        for payload in payloads:
            ref = secrets.token_urlsafe(12)
            refs.append(ref)
            docs.append({"ref": ref, **payload, "created_at": now})
        try:
            await db["download_refs"].bulk_write(
                [InsertOne(doc) for doc in docs],
                ordered=False,
            )
            return refs
        except Exception:
            logger.warning("Bulk ref insert failed, falling back to individual inserts", exc_info=True)
            result_refs: list[str] = []
            for payload in payloads:
                ref = await self.create_ref(payload)
                result_refs.append(ref)
            return result_refs

    async def resolve_ref(self, ref: str) -> str | None:
        """Resolve a persistent ref to a short download page URL.

        Returns the /d/{ref} URL (no redirect needed — page renders directly).
        Kept for backward compatibility; new code uses resolve_ref_for_page().
        """
        db = get_database()
        row = await db["download_refs"].find_one({"ref": ref})
        if not row:
            return None
        return f"{settings.download_base_url.rstrip('/')}/d/{ref}"

    async def resolve_ref_for_page(self, ref: str) -> tuple[dict[str, Any], str] | None:
        """Resolve a ref to (item_payload, stream_token) for rendering the download page.

        Creates or reuses a 24h expiring token in file_links for the stream URL.
        Returns None if ref is invalid.
        """
        # L1 memory cache — refs are immutable, safe to cache for 5 min
        cached = _ref_cache.get(ref)
        if cached is not None:
            return cached

        db = get_database()
        row = await db["download_refs"].find_one({"ref": ref})
        if not row:
            return None

        file_id = row.get("file_id")
        user_id = row.get("user_id", 0)
        token: str | None = None

        # Reuse existing valid token if one exists
        if file_id:
            existing = await db["file_links"].find_one(
                {"file_id": file_id, "expires_at": {"$gt": datetime.now(UTC)}},
                projection={"token": 1, "_id": 0},
            )
            if existing:
                token = existing["token"]

        # Create new token if needed
        if not token:
            payload = {
                k: row[k]
                for k in ("chat_id", "file_id", "file_name", "file_size", "message_id", "bot_index")
                if k in row
            }
            link = await self.create_link(user_id, payload)
            # Extract token from link URL
            token = link.rsplit("/", 1)[-1] if link else None

        if not token:
            return None

        # Build item payload for the template (includes chat_id/message_id for verification)
        item = {k: row[k] for k in ("file_name", "file_size", "user_id", "bot_index", "chat_id", "message_id") if k in row}
        result = (item, token)
        _ref_cache.set(ref, result, ttl=300.0)
        return result

    async def create_link(self, user_id: int, payload: dict[str, Any]) -> str:
        db = get_database()
        for _ in range(3):
            token = secrets.token_urlsafe(16)
            created_at = datetime.now(UTC)
            expires_at = created_at + timedelta(hours=_LINK_TTL_HOURS)
            link = f"{settings.download_base_url.rstrip('/')}/f/{token}"
            try:
                doc = {
                    "token": token,
                    "user_id": user_id,
                    "created_at": created_at,
                    "expires_at": expires_at,
                    **payload,
                }
                await db["file_links"].insert_one(doc)
            except DuplicateKeyError:
                continue
            break
        else:
            raise RuntimeError("Failed to generate unique download token")

        # Parallel insert into user_files
        user_doc = {
            "user_id": user_id,
            "token": token,
            "name": payload.get("file_name", "download"),
            "size": payload.get("file_size", 0),
            "size_text": format_bytes(payload.get("file_size", 0)),
            "link": link,
            "created_at": created_at,
            "expires_at": expires_at,
        }

        # Defer stale cleanup — only run every 10 minutes per user
        cleanup_tasks = [db["user_files"].insert_one(user_doc)]
        now = time.monotonic()
        if now - _last_stale_cleanup.get(user_id, 0) > _STALE_CLEANUP_INTERVAL:
            _last_stale_cleanup[user_id] = now
            stale_entries = await (
                db["user_files"]
                .find({"user_id": user_id}, projection={"_id": 1, "token": 1})
                .sort("created_at", -1)
                .skip(20)
                .to_list(length=None)
            )
            if stale_entries:
                stale_ids = [entry["_id"] for entry in stale_entries]
                stale_tokens = [entry["token"] for entry in stale_entries]
                cleanup_tasks.append(db["user_files"].delete_many({"_id": {"$in": stale_ids}}))
                cleanup_tasks.append(db["file_links"].delete_many({"token": {"$in": stale_tokens}}))
        await asyncio.gather(*cleanup_tasks)

        return link

    async def list_user_files(self, user_id: int) -> list[dict[str, Any]]:
        db = get_database()
        now = datetime.now(UTC)
        rows = await (
            db["user_files"]
            .find({"user_id": user_id, "expires_at": {"$gt": now}}, projection={"_id": 0, "created_at": 0, "user_id": 0, "expires_at": 0})
            .sort("created_at", -1)
            .to_list(length=10)
        )
        return rows

    async def revoke_file(self, user_id: int, token: str) -> bool:
        """Delete a user's file link. Returns True if found and deleted."""
        db = get_database()
        _get_cache.delete(token)
        # Remove from user_files (token here is the file_links token)
        result = await db["user_files"].delete_one({"user_id": user_id, "token": token})
        if result.deleted_count > 0:
            await db["file_links"].delete_many({"token": token})
            # Also remove the download_ref that points to this file
            # download_refs use "ref" field which is different from file_links token
            # The user_files doc has "token" matching file_links token
            return True
        # Also try removing from file_links directly
        fl_result = await db["file_links"].delete_many({"token": token})
        return fl_result.deleted_count > 0 or result.deleted_count > 0

    async def get(self, token: str) -> dict[str, Any] | None:
        cached = _get_cache.get(token)
        if cached is not None:
            return cached
        db = get_database()
        row = await db["file_links"].find_one({"token": token}, projection={"_id": 0, "token": 0})
        if not row:
            return None
        expires_at = row.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if datetime.now(UTC) > expires_at:
                row["expired"] = True
        for field in ("created_at", "expires_at"):
            if row.get(field) is not None:
                row[field] = row[field].isoformat()
        _get_cache.set(token, row)
        return row



link_store = LinkStore()

# Defer stale file cleanup — only run every 10 minutes per user
_last_stale_cleanup: dict[int, float] = {}
_STALE_CLEANUP_INTERVAL = 600.0  # seconds

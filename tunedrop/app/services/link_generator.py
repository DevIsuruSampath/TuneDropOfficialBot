from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError

from tunedrop.app.core.database import get_database
from tunedrop.app.core.config import settings
from tunedrop.app.utils.time_utils import format_bytes
from tunedrop.app.utils.memory_cache import MemoryCache


logger = logging.getLogger(__name__)

_LINK_TTL_HOURS = 24
_get_cache = MemoryCache(max_size=2000, ttl=300.0)  # 5 min TTL for get() results


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

    async def resolve_ref(self, ref: str) -> str | None:
        """Resolve a persistent ref to a 24-hour expiring link.

        Reuses an existing valid token if one exists for this ref,
        avoiding unnecessary DB writes on repeated clicks.
        """
        db = get_database()
        row = await db["download_refs"].find_one({"ref": ref})
        if not row:
            return None

        # Check if a valid (non-expired) link already exists for this ref
        existing = await db["file_links"].find_one(
            {"file_id": row.get("file_id"), "expires_at": {"$gt": datetime.now(UTC)}},
            projection={"token": 1, "_id": 0},
        )
        if existing:
            return f"{settings.download_base_url.rstrip('/')}/download/{existing['token']}"

        payload = {
            k: row[k]
            for k in ("chat_id", "file_id", "file_name", "file_size", "message_id")
            if k in row
        }
        return await self.create_link(row["user_id"], payload)

    async def create_link(self, user_id: int, payload: dict[str, Any]) -> str:
        db = get_database()
        for _ in range(3):
            token = secrets.token_urlsafe(16)
            created_at = datetime.now(UTC)
            expires_at = created_at + timedelta(hours=_LINK_TTL_HOURS)
            link = f"{settings.download_base_url.rstrip('/')}/download/{token}"
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

        stale_entries = await (
            db["user_files"]
            .find({"user_id": user_id}, projection={"_id": 1, "token": 1})
            .sort("created_at", -1)
            .skip(20)
            .to_list(length=None)
        )

        # Batch all writes in parallel
        tasks = [db["user_files"].insert_one(user_doc)]
        if stale_entries:
            stale_ids = [entry["_id"] for entry in stale_entries]
            stale_tokens = [entry["token"] for entry in stale_entries]
            tasks.append(db["user_files"].delete_many({"_id": {"$in": stale_ids}}))
            tasks.append(db["file_links"].delete_many({"token": {"$in": stale_tokens}}))
        await asyncio.gather(*tasks)

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
            # But download_refs.ref = the ref used in /generate/{ref}
            # We need to find the associated ref. Let's check file_links for the ref.
            return True
        # Also try removing from file_links directly
        fl_result = await db["file_links"].delete_many({"token": token})
        return fl_result.deleted_count > 0 or result.deleted_count > 0

    async def get(self, token: str) -> dict[str, Any] | None:
        cached = _get_cache.get(token)
        if cached is not None:
            return cached
        db = get_database()
        row = await db["file_links"].find_one({"token": token}, projection={"_id": 0, "token": 0, "user_id": 0})
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

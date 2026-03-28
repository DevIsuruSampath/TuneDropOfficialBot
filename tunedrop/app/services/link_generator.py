from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError

from tunedrop.app.core.database import get_database
from tunedrop.app.core.config import settings
from tunedrop.app.utils.time_utils import format_bytes


logger = logging.getLogger(__name__)

_LINK_TTL_HOURS = 24


class LinkStore:
    async def create_ref(self, payload: dict[str, Any]) -> str:
        """Create a persistent download reference (no expiry)."""
        db = get_database()
        for _ in range(3):
            ref = secrets.token_urlsafe(12)
            try:
                await db["download_refs"].insert_one(
                    {"ref": ref, **payload, "created_at": datetime.now(UTC)},
                )
                return ref
            except DuplicateKeyError:
                continue
        raise RuntimeError("Failed to generate unique download reference")

    async def resolve_ref(self, ref: str) -> str | None:
        """Resolve a persistent ref to a new 24-hour expiring link."""
        db = get_database()
        row = await db["download_refs"].find_one({"ref": ref})
        if not row:
            return None
        payload = {
            k: row[k]
            for k in ("chat_id", "file_id", "file_name", "file_size")
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
                await db["file_links"].insert_one(
                    {
                        "token": token,
                        "user_id": user_id,
                        "created_at": created_at,
                        "expires_at": expires_at,
                        **payload,
                    }
                )
            except DuplicateKeyError:
                continue
            break
        else:
            raise RuntimeError("Failed to generate unique download token")

        await db["user_files"].insert_one(
            {
                "user_id": user_id,
                "token": token,
                "name": payload.get("file_name", "download"),
                "size": payload.get("file_size", 0),
                "size_text": format_bytes(payload.get("file_size", 0)),
                "link": link,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        )

        stale_entries = await (
            db["user_files"]
            .find({"user_id": user_id}, projection={"_id": 1, "token": 1})
            .sort("created_at", -1)
            .skip(20)
            .to_list(length=None)
        )
        if stale_entries:
            stale_tokens = [entry["token"] for entry in stale_entries]
            await db["user_files"].delete_many(
                {"_id": {"$in": [entry["_id"] for entry in stale_entries]}}
            )
            await db["file_links"].delete_many({"token": {"$in": stale_tokens}})

        return link

    async def list_user_files(self, user_id: int) -> list[dict[str, Any]]:
        db = get_database()
        rows = await (
            db["user_files"]
            .find({"user_id": user_id}, projection={"_id": 0, "created_at": 0, "user_id": 0})
            .sort("created_at", -1)
            .to_list(length=10)
        )
        return rows

    async def get(self, token: str) -> dict[str, Any] | None:
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
        return row


link_store = LinkStore()

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from tunedrop.app.core.database import get_database
from tunedrop.app.core.config import settings
from tunedrop.app.utils.time_utils import format_bytes


class LinkStore:
    async def create_link(self, user_id: int, payload: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(16)
        created_at = datetime.now(UTC)
        link = f"{settings.download_base_url.rstrip('/')}/download/{token}"
        db = get_database()

        await db["file_links"].insert_one(
            {
                "token": token,
                "user_id": user_id,
                "created_at": created_at,
                **payload,
            }
        )

        await db["user_files"].insert_one(
            {
                "user_id": user_id,
                "token": token,
                "name": payload["file_name"],
                "size": payload["file_size"],
                "size_text": format_bytes(payload["file_size"]),
                "link": link,
                "created_at": created_at,
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
            .to_list(length=20)
        )
        return rows

    async def get(self, token: str) -> dict[str, Any] | None:
        db = get_database()
        row = await db["file_links"].find_one({"token": token}, projection={"_id": 0, "token": 0, "user_id": 0})
        if not row:
            return None
        created_at = row.get("created_at")
        if created_at is not None:
            row["created_at"] = created_at.isoformat()
        return row


link_store = LinkStore()

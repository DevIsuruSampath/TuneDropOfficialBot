from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from tunedrop.app.core.config import settings
from tunedrop.app.utils.file_utils import read_json_file, upsert_user_file_record, write_json_file
from tunedrop.app.utils.time_utils import format_bytes


class LinkStore:
    async def create_link(self, user_id: int, payload: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(16)
        cache = read_json_file(settings.cache_file, default={})
        payload["created_at"] = datetime.now(UTC).isoformat()
        cache[token] = payload
        write_json_file(settings.cache_file, cache)

        upsert_user_file_record(
            settings.users_file,
            user_id=user_id,
            item={
                "token": token,
                "name": payload["file_name"],
                "size": payload["file_size"],
                "size_text": format_bytes(payload["file_size"]),
                "link": f"{settings.download_base_url.rstrip('/')}/download/{token}",
            },
        )
        return f"{settings.download_base_url.rstrip('/')}/download/{token}"

    async def list_user_files(self, user_id: int) -> list[dict[str, Any]]:
        users = read_json_file(settings.users_file, default={})
        return list(reversed(users.get(str(user_id), [])))

    async def get(self, token: str) -> dict[str, Any] | None:
        cache = read_json_file(settings.cache_file, default={})
        return cache.get(token)


link_store = LinkStore()

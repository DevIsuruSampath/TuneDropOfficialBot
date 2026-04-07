from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from tunedrop.app.core.database import get_database

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Manage users and track Stars donations."""

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        db = get_database()
        user = await db["users"].find_one({"user_id": user_id})
        if user:
            return user
        now = datetime.now(UTC)
        doc = {
            "user_id": user_id,
            "stars_paid": 0,
            "created_at": now,
            "updated_at": now,
        }
        await db["users"].insert_one(doc)
        return doc

    async def record_donation(self, user_id: int, stars_amount: int) -> None:
        """Record a Stars donation from a user."""
        db = get_database()
        now = datetime.now(UTC)
        await db["users"].update_one(
            {"user_id": user_id},
            {
                "$inc": {"stars_paid": stars_amount},
                "$set": {"updated_at": now},
                "$setOnInsert": {"user_id": user_id, "created_at": now},
            },
            upsert=True,
        )
        logger.info("Recorded %d Stars donation from user %d", stars_amount, user_id)

    async def get_user_info(self, user_id: int) -> dict[str, Any] | None:
        db = get_database()
        return await db["users"].find_one({"user_id": user_id}, projection={"_id": 0})

    async def get_total_donations(self) -> int:
        """Get total Stars donated across all users."""
        db = get_database()
        result = await db["users"].aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$stars_paid"}}}
        ]).to_list(length=1)
        if result:
            return result[0].get("total", 0)
        return 0


subscription_service = SubscriptionService()

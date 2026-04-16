from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from tunedrop.app.core.database import get_database
from tunedrop.app.utils.memory_cache import MemoryCache

logger = logging.getLogger(__name__)

_PLAN_CACHE = MemoryCache(max_size=5000, ttl=300.0)  # 5 min TTL for plan checks
_ADMIN_STATS_CACHE = MemoryCache(max_size=10, ttl=60.0)  # 60s TTL for admin stats

_PRO_CAPABILITIES = {
    "telegram_delivery": True,
    "no_ads": True,
    "priority_queue": True,
}
_FREE_CAPABILITIES = {
    "telegram_delivery": False,
    "no_ads": False,
    "priority_queue": False,
}


class SubscriptionService:
    """Manage users, plans, and Stars donations."""

    async def get_or_create_user(self, user_id: int) -> dict[str, Any]:
        """Get user record, creating one if needed. Ensures plan fields exist."""
        db = get_database()
        user = await db["users"].find_one({"user_id": user_id})
        if user:
            # Migration: add missing plan fields
            if "plan" not in user:
                user["plan"] = "free"
            if "pro_until" not in user:
                user["pro_until"] = None
            return user
        now = datetime.now(UTC)
        doc = {
            "user_id": user_id,
            "plan": "free",
            "pro_until": None,
            "stars_paid": 0,
            "created_at": now,
            "updated_at": now,
        }
        await db["users"].insert_one(doc)
        return doc

    async def is_pro(self, user_id: int) -> bool:
        """Check if user has active Pro status (cached)."""
        cached = _PLAN_CACHE.get(f"plan:{user_id}")
        if cached is not None:
            return cached == "pro"
        user = await self.get_or_create_user(user_id)
        is_pro_user = self._check_pro_status(user)
        _PLAN_CACHE.set(f"plan:{user_id}", "pro" if is_pro_user else "free")
        return is_pro_user

    def _check_pro_status(self, user: dict[str, Any]) -> bool:
        """Check if a user document represents an active Pro user."""
        if user.get("plan") != "pro":
            return False
        pro_until = user.get("pro_until")
        if not pro_until:
            return False
        if pro_until.tzinfo is None:
            pro_until = pro_until.replace(tzinfo=UTC)
        return datetime.now(UTC) < pro_until

    async def get_user_plan(self, user_id: int) -> str:
        """Get user's current plan ('free' or 'pro')."""
        if await self.is_pro(user_id):
            return "pro"
        return "free"

    async def get_capabilities(self, user_id: int) -> dict[str, bool]:
        """Get user capabilities based on plan."""
        if await self.is_pro(user_id):
            return dict(_PRO_CAPABILITIES)
        return dict(_FREE_CAPABILITIES)

    async def grant_pro(self, user_id: int, days: int = 30) -> datetime:
        """Grant Pro status for the specified number of days.

        If user is currently Pro, extends from pro_until.
        If expired or Free, starts from now.
        Returns the new pro_until datetime.
        """
        db = get_database()
        now = datetime.now(UTC)
        user = await self.get_or_create_user(user_id)
        pro_until = user.get("pro_until")
        if pro_until and pro_until.tzinfo is None:
            pro_until = pro_until.replace(tzinfo=UTC)
        # If currently Pro, extend from pro_until. Otherwise start from now.
        if pro_until and pro_until > now:
            new_pro_until = pro_until + timedelta(days=days)
        else:
            new_pro_until = now + timedelta(days=days)
        await db["users"].update_one(
            {"user_id": user_id},
            {"$set": {"plan": "pro", "pro_until": new_pro_until, "updated_at": now}},
        )
        # Invalidate plan cache
        _PLAN_CACHE.delete(f"plan:{user_id}")
        logger.info("Granted %d days Pro to user %d (until %s)", days, user_id, new_pro_until.isoformat())
        return new_pro_until

    async def revoke_pro(self, user_id: int) -> None:
        """Revoke Pro status, setting user back to Free."""
        db = get_database()
        now = datetime.now(UTC)
        await db["users"].update_one(
            {"user_id": user_id},
            {"$set": {"plan": "free", "pro_until": None, "updated_at": now}},
        )
        _PLAN_CACHE.delete(f"plan:{user_id}")
        logger.info("Revoked Pro for user %d", user_id)

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        """Get or create user record (legacy alias for get_or_create_user)."""
        return await self.get_or_create_user(user_id)

    async def record_donation(self, user_id: int, stars_amount: int) -> None:
        """Record a Stars donation from a user."""
        db = get_database()
        now = datetime.now(UTC)
        # Run user update and donation insert in parallel
        await asyncio.gather(
            db["users"].update_one(
                {"user_id": user_id},
                {
                    "$inc": {"stars_paid": stars_amount},
                    "$set": {"updated_at": now},
                    "$setOnInsert": {"user_id": user_id, "created_at": now, "plan": "free", "pro_until": None},
                },
                upsert=True,
            ),
            db["donations"].insert_one({
                "user_id": user_id,
                "stars": stars_amount,
                "created_at": now,
            }),
        )
        _PLAN_CACHE.delete(f"plan:{user_id}")
        logger.info("Recorded %d Stars donation from user %d", stars_amount, user_id)

    async def get_donation_history(self, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
        """Get recent donations for a user (newest first)."""
        db = get_database()
        cursor = db["donations"].find(
            {"user_id": user_id},
            {"_id": 0, "stars": 1, "created_at": 1},
        ).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_user_info(self, user_id: int) -> dict[str, Any] | None:
        db = get_database()
        return await db["users"].find_one({"user_id": user_id}, projection={"_id": 0})

    async def get_total_donations(self) -> int:
        """Get total Stars donated across all users (cached 60s)."""
        cached = _ADMIN_STATS_CACHE.get("total_donations")
        if cached is not None:
            return cached
        db = get_database()
        cursor = await db["users"].aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$stars_paid"}}}
        ])
        result = await cursor.to_list(length=1)
        total = result[0].get("total", 0) if result else 0
        _ADMIN_STATS_CACHE.set("total_donations", total)
        return total

    async def get_pro_count(self) -> int:
        """Get count of users with active Pro status (cached 60s)."""
        cached = _ADMIN_STATS_CACHE.get("pro_count")
        if cached is not None:
            return cached
        db = get_database()
        now = datetime.now(UTC)
        count = await db["users"].count_documents({
            "plan": "pro",
            "pro_until": {"$gt": now},
        })
        _ADMIN_STATS_CACHE.set("pro_count", count)
        return count


subscription_service = SubscriptionService()

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from bot.config import get_settings


@dataclass
class Punishment:
    id: int
    chat_id: int
    user_id: int
    username: str | None
    punishment_type: str
    duration_minutes: int | None
    rule_references: str
    explanation: str
    message_id: int | None
    can_unpunish_ids: str
    active: bool
    created_at: str
    expires_at: str | None


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or get_settings().database_path

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        async with self.connection() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    rules_text TEXT NOT NULL DEFAULT '',
                    batch_interval INTEGER NOT NULL DEFAULT 30,
                    moderation_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS punishments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    punishment_type TEXT NOT NULL,
                    duration_minutes INTEGER,
                    rule_references TEXT NOT NULL DEFAULT '[]',
                    explanation TEXT NOT NULL DEFAULT '',
                    message_id INTEGER,
                    can_unpunish_ids TEXT NOT NULL DEFAULT '[]',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS dm_spam_bans (
                    user_id INTEGER PRIMARY KEY,
                    banned_until TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dm_spam_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gemini_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_index INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_punishments_chat_active
                    ON punishments(chat_id, active);
                CREATE INDEX IF NOT EXISTS idx_punishments_user
                    ON punishments(user_id);
                CREATE INDEX IF NOT EXISTS idx_gemini_usage_day
                    ON gemini_usage(project_index, model, created_at);
                """
            )
            await db.commit()

    async def get_chat_settings(self, chat_id: int) -> dict[str, Any]:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
            ).fetchone()
            if row:
                return dict(row)
            now = _now_iso()
            settings = get_settings()
            await db.execute(
                """
                INSERT INTO chat_settings (chat_id, rules_text, batch_interval, moderation_enabled, updated_at)
                VALUES (?, '', ?, 1, ?)
                """,
                (chat_id, settings.default_batch_interval, now),
            )
            await db.commit()
            return {
                "chat_id": chat_id,
                "rules_text": "",
                "batch_interval": settings.default_batch_interval,
                "moderation_enabled": 1,
                "updated_at": now,
            }

    async def update_chat_rules(self, chat_id: int, rules_text: str) -> None:
        await self.get_chat_settings(chat_id)
        async with self.connection() as db:
            await db.execute(
                "UPDATE chat_settings SET rules_text = ?, updated_at = ? WHERE chat_id = ?",
                (rules_text, _now_iso(), chat_id),
            )
            await db.commit()

    async def update_batch_interval(self, chat_id: int, interval: int) -> None:
        await self.get_chat_settings(chat_id)
        async with self.connection() as db:
            await db.execute(
                "UPDATE chat_settings SET batch_interval = ?, updated_at = ? WHERE chat_id = ?",
                (interval, _now_iso(), chat_id),
            )
            await db.commit()

    async def set_moderation_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.get_chat_settings(chat_id)
        async with self.connection() as db:
            await db.execute(
                "UPDATE chat_settings SET moderation_enabled = ?, updated_at = ? WHERE chat_id = ?",
                (1 if enabled else 0, _now_iso(), chat_id),
            )
            await db.commit()

    async def add_punishment(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        punishment_type: str,
        duration_minutes: int | None,
        rule_references: list[str],
        explanation: str,
        message_id: int | None,
        can_unpunish_ids: list[int],
        expires_at: datetime | None,
    ) -> int:
        async with self.connection() as db:
            cur = await db.execute(
                """
                INSERT INTO punishments (
                    chat_id, user_id, username, punishment_type, duration_minutes,
                    rule_references, explanation, message_id, can_unpunish_ids,
                    active, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    username,
                    punishment_type,
                    duration_minutes,
                    json.dumps(rule_references, ensure_ascii=False),
                    explanation,
                    message_id,
                    json.dumps(can_unpunish_ids),
                    _now_iso(),
                    expires_at.isoformat() if expires_at else None,
                ),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def deactivate_punishment(self, punishment_id: int) -> Punishment | None:
        async with self.connection() as db:
            await db.execute("UPDATE punishments SET active = 0 WHERE id = ?", (punishment_id,))
            await db.commit()
            row = await (
                await db.execute("SELECT * FROM punishments WHERE id = ?", (punishment_id,))
            ).fetchone()
            return _row_to_punishment(row) if row else None

    async def get_punishment(self, punishment_id: int) -> Punishment | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM punishments WHERE id = ?", (punishment_id,))
            ).fetchone()
            return _row_to_punishment(row) if row else None

    async def get_active_punishments(self, chat_id: int) -> list[Punishment]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM punishments WHERE chat_id = ? AND active = 1 ORDER BY created_at DESC",
                    (chat_id,),
                )
            ).fetchall()
            return [_row_to_punishment(r) for r in rows]

    async def get_all_punishments(self, chat_id: int | None = None, limit: int = 50) -> list[Punishment]:
        async with self.connection() as db:
            if chat_id is not None:
                rows = await (
                    await db.execute(
                        "SELECT * FROM punishments WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
                        (chat_id, limit),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM punishments ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    )
                ).fetchall()
            return [_row_to_punishment(r) for r in rows]

    async def get_punishments_for_users(self, chat_id: int, user_ids: list[int], days: int = 30) -> list[Punishment]:
        if not user_ids:
            return []
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        placeholders = ",".join("?" * len(user_ids))
        query = f"""
            SELECT * FROM punishments
            WHERE chat_id = ? AND user_id IN ({placeholders}) AND created_at >= ?
            ORDER BY created_at DESC
        """
        async with self.connection() as db:
            rows = await (await db.execute(query, (chat_id, *user_ids, since))).fetchall()
            return [_row_to_punishment(r) for r in rows]

    async def record_gemini_usage(self, project_index: int, model: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO gemini_usage (project_index, model, created_at) VALUES (?, ?, ?)",
                (project_index, model, _now_iso()),
            )
            await db.commit()

    async def get_gemini_usage_stats(self) -> dict[tuple[int, str], int]:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT project_index, model, COUNT(*) as cnt
                    FROM gemini_usage WHERE created_at >= ?
                    GROUP BY project_index, model
                    """,
                    (today_start,),
                )
            ).fetchall()
            return {(r["project_index"], r["model"]): r["cnt"] for r in rows}

    async def is_dm_banned(self, user_id: int) -> bool:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT banned_until FROM dm_spam_bans WHERE user_id = ?", (user_id,))
            ).fetchone()
            if not row:
                return False
            banned_until = datetime.fromisoformat(row["banned_until"])
            if banned_until > datetime.now(timezone.utc):
                return True
            await db.execute("DELETE FROM dm_spam_bans WHERE user_id = ?", (user_id,))
            await db.commit()
            return False

    async def record_dm_message(self, user_id: int) -> bool:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(seconds=settings.spam_window_seconds)).isoformat()
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO dm_spam_events (user_id, created_at) VALUES (?, ?)",
                (user_id, now.isoformat()),
            )
            row = await (
                await db.execute(
                    "SELECT COUNT(*) as cnt FROM dm_spam_events WHERE user_id = ? AND created_at >= ?",
                    (user_id, window_start),
                )
            ).fetchone()
            await db.commit()
            if row and row["cnt"] >= settings.spam_threshold:
                banned_until = now + timedelta(days=settings.spam_ban_days)
                await db.execute(
                    """
                    INSERT INTO dm_spam_bans (user_id, banned_until, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET banned_until = excluded.banned_until
                    """,
                    (user_id, banned_until.isoformat(), now.isoformat()),
                )
                await db.commit()
                return True
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_punishment(row: aiosqlite.Row) -> Punishment:
    return Punishment(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        username=row["username"],
        punishment_type=row["punishment_type"],
        duration_minutes=row["duration_minutes"],
        rule_references=row["rule_references"],
        explanation=row["explanation"],
        message_id=row["message_id"],
        can_unpunish_ids=row["can_unpunish_ids"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )

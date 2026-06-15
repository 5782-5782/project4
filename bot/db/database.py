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
                    admin_user_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS registered_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    owner_admin_id INTEGER NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sub_admins (
                    user_id INTEGER PRIMARY KEY,
                    daily_limit INTEGER NOT NULL DEFAULT 500,
                    display_name TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS moderation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    action TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    rule_references TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_registered_chats_owner
                    ON registered_chats(owner_admin_id);
                CREATE INDEX IF NOT EXISTS idx_moderation_log_chat
                    ON moderation_log(chat_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_gemini_usage_admin
                    ON gemini_usage(admin_user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_punishments_chat_active
                    ON punishments(chat_id, active);
                CREATE INDEX IF NOT EXISTS idx_punishments_user
                    ON punishments(user_id);
                CREATE INDEX IF NOT EXISTS idx_gemini_usage_day
                    ON gemini_usage(project_index, model, created_at);
                """
            )
            await db.commit()
            await self._migrate(db)

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ALTER TABLE gemini_usage ADD COLUMN admin_user_id INTEGER")
            await db.commit()
        except Exception:
            pass
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_rules_input (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

    async def register_chat(self, chat_id: int, title: str, owner_admin_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO registered_chats (chat_id, title, owner_admin_id, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
                """,
                (chat_id, title, owner_admin_id, _now_iso()),
            )
            await db.commit()
        await self.get_chat_settings(chat_id)

    async def get_registered_chat(self, chat_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM registered_chats WHERE chat_id = ?", (chat_id,))
            ).fetchone()
            return dict(row) if row else None

    async def list_chats_for_admin(self, admin_id: int, is_owner: bool) -> list[dict[str, Any]]:
        async with self.connection() as db:
            if is_owner:
                rows = await (
                    await db.execute("SELECT * FROM registered_chats ORDER BY title")
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        "SELECT * FROM registered_chats WHERE owner_admin_id = ? ORDER BY title",
                        (admin_id,),
                    )
                ).fetchall()
            return [dict(r) for r in rows]

    async def add_sub_admin(self, user_id: int, daily_limit: int, display_name: str = "") -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO sub_admins (user_id, daily_limit, display_name, active, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    daily_limit = excluded.daily_limit,
                    display_name = excluded.display_name,
                    active = 1
                """,
                (user_id, daily_limit, display_name, _now_iso()),
            )
            await db.commit()

    async def remove_sub_admin(self, user_id: int) -> None:
        async with self.connection() as db:
            await db.execute("UPDATE sub_admins SET active = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

    async def get_sub_admin(self, user_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM sub_admins WHERE user_id = ?", (user_id,))
            ).fetchone()
            return dict(row) if row else None

    async def list_sub_admins(self) -> list[dict[str, Any]]:
        async with self.connection() as db:
            rows = await (
                await db.execute("SELECT * FROM sub_admins WHERE active = 1 ORDER BY created_at")
            ).fetchall()
            return [dict(r) for r in rows]

    async def update_sub_admin_limit(self, user_id: int, daily_limit: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE sub_admins SET daily_limit = ? WHERE user_id = ?", (daily_limit, user_id)
            )
            await db.commit()

    async def log_moderation(
        self,
        chat_id: int,
        message_id: int | None,
        action: str,
        explanation: str,
        rule_references: list[str] | None = None,
    ) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO moderation_log (chat_id, message_id, action, explanation, rule_references, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    action,
                    explanation,
                    json.dumps(rule_references or [], ensure_ascii=False),
                    _now_iso(),
                ),
            )
            await db.commit()

    async def get_moderation_stats(self, chat_id: int | None = None, days: int = 1) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self.connection() as db:
            if chat_id:
                rows = await (
                    await db.execute(
                        """
                        SELECT action, COUNT(*) as cnt FROM moderation_log
                        WHERE chat_id = ? AND created_at >= ? GROUP BY action
                        """,
                        (chat_id, since),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        """
                        SELECT action, COUNT(*) as cnt FROM moderation_log
                        WHERE created_at >= ? GROUP BY action
                        """,
                        (since,),
                    )
                ).fetchall()
            return {r["action"]: r["cnt"] for r in rows}

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

    async def set_pending_rules_input(self, user_id: int, chat_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO pending_rules_input (user_id, chat_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    created_at = excluded.created_at
                """,
                (user_id, chat_id, _now_iso()),
            )
            await db.commit()

    async def get_pending_rules_input(self, user_id: int) -> int | None:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT chat_id FROM pending_rules_input WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
            return int(row["chat_id"]) if row else None

    async def clear_pending_rules_input(self, user_id: int) -> None:
        async with self.connection() as db:
            await db.execute("DELETE FROM pending_rules_input WHERE user_id = ?", (user_id,))
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
        active: bool = True,
    ) -> int:
        async with self.connection() as db:
            cur = await db.execute(
                """
                INSERT INTO punishments (
                    chat_id, user_id, username, punishment_type, duration_minutes,
                    rule_references, explanation, message_id, can_unpunish_ids,
                    active, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if active else 0,
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

    async def delete_punishment(self, punishment_id: int) -> bool:
        async with self.connection() as db:
            cur = await db.execute("DELETE FROM punishments WHERE id = ?", (punishment_id,))
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def get_chat_punishment_history(
        self,
        chat_id: int,
        limit: int = 30,
        days: int | None = None,
    ) -> list[Punishment]:
        settings = get_settings()
        retention_days = settings.punishment_history_days if days is None else days
        since = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM punishments
                    WHERE chat_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (chat_id, since, limit),
                )
            ).fetchall()
            return [_row_to_punishment(r) for r in rows]

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

    async def get_all_punishments(
        self,
        chat_id: int | None = None,
        limit: int = 50,
        days: int | None = None,
    ) -> list[Punishment]:
        settings = get_settings()
        retention_days = settings.punishment_history_days if days is None else days
        since = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        async with self.connection() as db:
            if chat_id is not None:
                rows = await (
                    await db.execute(
                        """
                        SELECT * FROM punishments
                        WHERE chat_id = ? AND created_at >= ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (chat_id, since, limit),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        """
                        SELECT * FROM punishments
                        WHERE created_at >= ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (since, limit),
                    )
                ).fetchall()
            return [_row_to_punishment(r) for r in rows]

    async def get_punishments_for_users(
        self,
        chat_id: int,
        user_ids: list[int],
        days: int | None = None,
    ) -> list[Punishment]:
        if not user_ids:
            return []
        settings = get_settings()
        retention_days = settings.punishment_history_days if days is None else days
        since = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        placeholders = ",".join("?" * len(user_ids))
        query = f"""
            SELECT * FROM punishments
            WHERE chat_id = ? AND user_id IN ({placeholders}) AND created_at >= ?
            ORDER BY created_at DESC
        """
        async with self.connection() as db:
            rows = await (await db.execute(query, (chat_id, *user_ids, since))).fetchall()
            return [_row_to_punishment(r) for r in rows]

    async def record_gemini_usage(self, project_index: int, model: str, admin_user_id: int | None = None) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO gemini_usage (project_index, model, admin_user_id, created_at) VALUES (?, ?, ?, ?)",
                (project_index, model, admin_user_id, _now_iso()),
            )
            await db.commit()

    async def get_admin_daily_usage(self, admin_user_id: int) -> int:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) as cnt FROM gemini_usage WHERE admin_user_id = ? AND created_at >= ?",
                    (admin_user_id, today_start),
                )
            ).fetchone()
            return row["cnt"] if row else 0

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

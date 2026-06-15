"""Clear bot database test data (run when bot is stopped or via /cleardb in DM)."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import get_settings
from bot.db.database import Database


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clear ShieldMod bot test data from SQLite DB")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also remove registered chats, chat settings (rules), and sub-admins",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = Database()
    await db.init()

    if not args.yes:
        scope = "ALL tables" if args.all else "punishments, logs, Gemini usage, spam bans"
        print(f"Database: {settings.database_path}")
        print(f"Will clear: {scope}")
        answer = input("Type yes to continue: ").strip().lower()
        if answer != "yes":
            print("Cancelled.")
            return

    counts = await db.clear_test_data(
        keep_chats=not args.all,
        keep_sub_admins=not args.all,
    )
    total = sum(counts.values())
    print(f"Cleared {total} row(s):")
    for table, n in counts.items():
        if n:
            print(f"  {table}: {n}")
    print("Done. Restart the bot to reset in-memory Gemini limits cache.")


if __name__ == "__main__":
    asyncio.run(main())

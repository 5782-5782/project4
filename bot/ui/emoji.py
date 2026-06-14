"""Premium-style emoji helpers for admin UI.

Custom emoji IDs can be replaced with your Telegram Premium emoji document IDs.
Fallback Unicode emojis are used when custom IDs are not configured.
"""

# Custom emoji IDs (set via env or replace with your premium emoji IDs)
CUSTOM_EMOJI = {
    "shield": "5372304520256577026",
    "robot": "5372304520256577027",
    "chart": "5372304520256577028",
    "fire": "5372304520256577029",
    "check": "5372304520256577030",
    "warn": "5372304520256577031",
    "ban": "5372304520256577032",
    "crown": "5372304520256577033",
    "gear": "5372304520256577034",
    "sparkles": "5372304520256577035",
}

# Unicode fallbacks for rich UI
E = {
    "shield": "🛡",
    "robot": "🤖",
    "chart": "📊",
    "fire": "🔥",
    "check": "✅",
    "warn": "⚠️",
    "ban": "🚫",
    "crown": "👑",
    "gear": "⚙️",
    "sparkles": "✨",
    "mute": "🔇",
    "pardon": "🕊",
    "rules": "📜",
    "clock": "⏱",
    "queue": "📥",
    "key": "🔑",
    "star": "⭐",
    "arrow": "➡️",
    "block": "⛔",
    "info": "ℹ️",
}


def bar(used: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = min(width, int(used / total * width))
    return "█" * filled + "░" * (width - filled)

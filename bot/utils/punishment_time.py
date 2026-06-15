from datetime import datetime, timedelta, timezone


def parse_utc_datetime(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def relative_time_ru(iso_str: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    delta = now - parse_utc_datetime(iso_str)
    if delta.total_seconds() < 0:
        return "в будущем"
    total_sec = int(delta.total_seconds())
    if total_sec < 60:
        return "только что"
    if total_sec < 3600:
        minutes = total_sec // 60
        return f"{minutes} мин назад"
    if total_sec < 86400:
        hours = total_sec // 3600
        return f"{hours} ч назад"
    days = delta.days
    if days < 7:
        return f"{days} дн назад"
    if days < 30:
        weeks = days // 7
        return f"{weeks} нед назад"
    return f"{days} дн назад"


def format_punishment_moment(iso_str: str, now: datetime | None = None) -> str:
    dt = parse_utc_datetime(iso_str)
    absolute = dt.strftime("%d.%m.%Y %H:%M UTC")
    relative = relative_time_ru(iso_str, now=now)
    return f"{absolute} ({relative})"

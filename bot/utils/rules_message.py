import html

from bot.ui.emoji import E

TELEGRAM_MESSAGE_LIMIT = 4096


def build_rules_view_messages(rules_text: str) -> list[str]:
    header = (
        f"{E['rules']} <b>Правила чата</b>\n\n"
        f"Отправьте новый текст правил следующим сообщением или .txt файлом.\n"
        f"Отмена: /cancel\n\n"
    )
    rules = rules_text or ""
    if not rules.strip():
        return [f"{header}<b>Текущие:</b>\n<pre>(пусто)</pre>"]

    first_prefix = f"{header}<b>Текущие:</b>\n<pre>"
    cont_prefix = "<b>Текущие (продолжение):</b>\n<pre>"
    close = "</pre>"

    first_max = TELEGRAM_MESSAGE_LIMIT - len(first_prefix) - len(close)
    cont_max = TELEGRAM_MESSAGE_LIMIT - len(cont_prefix) - len(close)

    raw_chunks: list[str] = [rules[:first_max]]
    pos = first_max
    while pos < len(rules):
        raw_chunks.append(rules[pos : pos + cont_max])
        pos += cont_max

    messages: list[str] = []
    for index, chunk in enumerate(raw_chunks):
        escaped = html.escape(chunk)
        if index == 0:
            messages.append(f"{first_prefix}{escaped}{close}")
        else:
            messages.append(f"{cont_prefix}{escaped}{close}")
    return messages

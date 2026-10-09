"""Напоминание при сжатии контекста. Сжатие не останавливается.

Первое в этом чате — нормально. Второе и дальше портят работу так же, как
второй и третий compact у Claude: в контекст попадает пересказ пересказа.
Сообщение видит человек, не модель.

Если есть план in-progress, заявленный этой сессией, — в additional_context
для модели: продолжать план целиком, журнал на диске, не ждать человека.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOME = Path.home() / ".cursor"
PLANS = HOME / "plans"
STATE = HOME / "logs" / "plan-sessions.json"
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def frontmatter_value(text: str, key: str) -> str:
    block = FRONTMATTER.match(text)
    if not block:
        return ""
    found = re.search(rf"^{key}:\s*(.+)$", block.group(1), re.M)
    return found.group(1).strip() if found else ""


def active_plan_for(session: str) -> str | None:
    if not session or not STATE.is_file():
        return None
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for number, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("session", "")) != session:
            continue
        for path in PLANS.glob(f"{number}-*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if frontmatter_value(text, "status") == "in-progress":
                return number
    return None


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except (ValueError, UnicodeError):
        request = {}
    if request.get("is_first_compaction", True):
        message = (
            "Контекст сжат в первый раз. Когда заполнится ещё примерно "
            "половина, лучше новый чат, чем второе сжатие."
        )
    else:
        message = (
            "Это уже не первое сжатие этого чата. Дальше качество падает. "
            "Новый чат и фраза «продолжи план N»: план на диске, пересказ чата — нет."
        )
    out: dict = {"user_message": message}
    session = str(
        request.get("conversation_id") or request.get("session_id") or ""
    )
    number = active_plan_for(session)
    if number:
        out["additional_context"] = (
            f"[plan-end-to-end] План {number} со status: in-progress ведёт эта "
            "сессия. Сжатие контекста не пауза и не повод ждать человека. "
            f"Прочитай журнал ~/.cursor/plans/{number}-*.md («В работе на момент "
            "записи») и продолжай со следующей незакрытой истории до конца плана. "
            "Скилл execute-plan / continuity.md; Stop-хук guard_plan_in_progress."
        )
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)

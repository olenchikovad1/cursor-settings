"""Жёсткий отказ на команды, которые в локальном allow Клода были запрещены.

Allowlist Cursor совпадает по префиксу: «git push» разрешает и force-push.
Этот хук возвращает запрет до выполнения. Всё остальное — allow, чтобы не
спрашивать лишний раз. Сломался — тоже allow: иначе одна ошибка останавливает
любую команду.
"""

import json
import re
import sys

FORCE = re.compile(r"(?:--force(?:-with-lease)?|(?:^|\s)-f)(?:\s|$)")
SHARED = re.compile(r"(?:^|[\s:/])(main|master|develop)(?:\s|$)")
RM = re.compile(r"(?:^|[;&|]\s*)rm\s+-[a-zA-Z]*r[a-zA-Z]*f\S*\s+(\./|~/|/|~|C:\\)", re.I)
DOCKER = re.compile(
    r"docker\s+volume\s+(?:rm|prune)|docker\s+system\s+prune|"
    r"docker\s+compose\s+down\b[^\n]*\s-v\b|docker\s+compose\s+down\s+--volumes",
    re.I)
DROPDB = re.compile(r"(?:^|[;&|]\s*)dropdb\b", re.I)
ALEMBIC = re.compile(r"\balembic\s+(upgrade|downgrade)\b", re.I)


def emit(permission: str, message: str = "") -> None:
    payload = {"permission": permission}
    if message:
        payload["user_message"] = message
        payload["agent_message"] = message
    print(json.dumps(payload, ensure_ascii=True))


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except (ValueError, UnicodeError):
        emit("allow")
        return 0
    command = request.get("command") or (request.get("tool_input") or {}).get("command") or ""
    if FORCE.search(command) and SHARED.search(command):
        emit("deny", "Force-push в main, master или develop запрещён.")
        return 0
    if RM.search(command):
        emit("deny", "Рекурсивное удаление от корня или домашнего каталога запрещено.")
        return 0
    if DOCKER.search(command):
        emit("deny", "Удаление томов и данных docker запрещено без отдельного решения.")
        return 0
    if DROPDB.search(command):
        emit("deny", "dropdb запрещён.")
        return 0
    if ALEMBIC.search(command):
        emit("ask", "Миграция alembic upgrade или downgrade — нужно подтверждение.")
        return 0
    emit("allow")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        emit("allow")
        sys.exit(0)

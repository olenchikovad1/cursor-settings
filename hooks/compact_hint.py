"""Напоминание при сжатии контекста. Сжатие не останавливается.

Первое в этом чате — нормально. Второе и дальше портят работу так же, как
второй и третий compact у Claude: в контекст попадает пересказ пересказа.
Сообщение видит человек, не модель.
"""

import json
import sys


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except (ValueError, UnicodeError):
        request = {}
    if request.get("is_first_compaction", True):
        message = ("Контекст сжат в первый раз. Когда заполнится ещё примерно "
                   "половина, лучше новый чат, чем второе сжатие.")
    else:
        message = ("Это уже не первое сжатие этого чата. Дальше качество падает. "
                   "Новый чат и фраза «продолжи план N»: план на диске, пересказ чата — нет.")
    print(json.dumps({"user_message": message}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)

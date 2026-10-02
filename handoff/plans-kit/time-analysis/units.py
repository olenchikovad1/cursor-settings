"""Единица работы: то, к чему относится измеренная минута.

Учёт до этого знал только историю, и 74% измеренного времени не попадало ни в
одну: часть — потому что истории не открывались при выполнении планов (в планах
026–038 открыто 90 историй из 224), часть — потому что работа начиналась
просьбой в разговоре и историей не становилась никогда.

**Единица — ход.** Реплика владельца открывает её, следующая его реплика
закрывает. Такая граница не требует от владельца ничего: он не объявляет ни
начала, ни конца, а пачка замечаний, надиктованная одной репликой, ложится
ровно в одну единицу.

**Пауза владельца в работу не входит.** Длительность единицы складывается из
её шагов, а не считается по краям: между репликами ловились интервалы до
39 минут, когда владелец просто отходил от терминала.

**Род времени отделён от работы.** Единица, в которой ничего не правилось и не
запускалось, — разговор: обсуждение, планирование, разбор. Считать её наравне с
работой значит мерить обсуждение мерой сделанного.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# Виды шагов, которые меняют состояние, не трогая файлов. По инструменту это
# не определить: `Bash` с `cat` — чтение, `Bash` с `git commit` — работа, и
# первый прогон на живых данных дал 90 единиц «работы» из 90 именно поэтому.
CHANGING_KINDS = {"git: коммит", "git: пуш", "деплой", "бд: миграции",
                  "стенд: поднять"}


def _ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_real_user(entry: dict) -> bool:
    """Настоящая реплика человека, а не результат инструмента.

    Результаты приходят как `type: user`; приняв их за реплику, мы породили бы
    отдельную единицу на каждый вызов инструмента.
    """
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return False


def _first_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()[:200]
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return (b.get("text") or "").strip()[:200]
    return ""


def units_from_transcript(path: Path) -> list[dict]:
    """Единицы работы одного транскрипта.

    Длительность берётся из шагов внутри единицы (`extract_steps`), поэтому
    паузы владельца в неё не попадают: шаг, оборванный его репликой, не
    измеряется вовсе.
    """
    from extract_steps import steps_from_transcript

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Границы: моменты настоящих реплик владельца.
    bounds: list[tuple[datetime, str]] = []
    for e in entries:
        if e.get("type") == "user" and _is_real_user(e):
            t = _ts(e.get("timestamp"))
            if t:
                bounds.append((t, _first_text(e)))
    if not bounds:
        return []

    steps = steps_from_transcript(path)
    out: list[dict] = []
    for i, (start, text) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else None
        mine = [s for s in steps
                if (t := _ts(s.get("started"))) and t >= start
                and (end is None or t < end)]
        if not mine:
            continue
        seconds = round(sum(float(s.get("sec") or 0) for s in mine), 1)
        tools: dict[str, int] = {}
        kinds: set[str] = set()
        edited = created = 0
        for s in mine:
            for name, n in (s.get("tools") or {}).items():
                tools[name] = tools.get(name, 0) + n
            kinds.add(s.get("kind") or "прочее")
            edited += int(s.get("files_edited") or 0)
            created += int(s.get("files_created") or 0)
        out.append({
            "session": path.stem,
            "opened_by": text,
            "started": _ts(mine[0]["started"]).isoformat(),
            "finished": _ts(mine[-1]["started"]).isoformat(),
            "seconds": seconds,
            "steps": len(mine),
            "genre": genre(kinds, edited, created),
            "kinds": sorted(kinds),
            "tools": tools,
            "model": mine[0].get("model"),
            "effort": mine[0].get("effort"),
        })
    return out


def genre(kinds: set[str], edited: int, created: int) -> str:
    """«работа» или «разговор».

    Работа — единица, в которой что-то изменилось: правились или создавались
    файлы, либо шёл шаг вида, меняющего состояние снаружи файлов.

    Всё остальное — разговор: чтение, поиск, прогон тестов без правок. Это
    обсуждение, планирование и разбор; они стоят времени, но сделанного собой
    не показывают, и держать их отдельно обязательно — иначе час обсуждения
    встанет в один ряд с часом работы.
    """
    if edited or created:
        return "работа"
    if kinds & CHANGING_KINDS:
        return "работа"
    return "разговор"


def overlaps(us: list[dict]) -> list[tuple[str, str]]:
    """Пары пересекающихся единиц ВНУТРИ одной сессии.

    Между сессиями пересечение законно: две сессии работают одновременно, и это
    разные потоки работы, а не двойной счёт одной и той же минуты. Внутри одной
    сессии пересечение означает, что минута попала в две единицы, и тогда сумма
    по единицам перестаёт сходиться с суммой по замерам.
    """
    bad = []
    by_session: dict[str, list[dict]] = {}
    for u in us:
        by_session.setdefault(u.get("session") or "?", []).append(u)
    for group in by_session.values():
        ordered = sorted(group, key=lambda u: u["started"])
        for a, b in zip(ordered, ordered[1:]):
            if _ts(b["started"]) < _ts(a["finished"]):
                bad.append((a["started"], b["started"]))
    return bad


def coverage(steps: list[dict], us: list[dict]) -> dict:
    """Сколько замеров не попало ни в одну единицу.

    Доля называется числом и должна не расти со временем: растущая означает,
    что работа уходит мимо учёта, и обе суммы — по единицам и по замерам —
    перестают сходиться.
    """
    windows = [(_ts(u["started"]), _ts(u["finished"])) for u in us]
    total = len(steps)
    total_sec = sum(float(s.get("sec") or 0) for s in steps)
    unassigned_n = 0
    unassigned_sec = 0.0
    for s in steps:
        t = _ts(s.get("ts") or s.get("started"))
        if t is None or not any(a <= t <= b for a, b in windows):
            unassigned_n += 1
            unassigned_sec += float(s.get("sec") or 0)
    return {
        "steps": total,
        "units": len(us),
        "unassigned_n": unassigned_n,
        "unassigned_pct": round(unassigned_n / total * 100, 1) if total else 0.0,
        "unassigned_minutes": round(unassigned_sec / 60, 1),
        "total_minutes": round(total_sec / 60, 1),
    }

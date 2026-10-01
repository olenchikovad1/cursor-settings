#!/usr/bin/env python3
"""Разбор транскрипта в записи о шагах: что делалось, сколько заняло.

    py extract_steps.py --transcript <файл>     разобрать один
    py extract_steps.py --recent [N]            N последних транскриптов
    py extract_steps.py --summary [N]           сводка по похожим шагам
    py extract_steps.py --json                  выдать записи, а не отчёт

Что считается шагом. То же, что и в `_time_estimate_common`: **объявленное
действие** — запись с вызовом инструмента, перед которой был текст. Вызовы без
текста перед ними — продолжение уже объявленного шага (дочитать файл, добрать
grep), а не новый шаг.

Как меряется длительность. От объявления шага до объявления следующего. Это
намеренно: в интервал входит и работа инструмента, и моё обдумывание перед
следующей фразой. Пользователь ждёт ровно это время, значит оно и есть
стоимость шага.

Две ловушки, на которых наивный разбор врёт, обе проверены на живых данных:

* **Результаты инструментов приходят как записи `user`.** Если считать их
  репликой человека, каждый шаг рвётся на первом же вызове и все длительности
  выходят по секунде.
* **Паузы между ходами.** Между соседними записями попадались интервалы до
  2353 секунд — это пользователь отходил от терминала. Интервал, пересекающий
  настоящую реплику человека, в замер не идёт.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS = CLAUDE_HOME / "projects"

# Строки, которые выглядят как шаг, но им не являются.
NOT_A_STEP = re.compile(
    r"^(Оценка задачи целиком|Пересчёт|Пайплайн|Оценка:)", re.I)
TIMEISH = re.compile(r"\d+\s*(мин|сек|час)", re.I)
MAX_STEP_WORDS = 16          # с запасом к правилу «≤12 слов»


def host() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def _ts(entry: dict):
    t = entry.get("timestamp")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_real_user(entry: dict) -> bool:
    """Настоящая реплика человека, а не результат инструмента."""
    c = (entry.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in c)
    return False


def _blocks(entry: dict):
    c = (entry.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def steps_from_transcript(path: Path, only_last_turn: bool = False) -> list[dict]:
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

    if only_last_turn:
        last_user = 0
        for i, e in enumerate(entries):
            if e.get("type") == "user" and _is_real_user(e):
                last_user = i
        entries = entries[last_user:]

    steps: list[dict] = []
    cur: dict | None = None
    pending_text: list[str] = []

    def close(cur: dict | None, end, cut: bool):
        if cur is None:
            return
        cur["sec"] = round((end - cur["_t0"]).total_seconds(), 1) if end else 0.0
        cur["cut"] = cut
        cur.pop("_t0", None)
        steps.append(cur)

    for e in entries:
        t = _ts(e)
        if e.get("type") == "user":
            if _is_real_user(e):
                close(cur, t, cut=True)   # реплика человека рвёт замер
                cur, pending_text = None, []
            continue
        if e.get("type") != "assistant":
            continue

        # Режим снимается с той записи, которая шаг открыла. `perTurnEffort`
        # важнее `effort`: первое — уровень, на котором ход реально шёл,
        # второе — настройка сессии, которая могла быть переопределена.
        effort = e.get("perTurnEffort") or e.get("effort")
        model = (e.get("message") or {}).get("model")

        for b in _blocks(e):
            if b.get("type") == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    pending_text.append(txt)
            elif b.get("type") == "tool_use":
                name = b.get("name")
                inp = b.get("input") or {}
                if pending_text:
                    # объявленное действие — новый шаг
                    phrase = ""
                    for chunk in pending_text:
                        for ln in chunk.splitlines():
                            if ln.strip():
                                phrase = ln.strip()
                    pending_text = []
                    if (phrase and len(phrase.split()) <= MAX_STEP_WORDS
                            and TIMEISH.search(phrase)
                            and not NOT_A_STEP.match(phrase)):
                        close(cur, t, cut=False)
                        cur = {"phrase": phrase, "_t0": t,
                               "started": t.isoformat() if t else None,
                               "model": model, "effort": effort,
                               "tools": {}, "files_edited": set(),
                               "files_created": set(), "commands": 0}
                if cur is None:
                    continue
                cur["tools"][name] = cur["tools"].get(name, 0) + 1
                fp = inp.get("file_path")
                if name in ("Edit", "MultiEdit") and fp:
                    cur["files_edited"].add(fp)
                elif name == "Write" and fp:
                    cur["files_created"].add(fp)
                elif name in ("Bash", "PowerShell"):
                    cur["commands"] += 1
                    cmd = (inp.get("command") or "")[:400]
                    cur.setdefault("cmds", []).append(cmd)

    close(cur, None, cut=True)

    out = []
    for s in steps:
        # Шаг, оборванный репликой человека, не измерим: в интервал попала
        # пауза, пока пользователь читал и печатал. Такие в замер не идут —
        # иначе в данные попадают «шаги» по 40 минут, которых не было.
        # По той же причине выбрасывается последний шаг хода: за ним ничего
        # не следует, и конца у него нет. Один шаг с хода — приемлемая
        # плата за то, что определение длительности остаётся одним.
        if s.get("cut"):
            continue
        if not s.get("sec") or s["sec"] <= 0 or s["sec"] > 3600:
            continue
        rec = {
            "phrase": s["phrase"],
            "sec": s["sec"],
            "started": s["started"],
            "tools": s["tools"],
            "files_edited": len(s["files_edited"]),
            "files_created": len(s["files_created"]),
            "commands": s["commands"],
            "session": path.stem,
            "host": host(),
            # Режим — None там, где транскрипт его не назвал. Пропуск честнее
            # догадки: по нему видно, какой части выборки доверять нельзя.
            "model": s.get("model"),
            "effort": s.get("effort"),
        }
        rec["kind"] = classify(rec, s.get("cmds") or [])
        out.append(rec)
    return out


# --- вид деятельности --------------------------------------------------------
#
# Группировать шаги по формулировке бесполезно: на 568 шагах из восьми сессий
# нашлось всего две группы с тремя повторами — фразы каждый раз разные.
# Время предсказывает не формулировка, а ВИД деятельности плюс масштаб, как и
# описывал пользователь: тесты стоят примерно одинаково, новый файл чуть
# дороже правки, правки в десяти файлах — примерно как один новый файл.

_R = lambda p: re.compile(p, re.I | re.M)  # noqa: E731

# `cd ... && ` — навигация, а не работа. Без её отсечения каждая вторая команда
# классифицируется как «cd» и всё сваливается в «прочее»: в сыром корпусе `cd`
# оказался самой частой головной командой после `echo`.
CD_PREFIX = _R(r"^\s*cd\s+\S+\s*(&&|;)\s*")

# Порядок правил — по убыванию того, что определяет длительность.
#
# Сначала внешние процессы: если шаг ждёт сьют, сборку или деплой, время
# определяют они, что бы ещё в шаге ни делалось. Потом ручные проверки. Потом
# правки файлов — и только потом попутные git/grep. Последнее важно: 42%
# шагов с правками содержат попутный grep или git status, и без такого порядка
# они уезжали бы в «поиск по коду», хотя работа там была совсем другая.
WAIT_RULES = [
    ("деплой", _R(r"\bssh\b[^\n]*(docker|compose|deploy|build|pull|up\b)|deploy\.sh|\bscp\b")),
    ("стенд: поднять", _R(r"compose\s+(up|restart|down|start|stop)|docker\s+(start|restart)")),
    ("тесты: весь сьют", _R(r"pytest(?![^\n|]*(-m |::|-k |test_[\w]*\.py))")),
    ("тесты: целевые", _R(r"pytest[^\n|]*(-m |::|-k |test_[\w]*\.py)")),
    ("тесты: фронт", _R(r"npm (run )?test|vitest|jest")),
    ("сборка фронта", _R(r"npm run build|vite build|\btsc\b")),
    ("бд: миграции", _R(r"\balembic\b")),
    ("бд: запросы", _R(r"\bpsql\b|select .*\bfrom\b|insert into|update .* set ")),
    ("линт и типы", _R(r"\bruff\b|\beslint\b|\boxlint\b|\bmypy\b|\bflake8\b|--noEmit")),
]
GIT_RULES = [
    ("git: пуш", _R(r"\bgit\b[^\n|]*\bpush\b")),
    ("git: коммит", _R(r"\bgit\b[^\n|]*\bcommit\b")),
    ("git: состояние", _R(r"\bgit\b[^\n|]*(status|log|diff|branch|ls-tree|rev-parse|show|check-)")),
]
HTTP_RE = _R(r"\bcurl\b|\bwget\b|Invoke-WebRequest")
SEARCH_RE = _R(r"\bgrep\b|\brg\b|\bfind\b")
# Чтение файлов идёт через оболочку, а не через `Read`: из 64 замеров вида только
# пять пришли инструментом. Без этого правила 59 шагов уезжали в «команду прочую»
# и там усреднялись, хотя чтение вдвое дешевле — 12 с на вызов против 20 с.
# Голова команды, а не вхождение: `cat x | grep y` — это поиск, и его ловит
# SEARCH_RE раньше.
READ_RE = _R(r"^\s*(cat|head|tail|less|more|type)\b|^\s*sed\s+-n\b")


def classify(rec: dict, cmds: list[str]) -> str:
    blob = "\n".join(CD_PREFIX.sub("", c) for c in cmds)
    for name, pat in WAIT_RULES:
        if pat.search(blob):
            return name

    tools = rec.get("tools") or {}
    # Ручная проверка глазами — это работа через браузер, а не curl. В сыром
    # корпусе такие шаги висели в «прочем», пока инструменты браузера не стали
    # отдельным признаком.
    if any(t.startswith("mcp__") and ("rowser" in t or "chrome" in t) for t in tools):
        return "ручная проверка в браузере"
    if HTTP_RE.search(blob):
        return "ручная проверка по HTTP"

    created, edited = rec["files_created"], rec["files_edited"]
    if created and edited:
        return "код: новое и правки"
    if created:
        return "код: новый файл"
    if edited:
        return "код: правки (1 файл)" if edited == 1 else "код: правки (2+ файла)"

    for name, pat in GIT_RULES:
        if pat.search(blob):
            return name
    if SEARCH_RE.search(blob) or any(t in tools for t in ("Grep", "Glob")):
        return "поиск по коду"
    if "Read" in tools or READ_RE.search(blob):
        return "чтение файлов"
    # Разведка снаружи: документация, чужие репозитории, догрузка инструмента.
    # Стоит вдвое дешевле «команды прочей» и раньше целиком составляла «прочее»
    # (23 шага из 23 на корпусе 10.09.2026), то есть вид был, а имени у него не было.
    if any(t in tools for t in ("WebFetch", "WebSearch", "ToolSearch")):
        return "веб-разведка"
    if rec["commands"]:
        return "команда прочая"
    return "прочее"


def recent_transcripts(limit: int = 5) -> list[Path]:
    if not PROJECTS.is_dir():
        return []
    files = [p for d in PROJECTS.iterdir() if d.is_dir()
             for p in d.glob("*.jsonl")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


# --- сводка по похожим шагам -------------------------------------------------

STOP = set("и в на по с из для не что это как а но же ли бы то так уже "
           "мин сек час минуты минут секунд около примерно".split())


def normalize(phrase: str) -> str:
    """Грубая нормализация фразы: без чисел, знаков и стоп-слов."""
    p = re.sub(r"[^\wА-Яа-яёЁ ]+", " ", phrase.lower())
    p = re.sub(r"\d+", " ", p)
    words = [w[:6] for w in p.split() if w not in STOP and len(w) > 2]
    return " ".join(words[:4])


def summarize(records: list[dict], by: str = "kind") -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in records:
        key = r.get("kind", "прочее") if by == "kind" else normalize(r["phrase"])
        groups.setdefault(key, []).append(r)
    out = []
    for key, rs in groups.items():
        secs = sorted(r["sec"] for r in rs)
        out.append({
            "key": key,
            "n": len(rs),
            "median_sec": secs[len(secs) // 2],
            "min_sec": secs[0],
            "max_sec": secs[-1],
            "edited": max(r["files_edited"] for r in rs),
            "created": max(r["files_created"] for r in rs),
            "examples": [r["phrase"] for r in rs[:3]],
        })
    out.sort(key=lambda g: (-g["n"], -g["median_sec"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript")
    ap.add_argument("--recent", nargs="?", type=int, const=5)
    ap.add_argument("--summary", nargs="?", type=int, const=5)
    ap.add_argument("--min-n", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.transcript:
        paths = [Path(a.transcript)]
    else:
        paths = recent_transcripts(a.summary or a.recent or 5)

    records: list[dict] = []
    for p in paths:
        records.extend(steps_from_transcript(p))

    if a.json:
        print(json.dumps(records, ensure_ascii=False))
        return 0

    if a.summary is not None:
        groups = [g for g in summarize(records) if g["n"] >= a.min_n]
        print(f"транскриптов: {len(paths)} | шагов: {len(records)} | "
              f"групп от {a.min_n} повторов: {len(groups)}\n")
        for g in groups[:30]:
            print(f"  n={g['n']:3}  медиана {g['median_sec']:6.0f}с  "
                  f"({g['min_sec']:.0f}–{g['max_sec']:.0f})  "
                  f"правок≤{g['edited']} новых≤{g['created']}")
            print(f"        {g['examples'][0][:76]}")
        return 0

    secs = sorted(r["sec"] for r in records)
    if not secs:
        print("шагов не найдено")
        return 0
    q = lambda f: secs[min(int(len(secs) * f), len(secs) - 1)]
    print(f"транскриптов: {len(paths)} | шагов: {len(records)}")
    print(f"секунды: медиана {q(.5):.0f} | p25 {q(.25):.0f} | "
          f"p75 {q(.75):.0f} | p90 {q(.9):.0f} | max {secs[-1]:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stop-хук Cursor: не бросать план со status: in-progress.

Перенос с ~/.claude/hooks/guard_plan_in_progress.py. Там возврат был
`decision: block`; здесь — `followup_message`, иначе Cursor ход не продлевает.

Планы и журналы — в ~/.cursor. Заявка сессии — logs/plan-sessions.json.
Сжатие контекста и «за сессию не успеть» не снимают обязательство довести
план: остановить может только человек (слова / notes/<NNN>-paused / --release).
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOME = Path.home() / ".cursor"
PLANS = HOME / "plans"
NOTES = PLANS / "notes"
PROJECTS = HOME / "projects.d"
LOCAL = HOME / "local.json"
STORIES = HOME / "time-analysis" / "records" / "stories.jsonl"
STATE = HOME / "logs" / "plan-sessions.json"
LOG = HOME / "logs" / "plan-guard.log"

MAX_IDLE_BLOCKS = 30
MAX_BLOCKS = 400
RECENT = timedelta(hours=6)

STORY_HEADING = re.compile(r"^##\s+US-(\d{4})\.", re.M)
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)

_STOP_VERB = r"(?:останов\w*|стоп(?!-)|прерв\w*|притормоз\w*|закончи\w*|заканчива\w*|хватит|пауз\w*)"
_STOP_WHAT = (
    r"(?:план\w*|работ\w*|истори\w*|на сегодня|на этом|"
    r"после (?:текущ\w*|этого|этой)|блок(?:а|е|ом|ов)?(?![а-яё]))"
)
STOP_RE = re.compile(
    rf"{_STOP_VERB}(?:\W+\w+){{0,3}}\W+{_STOP_WHAT}"
    rf"|{_STOP_WHAT}(?:\W+\w+){{0,3}}\W+{_STOP_VERB}",
    re.IGNORECASE,
)
RESUME_RE = re.compile(
    r"продолж\w*|поехали|дальше по плану|сним\w+ пауз\w*|возобнов\w*",
    re.IGNORECASE,
)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _user_texts(transcript_path: str) -> list[str]:
    """Реплики человека из транскрипта Cursor (role=user), свежие первыми."""
    path = Path(transcript_path or "")
    if not path.is_file():
        return []
    out: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            role = entry.get("role") or entry.get("type")
            if role not in ("user",):
                continue
            message = entry.get("message") or entry
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                out.append(content)
                continue
            if not isinstance(content, list):
                continue
            if all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
            if parts:
                out.append(" ".join(parts))
    out.reverse()
    return out


def stop_requested(transcript_path: str) -> bool:
    for text in _user_texts(transcript_path):
        if RESUME_RE.search(text):
            return False
        if STOP_RE.search(text):
            return True
    return False


def plan_number(path: Path) -> str:
    return path.name[:3]


def frontmatter_value(text: str, key: str) -> str:
    block = FRONTMATTER.match(text)
    if not block:
        return ""
    found = re.search(rf"^{key}:\s*(.+)$", block.group(1), re.M)
    return found.group(1).strip() if found else ""


def story_records() -> list[dict]:
    if not STORIES.exists():
        return []
    records: list[dict] = []
    for line in STORIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def project_paths(name: str) -> list[Path]:
    found: list[Path] = []
    manifest = PROJECTS / f"{name}.md"
    if manifest.exists():
        text = manifest.read_text(encoding="utf-8", errors="replace")
        expr = frontmatter_value(text, "path_expr")
        if expr:
            found.append(Path(expr))
    if LOCAL.exists():
        try:
            local = json.loads(LOCAL.read_text(encoding="utf-8"))
        except ValueError:
            local = {}
        paths = local.get("cache") or {}
        value = paths.get(name) if isinstance(paths, dict) else None
        if isinstance(value, str):
            found.append(Path(value))
        elif isinstance(value, dict):
            for candidate in value.values():
                if isinstance(candidate, str):
                    found.append(Path(candidate))
    return found


def inside(directory: str | None, roots: list[Path]) -> bool:
    if not directory or not roots:
        return False
    try:
        here = Path(directory).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            here.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def note(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        with io.open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp.replace(STATE)
    except OSError as e:
        note(f"состояние не записано: {e}")


def owner_of(number: str, state: dict) -> str:
    rec = state.get(number)
    return str(rec.get("session", "")) if isinstance(rec, dict) else ""


def claim(number: str, session: str, cwd: str | None, state: dict) -> None:
    rec = state.get(number) or {}
    rec.update({
        "session": session,
        "cwd": cwd or rec.get("cwd", ""),
        "claimed": rec.get("claimed")
        or datetime.now().isoformat(timespec="seconds"),
    })
    state[number] = rec
    save_state(state)
    note(f"{number}: заявлен сессией {session}")


def release(number: str, state: dict | None = None) -> None:
    state = load_state() if state is None else state
    if state.pop(number, None) is not None:
        save_state(state)
        note(f"{number}: освобождён")


def unfinished_plan(session: str = "", cwd: str | None = None) -> dict | None:
    if not PLANS.exists():
        return None

    records = story_records()
    state = load_state()
    now = datetime.now(tz=UTC)

    for path in sorted(PLANS.glob("[0-9][0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if frontmatter_value(text, "status") != "in-progress":
            continue

        number = plan_number(path)
        if (NOTES / f"{number}-paused").exists():
            continue

        stories = {match.group(1) for match in STORY_HEADING.finditer(text)}
        if not stories:
            continue

        mine = [r for r in records if str(r.get("plan", "")) == number]
        blocked = {str(r.get("us", ""))[-4:] for r in mine if r.get("blocked")}
        closed = {
            str(r.get("us", ""))[-4:]
            for r in mine
            if r.get("finished") and not r.get("blocked")
        }
        opened = [r for r in mine if not r.get("finished") and not r.get("blocked")]

        latest = max(
            (
                m
                for r in mine
                for m in (moment(r.get("finished")), moment(r.get("started")))
                if m
            ),
            default=None,
        )
        try:
            mtime_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            mtime_dt = None
        # Нет story-start — всё равно держим, если файл плана трогали недавно
        # или эту сессию уже заявили: иначе перенос на Cursor молчит.
        claimed_at = moment((state.get(number) or {}).get("claimed"))
        activity = latest or mtime_dt or claimed_at
        if activity is None or now - activity > RECENT:
            continue

        remaining = len(stories) - len((closed | blocked) & stories)
        if remaining <= 0:
            release(number, state)
            continue

        owner = owner_of(number, state)
        if owner and owner != session:
            continue
        if not owner:
            if not inside(cwd, project_paths(frontmatter_value(text, "project"))):
                continue
            if session:
                claim(number, session, cwd, state)

        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = 0
        done = len(closed & stories)
        stuck = len(blocked & stories)
        return {
            "number": number,
            "closed": done,
            "blocked": stuck,
            "total": len(stories),
            "open_story": str(opened[-1].get("us", "")) if opened else "",
            "fingerprint": f"{done}:{len(mine)}:{mtime}",
            "state": state,
        }

    return None


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig") or "{}"
        payload = json.loads(raw)
    except (ValueError, OSError, UnicodeError):
        return 0

    session = str(
        payload.get("conversation_id")
        or payload.get("session_id")
        or ""
    )

    try:
        found = unfinished_plan(session=session, cwd=payload.get("cwd"))
    except Exception as e:  # noqa: BLE001
        note(f"НЕОЖИДАННАЯ ОШИБКА: {type(e).__name__}: {e}")
        return 0

    if not found:
        return 0

    number = found["number"]

    stopping = False
    try:
        stopping = stop_requested(str(payload.get("transcript_path") or ""))
    except Exception as e:  # noqa: BLE001
        note(f"разбор транскрипта не удался: {type(e).__name__}: {e}")

    if stopping and not found["open_story"]:
        note(f"{number}: человек просил остановиться, открытых историй нет — отпускаю")
        return 0

    state = found["state"]
    rec = state.get(number) or {}

    blocks = int(rec.get("blocks") or 0)
    same = rec.get("fingerprint") == found["fingerprint"]
    idle = (int(rec.get("idle_blocks") or 0) + 1) if same else 0

    rec["fingerprint"] = found["fingerprint"]
    rec["idle_blocks"] = idle

    if idle > MAX_IDLE_BLOCKS or blocks >= MAX_BLOCKS:
        why = "холостой цикл" if idle > MAX_IDLE_BLOCKS else "потолок блокировок"
        note(f"{number}: замолчал — {why} (blocks={blocks}, idle={idle})")
        state[number] = rec
        save_state(state)
        return 0

    rec["blocks"] = blocks + 1
    rec["last_block"] = datetime.now().isoformat(timespec="seconds")
    state[number] = rec
    save_state(state)

    if stopping:
        note(f"{number}: остановка по просьбе человека после {found['open_story']}")
        reason = (
            f"Человек попросил остановить план {number}. Доведи "
            f"{found['open_story']} до демонстрации, закрой учёт, запиши в "
            "журнал, что остановлено по просьбе. Следующую историю не начинай. "
            "Заявку не снимай."
        )
    else:
        if found["open_story"]:
            what_next = (
                f"история {found['open_story']} открыта — доведи до демонстрации"
            )
        else:
            what_next = (
                "возьми следующую незакрытую историю по «Порядок и зависимости»"
            )
        note(
            f"{number}: держу ход ({blocks + 1}), "
            f"закрыто {found['closed']}/{found['total']}"
        )
        stuck = found.get("blocked") or 0
        stuck_said = (
            f", заблокировано {stuck} — их двигать нечем" if stuck else ""
        )
        reason = (
            f"План {number} ведёт эта сессия: закрыто "
            f"{found['closed']} из {found['total']} историй{stuck_said}. "
            f"Не заканчивай ход — {what_next}. "
            "План выполняется целиком, без остановок между историями и без "
            "ожидания человека после сжатия контекста. "
            "Решение «за сессию не успеть» принимать не тебе. "
            "Неблокирующие вопросы — в "
            f"~/.cursor/plans/notes/{number}-open-questions.md. "
            "Остановить может только человек словами или "
            f"plans/notes/{number}-paused / plan_session.py --release {number}."
        )

    return _emit({"followup_message": reason})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        raise SystemExit(0)

"""Памятка с эталонами в начале хода и факт хода в журнал Cursor.

Транскрипт Cursor не содержит вызовов инструментов, поэтому шаг меряется
событиями хука: начало хода, текст ответа, вызовы, конец. Запись идёт в
~/.cursor/time-analysis и относится к версии «работа в Cursor» по времени.

Запуск: py -X utf8 ./hooks/time_estimate.py <событие>
"""

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(Path.home() / ".cursor" / "time-analysis"))

TURNS = Path.home() / ".cursor" / "time-analysis" / "turns"
MIN_SECONDS = 3

EDIT = {"StrReplace", "Edit", "EditNotebook", "MultiEdit"}
WRITE = {"Write"}


def read_request() -> dict:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return {}
    try:
        # utf-8-sig, а не utf-8: Cursor на Windows подаёт JSON с BOM, и чистый
        # utf-8 даёт ошибку разбора — запрос пустой, ход не меряется вовсе.
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return {}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True))


def state_path(request: dict) -> Path | None:
    cid = request.get("conversation_id") or request.get("session_id")
    if not cid:
        return None
    return TURNS / f"{cid}.json"


def load(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path: Path | None, state: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def reminder() -> str:
    try:
        from check_time_estimate_start import REMINDER, reference_line
        line = reference_line()
    except Exception:
        return ""
    return REMINDER + (("\n" + line) if line else "")


def on_prompt(request: dict) -> dict:
    path = state_path(request)
    save(path, {
        "start": datetime.now(timezone.utc).isoformat(),
        "generation": request.get("generation_id"),
        "tools": [],
        "text": "",
        "logged": False,
    })
    text = reminder()
    out = {"continue": True}
    if text:
        out["additional_context"] = text
    return out


def fix_mojibake(text: str) -> str:
    """Вернуть русский текст, который Cursor отдал как UTF-8, прочитанный в cp1251.

    На Windows текст ответа приходит в хук так («РџСЂРёРЅСЏС‚Рѕ» вместо
    «Принято»), и «сек»/«мин» в нём не находятся — каждый ход считался бы
    ходом без оценки. Байт, которого в cp1251 нет (0x98 в «И»), приходит
    символом с тем же кодом. Нормальный русский текст обратно в UTF-8 не
    собирается, и тогда он остаётся как был.
    """
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1251")
        except UnicodeEncodeError:
            if ord(ch) > 0xFF:
                return text
            out.append(ord(ch))
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return text


def merge_text(old: str, new: str) -> str:
    """Склеить куски ответа одного хода.

    afterAgentResponse приходит на каждый фрагмент текста, а не один раз на
    весь ответ. Замена затирала строку оценки, если она была в раннем куске,
    а последний абзац её уже не содержал. Полный снимок (новый текст начинается
    со старого) заменяет, повтор того же куска не дублируется.
    """
    if not new:
        return old
    if not old or new.startswith(old):
        return new
    if old.endswith(new) or new in old:
        return old
    return old + "\n" + new


def on_text(request: dict) -> dict:
    path = state_path(request)
    state = load(path)
    state["text"] = merge_text(state.get("text") or "",
                               fix_mojibake(request.get("text") or ""))
    save(path, state)
    return {}


def on_tool(request: dict) -> dict:
    path = state_path(request)
    state = load(path)
    if not state.get("start"):
        state["start"] = datetime.now(timezone.utc).isoformat()
    name = request.get("tool_name") or ""
    cmd = ""
    if name == "Shell":
        cmd = str((request.get("tool_input") or {}).get("command") or "")[:240]
    state.setdefault("tools", []).append({"name": name, "cmd": cmd})
    save(path, state)
    return {}


def classify(tools: list[dict]) -> str:
    names = [t.get("name") for t in tools]
    cmds = "\n".join(t.get("cmd") or "" for t in tools)
    writes = sum(n in WRITE for n in names)
    edits = sum(n in EDIT for n in names)
    if any(x in cmds for x in ("pytest", "unittest", "npm test", "vitest")):
        return "тесты: целевые"
    if "git push" in cmds:
        return "git: пуш"
    if "git commit" in cmds:
        return "git: коммит"
    if writes and edits:
        return "код: новое и правки"
    if writes:
        return "код: новый файл"
    if edits > 1:
        return "код: правки (2+ файла)"
    if edits == 1:
        return "код: правки (1 файл)"
    if any(n in ("Grep", "Glob") for n in names):
        return "поиск по коду"
    if "Read" in names:
        return "чтение файлов"
    if cmds.strip():
        return "команда прочая"
    return "прочее"


def announcement_lines(text: str) -> list[str]:
    from _time_estimate_common import ESTIMATE_PATTERN, _word_count
    lines = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*")
        if ESTIMATE_PATTERN.search(stripped) and _word_count(stripped) <= 16:
            lines.append(stripped)
    return lines


TRANSCRIPTS = Path.home() / ".cursor" / "projects"


def transcript_turn_text(cid: str) -> str | None:
    """Текст ассистента в последнем ходе по транскрипту Cursor.

    afterAgentResponse отдаёт только итоговое сообщение хода, а строки оценки
    стоят перед вызовами инструментов — в промежуточных сообщениях. Они есть
    только в транскрипте. None — транскрипта нет вовсе; "" — транскрипт есть,
    но текущий ход в него ещё не записан, и проверять нечего.
    """
    if not cid:
        return None
    files = list(TRANSCRIPTS.glob(f"*/agent-transcripts/{cid}/{cid}.jsonl"))
    files += list(TRANSCRIPTS.glob(f"*/agent-transcripts/{cid}.jsonl"))
    if not files:
        return None
    try:
        rows = files[0].read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    texts: list[str] = []
    for row in rows:
        try:
            entry = json.loads(row)
        except ValueError:
            continue
        role = entry.get("role") or entry.get("type")
        if role == "user":
            texts = []
            continue
        if role != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [b.get("text") or "" for b in content if b.get("type") == "text"]
    return "\n".join(texts)


def on_stop(request: dict) -> dict:
    if request.get("status") not in (None, "completed"):
        return {}
    if int(request.get("loop_count") or 0) > 0:
        return {}
    path = state_path(request)
    state = load(path)
    tools = state.get("tools") or []
    if not tools or state.get("logged"):
        return {}
    try:
        started = datetime.fromisoformat(state["start"])
    except (KeyError, ValueError):
        return {}
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    state["logged"] = True
    save(path, state)
    if elapsed >= MIN_SECONDS:
        try:
            import steps_store
            kind = classify(tools)
            step = {
                "sec": round(elapsed, 1),
                "kind": kind,
                "started": state["start"],
                "host": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                "files_created": sum(t.get("name") in WRITE for t in tools),
                "files_edited": sum(t.get("name") in EDIT for t in tools),
                "commands": sum(t.get("name") == "Shell" for t in tools),
                "model": "cursor",
                "unit": "turn",
                "phrase": (state.get("text") or "").strip().splitlines()[:1] or [""],
            }
            step["phrase"] = step["phrase"][0][:160]
            steps_store.append([steps_store.to_record(step, steps_store.load_step_refs())])
        except Exception:
            pass
    text = state.get("text") or ""
    from_transcript = transcript_turn_text(
        request.get("conversation_id") or request.get("session_id") or "")
    try:
        from _time_estimate_common import step_verdict
        lines = announcement_lines(text) + announcement_lines(from_transcript or "")
    except Exception:
        return {}
    if not lines and from_transcript == "":
        return {}
    if not lines:
        return {"followup_message": (
            "Ход с действиями прошёл без строки оценки. Перед следующим шагом "
            "одна строка: действие и время из эталона, состав в скобках.")}
    verdict = next((step_verdict(line) for line in lines if step_verdict(line)), None)
    if verdict:
        return {"followup_message": "Оценка шага: " + verdict}
    return {}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    request = read_request()
    event = event or request.get("hook_event_name") or ""
    if event == "sessionStart":
        text = reminder()
        emit({"additional_context": text} if text else {})
    elif event == "beforeSubmitPrompt":
        emit(on_prompt(request))
    elif event == "afterAgentResponse":
        emit(on_text(request))
    elif event == "postToolUse":
        emit(on_tool(request))
    elif event == "stop":
        emit(on_stop(request))
    else:
        emit({})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        emit({})
        sys.exit(0)

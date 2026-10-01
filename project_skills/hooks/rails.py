"""Единая точка входа хуков библиотеки в Cursor.

    py -X utf8 .cursor/hooks/rails.py <событие>

Cursor запускает команду хука на каждое событие отдельно. Шесть проверок по
отдельному процессу на каждую команду оболочки — это полсекунды задержки на
каждом шаге работы, и такую задержку выключают вместе с проверками. Поэтому
процесс один: он читает запрос, опрашивает проверки, поставленные в проект
(файлы рядом с этим), и собирает их ответы в один.

Каждая проверка — модуль с таблицей `HANDLES = {"<событие>": функция}`.
Функция получает запрос и общее состояние беседы и возвращает ответ события
или `None`. Упавшая проверка пропускается: хук, роняющий работу, мешает
сильнее пропущенного нарушения.
"""

import importlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _rails_common as g  # noqa: E402

# Порядок важен только для `postToolUse`: подпись в коммите проверяется до того,
# как ветка уедет на удалённый.
GUARDS = [
    "require_skills",
    "guard_shared_branch_push",
    "guard_commit_trailers",
    "push_task_branch",
    "guard_claim_without_run",
    "guard_ui_boundary",
]

PERMISSION_EVENTS = {"beforeShellExecution", "beforeMCPExecution", "preToolUse"}
RANK = {"allow": 0, "ask": 1, "deny": 2}


def installed() -> list:
    out = []
    for name in GUARDS:
        if (HERE / f"{name}.py").is_file():
            try:
                out.append(importlib.import_module(name))
            except Exception as failure:  # noqa: BLE001
                print(f"rails: {name} не загрузился: {failure}", file=sys.stderr)
    return out


def merge(event: str, answers: list[dict]) -> dict:
    out: dict = {}
    if event in PERMISSION_EVENTS:
        permission = "allow"
        for answer in answers:
            value = answer.get("permission", "allow")
            if RANK.get(value, 0) > RANK[permission]:
                permission = value
        out["permission"] = permission
    for key in ("user_message", "agent_message", "additional_context", "followup_message"):
        parts = [a[key] for a in answers if a.get(key)]
        if parts:
            out[key] = "\n\n".join(parts)
    env = {}
    for answer in answers:
        env.update(answer.get("env") or {})
    if env:
        out["env"] = env
    if event == "beforeSubmitPrompt":
        out["continue"] = True
    return out


def main() -> None:
    request = g.read_request()
    event = (sys.argv[1] if len(sys.argv) > 1 else "") or request.get("hook_event_name", "")
    state = g.load_state(request)
    answers = []
    for module in installed():
        handler = getattr(module, "HANDLES", {}).get(event)
        if handler is None:
            continue
        try:
            answer = handler(request, state)
        except Exception as failure:  # noqa: BLE001
            print(f"rails: {module.__name__} на {event} упал: {failure}", file=sys.stderr)
            continue
        if answer:
            answers.append(answer)
    g.save_state(request, state)
    result = merge(event, answers)
    if result:
        print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0)


if __name__ == "__main__":
    main()

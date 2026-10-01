"""Служебные подписи не попадают в тело коммита.

# skill: process-git
# events: beforeShellExecution, postToolUse

Правило говорит: тело коммита отвечает на «почему», и служебные подписи вроде
`Co-Authored-By` в нём не пишутся — они не несут информации для читателя
истории и занимают место, в котором должна быть причина.

В Cursor подпись приходит двумя путями, и ловятся они в разные моменты:

1. **Модель пишет её в команде сама.** Это видно до выполнения, и команда
   останавливается (`deny`): решать тут нечего, сообщение переписывается.
2. **Cursor дописывает свою сам** (`Co-authored-by: Cursor
   <cursoragent@cursor.com>`, «Made with Cursor») — уже после того, как
   команда ушла, и в аргументах её нет. Её видно только в записанном
   коммите, поэтому после коммита хук смотрит на последнее сообщение и
   говорит, если подпись там. Лечится это не хуком, а настройкой:
   Cursor Settings → Git & PRs → Attribution (до 3.11 — Agent → Attribution).

Список подписей берётся из `rails.json` (`forbidden_trailers`); подписи самого
Cursor проверяются всегда, кроме `allow_cursor_attribution: true`.
"""

import re

import _rails_common as g


def found_trailer(command: str, trailers: list[str]) -> str | None:
    """Подпись в тексте команды: в начале строки или сразу за кавычкой."""
    for name in trailers:
        pattern = rf"""(?:^|["'\n])[ \t]*{re.escape(name)}[ \t]*:"""
        if re.search(pattern, command, re.IGNORECASE | re.MULTILINE):
            return name
    if "--trailer" in command:
        for name in trailers:
            if re.search(rf"--trailer[ =]+[\"']?{re.escape(name)}", command, re.IGNORECASE):
                return name
    return None


def before_shell(request: dict, state: dict) -> dict | None:
    command = g.shell_command(request)
    if not command or not g.is_git(command, "commit"):
        return None
    name = found_trailer(command, g.setting(request, "forbidden_trailers", g.DEFAULT_TRAILERS))
    if not name:
        return None
    return {
        "permission": "deny",
        "user_message": f"Коммит остановлен: в сообщении служебная подпись {name} (process-git).",
        "agent_message": (
            f"В теле коммита служебная подпись `{name}`. Правило process-git: "
            f"служебные подписи не пишутся — они не отвечают на «почему». "
            f"Убери её и повтори коммит."),
    }


def after_shell(request: dict, state: dict) -> dict | None:
    command = g.shell_command(request)
    if not command or not g.is_git(command, "commit") or not g.shell_succeeded(request):
        return None
    lines = []
    for repo in g.touched_repositories(request):
        mark = g.forbidden_in_last_commit(request, repo)
        if mark:
            lines.append(
                f"{repo.name}: в последнем коммите служебная подпись «{mark}» "
                f"(process-git). Если это подпись самого Cursor — её дописывает "
                f"клиент, а не команда: выключается в Cursor Settings → Git & PRs → "
                f"Attribution. Ветка задачи ещё не отправлена — поправь сообщение "
                f"`git commit --amend` и скажи об этом человеку.")
    return {"additional_context": "\n".join(lines)} if lines else None


HANDLES = {"beforeShellExecution": before_shell, "postToolUse": after_shell}

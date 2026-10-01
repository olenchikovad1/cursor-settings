"""Коммит в ветке задачи уезжает на удалённый сам.

# skill: process-git
# events: postToolUse

Правило говорит: работа, лежащая только на одной машине, не сделана. Цена
известна поимённо: вечер работы остался на домашней машине, утром человек
решил, что всё отправлено, и продолжил на рабочей. Обнаружилось через сутки.

Пуш — действие механическое, у него нет повода ждать отдельной просьбы.
Общие ветки (`shared_branches` в `rails.json`) пропускаются молча: туда пуш
спрашивает человека, и это другая проверка.

Хук **ничего не блокирует**. Отказ — строка в контексте беседы, а не
остановка: сеть недоступна, удалённого нет, права не те — поводы сказать.

Не пушит, если в последнем коммите служебная подпись: отправленную ветку
пришлось бы переписывать, а переписывать опубликованную историю нельзя.

Отключается `auto_push: false` в `rails.json` или переменной окружения
`CURSOR_NO_AUTO_PUSH` (`CLAUDE_NO_AUTO_PUSH` тоже работает).
"""

import os
import subprocess
from pathlib import Path

import _rails_common as g

PUSH_TIMEOUT = 120


def nothing_to_push(cwd: Path, branch: str) -> bool:
    try:
        result = g.git("rev-list", "--count", f"origin/{branch}..{branch}", cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


def has_origin(cwd: Path) -> bool:
    try:
        return g.git("remote", "get-url", "origin", cwd=cwd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def publish(request: dict, cwd: Path, shared: set[str]) -> str | None:
    branch = g.current_branch(cwd)
    if not branch or branch in shared:
        return None
    name = cwd.name
    if not has_origin(cwd):
        return (f"{name}: ветка «{branch}» никуда не отправлена — у репозитория нет "
                f"удалённого «origin». Работа есть только на этой машине, скажи об "
                f"этом человеку.")
    mark = g.forbidden_in_last_commit(request, cwd)
    if mark:
        return (f"{name}: ветка «{branch}» не отправлена — в последнем коммите "
                f"служебная подпись «{mark}». Сначала поправь сообщение, потом пушь.")
    if (os.environ.get("CURSOR_NO_AUTO_PUSH") or os.environ.get("CLAUDE_NO_AUTO_PUSH")
            or g.setting(request, "auto_push", True) is False):
        return (f"{name}: автопуш отключён — коммит ветки «{branch}» остался только "
                f"на этой машине.")
    try:
        result = g.git("push", "-u", "origin", "HEAD", cwd=cwd, timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (f"{name}: пуш ветки «{branch}» не уложился в {PUSH_TIMEOUT} с и прерван. "
                f"Коммит остался только на этой машине.")
    except (OSError, subprocess.SubprocessError) as failure:
        return f"{name}: пуш ветки «{branch}» не выполнился: {failure}."
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "").strip().splitlines()
        return (f"{name}: пуш ветки «{branch}» отказал — "
                f"{reason[-1] if reason else 'без причины'}. Коммит остался только "
                f"здесь, скажи человеку.")
    if not nothing_to_push(cwd, branch):
        return (f"{name}: пуш ветки «{branch}» отчитался об успехе, но удалённая ветка "
                f"с местной не сошлась. Проверь, куда она уехала.")
    return f"{name}: ветка «{branch}» отправлена в origin."


def after_shell(request: dict, state: dict) -> dict | None:
    command = g.shell_command(request)
    if not command or not g.is_git(command, "commit") or not g.shell_succeeded(request):
        return None
    shared = set(g.setting(request, "shared_branches", g.DEFAULT_SHARED)) | set(g.DEFAULT_SHARED)
    lines = [line for repo in g.touched_repositories(request)
             if (line := publish(request, repo, shared)) is not None]
    return {"additional_context": "\n".join(lines)} if lines else None


HANDLES = {"postToolUse": after_shell}

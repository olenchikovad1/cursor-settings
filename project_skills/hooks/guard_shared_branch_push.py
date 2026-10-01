"""Пуш в общую ветку спрашивает человека.

# skill: process-git
# events: beforeShellExecution

Правило говорит: в общие ветки — только через слияние ветки задачи и только с
явным разрешением человека, причём разрешение спрашивается каждый раз. Прямой
пуш обходит единственный момент, где изменение можно посмотреть до того, как
оно повлияет на других.

Ответ `ask`, а не `deny`. Разрешение должен давать человек, а не модель,
которая решила, что случай подходящий, — и Cursor спрашивает его на каждый
вызов отдельно, так что согласие на один пуш не переносится на следующий.

Общие ветки берутся из `rails.json` (`shared_branches`).
"""

import _rails_common as g


def target_branches(words: list[str], request: dict) -> list[str]:
    """Ветки назначения у `git push`. Ссылки нет — уедет текущая ветка."""
    remote_seen, refs = False, []
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            if word in ("--repo", "--exec", "--receive-pack", "-o", "--push-option"):
                i += 2
                continue
            i += 1
            continue
        if not remote_seen:
            remote_seen = True
        else:
            refs.append(word)
        i += 1

    if not refs:
        branch = g.current_branch(g.command_dir(request))
        return [branch] if branch else []

    out = []
    for ref in refs:
        dst = ref.split(":")[-1].lstrip("+")
        if dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        if dst:
            out.append(dst)
    return out


def before_shell(request: dict, state: dict) -> dict | None:
    command = g.shell_command(request)
    if not command:
        return None
    shared = g.setting(request, "shared_branches", g.DEFAULT_SHARED)
    for words in g.commands(command):
        rest = g.git_subcommand(words, "push")
        if rest is None:
            continue
        hit = sorted({b for b in target_branches(rest, request) if b in shared})
        if not hit:
            continue
        names = ", ".join(hit)
        return {
            "permission": "ask",
            "user_message": (
                f"Пуш в общую ветку: {names}. Правило process-git: в общие ветки — "
                f"только слиянием ветки задачи и только с вашего разрешения, "
                f"на каждый пуш отдельно. Разрешить именно этот?"),
            "agent_message": (
                f"Пуш в общую ветку {names} требует разрешения человека "
                f"(process-git). Не обходи вопрос другим способом пуша."),
        }
    return None


HANDLES = {"beforeShellExecution": before_shell}

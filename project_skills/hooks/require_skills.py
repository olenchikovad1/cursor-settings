"""Скиллы области открываются до правки, а не вспоминаются.

# skill: quality-review
# events: sessionStart, beforeSubmitPrompt, postToolUse

В Cursor описания скиллов и так лежат в каталоге беседы, но текст скилла
появляется только когда его открыли. Работа «по памяти о правиле» соблюдает
верхний уровень и теряет пункты: набор компонентов использовался честно, а
пункт того же правила про поведение списка нарушался девять раз подряд.

Хук закрывает это с двух сторон и ничего не зашивает про конкретные скиллы:
он **обходит папки** скиллов проекта и берёт то, что там лежит.

1. На старте беседы (`sessionStart`) — перечень скиллов проекта по областям:
   к каким папкам относится каждая группа. И отдельной строкой — какие имена
   совпадают с личными скиллами: при совпадении действует **проектная**
   копия, в ней нюансы проекта.
2. Во время работы (`postToolUse`) — после правки файла (инструментом или
   скриптом в оболочке) называет скиллы этой области, которые в беседе ещё
   не открывались. Один раз на скилл за беседу: повтор на каждую команду
   перестают читать.

Скилл считается открытым, если в беседе прочитан любой его файл или человек
вызвал его как `/имя`.

Область скилла берётся, в порядке убывания надёжности:

- поле `applies_to` в шапке скилла (пути-префиксы через запятую);
- иначе часть имени каталога до дефиса (`frontend-*`, `data-*`) и её папки
  из `rails.json` (`skill_areas`);
- иначе скилл общий: к папке не привязан и в напоминании не участвует.

Отвечает контекстом, а не остановкой: правки уже сделаны, останавливать
нечего.
"""

import re
from pathlib import Path

import _rails_common as g

# Правило перевода «часть имени скилла → папки проекта» по умолчанию. Заменяется
# целиком ключом `skill_areas` в `rails.json` — у каждого проекта своя раскладка.
DEFAULT_AREAS: dict[str, list[str]] = {
    "frontend": ["web/"],
    "backend": ["backend/"],
    "api": ["backend/app/api/"],
    "data": ["backend/app/models/", "backend/app/repo/", "backend/alembic/"],
    "architecture": ["backend/app/", "web/src/"],
    "ops": ["deploy/", "docker-compose.yml", "docker-compose.dev.yml"],
    "docs": ["manifest/", "README.md", "AGENTS.md", "CLAUDE.md"],
}

PROJECT_DIRS = (".cursor/skills", ".agents/skills", ".claude/skills")
PERSONAL_DIRS = (".cursor/skills", ".agents/skills", ".claude/skills")


def head_fields(text: str) -> dict[str, str]:
    """Нужные поля шапки без разбора YAML: многострочное значение склеивается."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in text[3:end].splitlines():
        if line[:1] in (" ", "\t") and key:
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        out[key] = "" if value in (">", ">-", "|", "|-") else value
    return out


def project_skills(root: Path) -> list[dict]:
    found: dict[str, dict] = {}
    for rel in PROJECT_DIRS:
        for path in sorted((root / rel).glob("*/SKILL.md")):
            name = path.parent.name
            if name in found:
                continue
            try:
                head = head_fields(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            applies = [p.strip() for p in head.get("applies_to", "").strip("[]").split(",")
                       if p.strip()]
            found[name] = {"name": name, "where": rel, "group": name.split("-", 1)[0],
                           "applies_to": applies}
    return sorted(found.values(), key=lambda s: s["name"])


def paths_of(skill: dict, mapping: dict[str, list[str]]) -> list[str]:
    return skill["applies_to"] or mapping.get(skill["group"], [])


def shadowed(root: Path) -> list[str]:
    home = Path.home()
    mine = {p.parent.name for rel in PERSONAL_DIRS for p in (home / rel).glob("*/SKILL.md")}
    theirs = {s["name"] for s in project_skills(root)}
    if root.resolve() == home.resolve():
        return []
    return sorted(mine & theirs)


def on_session_start(request: dict, state: dict) -> dict | None:
    g.prune_states()
    root = g.project_root(request)
    state["baseline"] = g.dirty_files(root)
    rows = project_skills(root)
    if not rows:
        return None
    mapping = g.setting(request, "skill_areas", DEFAULT_AREAS)
    by_group: dict[str, list[dict]] = {}
    for skill in rows:
        by_group.setdefault(skill["group"], []).append(skill)
    lines = [
        f"Скиллы проекта ({len(rows)}). Правило quality-review: перед правкой области "
        f"её скиллы открываются и читаются, а не вспоминаются. Раздел «Отступления в "
        f"этом проекте» в конце скилла — часть правила."]
    for group in sorted(by_group):
        where = ", ".join(paths_of(by_group[group][0], mapping)) or "общее"
        names = ", ".join(s["name"] for s in by_group[group])
        lines.append(f"- {group} ({where}): {names}")
    covered = shadowed(root)
    if covered:
        lines.append(
            f"Совпадают по имени с личными скиллами ({len(covered)}): {', '.join(covered)}. "
            f"В этом проекте действует проектная копия — открывай её "
            f"({rows[0]['where']}/<имя>/SKILL.md), а не личную.")
    return {"additional_context": "\n".join(lines)}


def on_prompt(request: dict, state: dict) -> dict | None:
    prompt = request.get("prompt") or ""
    opened = set(state.get("skills_opened", []))
    opened |= set(re.findall(r"(?:^|\s)/([a-z0-9][a-z0-9-]*)", prompt))
    state["skills_opened"] = sorted(opened)
    return None


def _note_read(request: dict, state: dict) -> None:
    path = g.read_path(request)
    if path is None:
        return
    m = re.search(r"[\\/]skills[\\/]([a-z0-9][a-z0-9-]*)[\\/][^\\/]+\.md$", str(path))
    if m:
        opened = set(state.get("skills_opened", []))
        opened.add(m.group(1))
        state["skills_opened"] = sorted(opened)


def after_tool(request: dict, state: dict) -> dict | None:
    _note_read(request, state)
    root = g.project_root(request)
    files: list[str] = []
    edited = g.edited_path(request)
    if edited is not None:
        rel = g.relative(edited, root)
        if rel:
            files = [rel]
    elif g.shell_command(request):
        files = g.changed_since_start(request, state)
    if not files:
        return None

    mapping = g.setting(request, "skill_areas", DEFAULT_AREAS)
    opened = set(state.get("skills_opened", []))
    missing = []
    for skill in project_skills(root):
        prefixes = paths_of(skill, mapping)
        if not prefixes or skill["name"] in opened:
            continue
        if any(name.startswith(prefix) for prefix in prefixes for name in files):
            missing.append(skill)
    fresh = g.once(state, "skills_reminded", [s["name"] for s in missing])
    if not fresh:
        return None
    where = {s["name"]: s["where"] for s in missing}
    listed = ", ".join(f"{where[n]}/{n}/SKILL.md" for n in fresh[:4])
    rest = f" и ещё {len(fresh) - 4}" if len(fresh) > 4 else ""
    return {"additional_context": (
        f"quality-review: правка затронула {', '.join(files[:3])}, а скиллы этой области "
        f"в беседе не открывались — {listed}{rest}. Открой и сверь правку: правило "
        f"теряется пунктами, а не целиком.")}


HANDLES = {
    "sessionStart": on_session_start,
    "beforeSubmitPrompt": on_prompt,
    "postToolUse": after_tool,
}

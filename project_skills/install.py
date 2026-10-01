"""Установка скиллов библиотеки в проект под Cursor.

Библиотека разложена по областям (`skills/<область>/<имя>/`), в проекте
скиллы лежат плоско: `<проект>/.cursor/skills/<область>-<имя>/`. Cursor
требует, чтобы имя каталога совпадало с `name` в шапке, — поэтому имя
каталога и есть каноническое имя правила.

Главное правило переноса: **существующая проектная копия не заменяется.** В
ней могут быть нюансы проекта — раздел «Отступления в этом проекте» и правки по
тексту. Установщик её только сравнивает и выкладывает различия; сливает их
агент вместе с человеком (`install-project-skills`).

Запуск:

    py -X utf8 install.py --list                      # что есть в библиотеке
    py -X utf8 install.py <проект> [отбор]            # план, ничего не меняет
    py -X utf8 install.py <проект> [отбор] --apply    # выполнить план

Отбор: `--area frontend,quality`, `--only process-git,quality-testing`,
`--skip ops-containers`. Без отбора — вся библиотека.

Что делает `--apply`:

1. кладёт новые скиллы с датой переноса, разделом отступлений и пометкой у
   ссылок на правила, которые в проект не ставились;
2. совпадающие копии не трогает, отличающиеся — тоже, только перечисляет;
3. копирует хуки выбранных скиллов и диспетчер `rails.py` в `.cursor/hooks/`
   (отличающийся файл хука заменяется только с `--update-hooks`);
4. дописывает `.cursor/hooks.json`, не трогая чужие записи;
5. заводит `.cursor/rails.json`, если настроек ещё нет ни в `.cursor`, ни в
   `.claude`.
"""

import argparse
import difflib
import io
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

LIB = Path(__file__).resolve().parent
SKILLS = LIB / "skills"
HOOKS = LIB / "hooks"
FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
REF = re.compile(r"`([a-z]+-[a-z][a-z-]*)`(?! \(в библиотеке)")
MISSING_MARK = " (в библиотеке, сюда не переносилось)"
DEVIATIONS_HEAD = "## Отступления в этом проекте"
PROJECT_DIRS = (".cursor/skills", ".agents/skills", ".claude/skills")
PERSONAL_DIRS = (".cursor/skills", ".agents/skills", ".claude/skills", ".codex/skills")
RUNTIME = ("rails.py", "_rails_common.py")
PYTHON = "py -X utf8" if os.name == "nt" else "python3 -X utf8"

DEVIATIONS = f"""

---

{DEVIATIONS_HEAD}

Пока нет.

Правило перенесено из общей библиотеки. Если проекту оно подходит не целиком,
текст правила **не режется** — отступление записывается сюда, отдельным
блоком:

    ### <к какому пункту правила> — <дата>

    **Отступаем:** что делается иначе.
    **Потому что:** причина. Без причины отступление не действует — оно
    неотличимо от забытой правки.

Отступление читается как часть правила: код, соответствующий ему, находкой не
считается. Наверх, в библиотеку, оно не поднимается — это специфика проекта, а
не общее правило.

Больше трёх отступлений здесь означают, что правило проекту не подходит
целиком: это повод пересмотреть его, а не дописать четвёртое.
"""

# Как хук подключается к событию. Таймаут postToolUse покрывает автопуш:
# медленная сеть не должна обрывать хук посреди пуша.
EVENT_ENTRY = {
    "beforeShellExecution": {"matcher": "git"},
    "postToolUse": {"timeout": 150},
    "stop": {"loop_limit": 1},
}


# --- библиотека ---------------------------------------------------------------

def head_value(head: str, key: str) -> str:
    lines = head.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            value = line.split(":", 1)[1].strip()
            if value in (">", ">-", "|", "|-"):
                rest = []
                for nxt in lines[i + 1:]:
                    if not nxt[:1].isspace():
                        break
                    rest.append(nxt.strip())
                return " ".join(rest)
            return value
    return ""


def library() -> list[dict]:
    out = []
    for path in sorted(SKILLS.glob("*/*/SKILL.md")):
        text = io.open(path, encoding="utf-8").read()
        head = FM.match(text).group(1)
        area, short = path.parent.parent.name, path.parent.name
        out.append({
            "name": f"{area}-{short}", "area": area, "folder": path.parent,
            "description": head_value(head, "description"),
            "hooks": [h.strip() for h in head_value(head, "hooks").strip("[]").split(",")
                      if h.strip()],
        })
    return out


def select(skills: list[dict], args) -> list[dict]:
    split = lambda v: {x.strip() for item in (v or []) for x in item.split(",") if x.strip()}
    areas, only, skip = split(args.area), split(args.only), split(args.skip)
    known = {s["name"] for s in skills} | {s["area"] for s in skills}
    unknown = sorted((areas | only | skip) - known)
    if unknown:
        raise SystemExit(f"нет в библиотеке: {', '.join(unknown)} (см. --list)")
    chosen = [s for s in skills
              if (not areas or s["area"] in areas) and (not only or s["name"] in only)
              and s["name"] not in skip]
    return chosen


# --- то, что ляжет в проект ---------------------------------------------------

def annotate_missing(text: str, taken: set[str], known: set[str]) -> str:
    """Ссылка в никуда хуже отсутствующей: по ней идут. Вырезать её тоже
    нельзя — правило теряет связь. Поэтому она помечается."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in taken or name not in known:
            return m.group(0)
        return f"`{name}`{MISSING_MARK}"
    return REF.sub(repl, text)


def rendered(skill: dict, taken: set[str], known: set[str]) -> dict[str, str]:
    files = {}
    for path in sorted(skill["folder"].glob("*.md")):
        text = annotate_missing(io.open(path, encoding="utf-8").read(), taken, known)
        if path.name == "SKILL.md":
            m = FM.match(text)
            lines = [ln for ln in m.group(1).splitlines() if not ln.startswith("adapted:")]
            lines.append(f"adapted: {date.today()}")
            text = "---\n" + "\n".join(lines) + "\n---\n" + text[m.end():].rstrip("\n") + DEVIATIONS
        files[path.name] = text
    return files


def comparable(text: str) -> str:
    """Текст правила без того, что перенос добавляет сам: шапки, раздела
    отступлений, пометок о неперенесённых ссылках, разницы в пробелах."""
    m = FM.match(text)
    if m:
        text = text[m.end():]
    cut = text.find(DEVIATIONS_HEAD)
    if cut != -1:
        text = text[:cut].rstrip().removesuffix("---")
    text = text.replace(MISSING_MARK, "")
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def deviations_of(text: str) -> str:
    cut = text.find(DEVIATIONS_HEAD)
    if cut == -1:
        return ""
    # Шаблон записи в разделе стоит с отступом; настоящая запись — заголовок
    # третьего уровня с начала строки.
    body = text[cut + len(DEVIATIONS_HEAD):]
    entries = re.split(r"(?m)^(?=### )", body, maxsplit=1)
    return entries[1].strip() if len(entries) > 1 else ""


def refresh_marks(project: Path, taken: set[str]) -> list[str]:
    """Пометка «сюда не переносилось» у правила, которое теперь стоит, — ложь."""
    fixed = []
    for path in sorted((project / ".cursor" / "skills").glob("*/*.md")):
        text = io.open(path, encoding="utf-8").read()
        new = re.sub(r"`([a-z]+-[a-z][a-z-]*)`" + re.escape(MISSING_MARK),
                     lambda m: m.group(0) if m.group(1) not in taken else f"`{m.group(1)}`", text)
        if new != text:
            io.open(path, "w", encoding="utf-8", newline="\n").write(new)
            fixed.append(path.relative_to(project).as_posix())
    return fixed


# --- состояние проекта --------------------------------------------------------

def existing_copies(project: Path, name: str) -> list[Path]:
    return [project / d / name for d in PROJECT_DIRS if (project / d / name / "SKILL.md").is_file()]


def compare(copy: Path, wanted: dict[str, str]) -> dict:
    have = {p.name: io.open(p, encoding="utf-8").read() for p in sorted(copy.glob("*.md"))}
    changed = []
    for leaf in sorted(set(have) | set(wanted)):
        if leaf not in have:
            changed.append(f"{leaf}: в копии нет")
            continue
        if leaf not in wanted:
            changed.append(f"{leaf}: есть только в копии")
            continue
        a, b = comparable(wanted[leaf]).splitlines(), comparable(have[leaf]).splitlines()
        if a != b:
            diff = list(difflib.unified_diff(a, b, lineterm="", n=0))
            plus = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
            minus = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
            changed.append(f"{leaf}: в копии +{plus}/-{minus} строк против библиотеки")
    return {"changed": changed, "deviations": deviations_of(have.get("SKILL.md", ""))}


def personal_twins(name: str) -> list[str]:
    home = Path.home()
    return [f"~/{d}/{name}" for d in PERSONAL_DIRS if (home / d / name / "SKILL.md").is_file()]


def guard_events(hook: str) -> list[str]:
    text = io.open(HOOKS / hook, encoding="utf-8").read(3000)
    m = re.search(r"^# events:\s*(.+)$", text, re.M)
    return [e.strip() for e in m.group(1).split(",")] if m else []


def rails_defaults(hooks: list[str]) -> dict:
    sys.path.insert(0, str(HOOKS))
    import _rails_common as g  # noqa: E402
    out: dict = {}
    if "guard_shared_branch_push.py" in hooks:
        out["shared_branches"] = g.DEFAULT_SHARED
    if "guard_commit_trailers.py" in hooks:
        out["forbidden_trailers"] = g.DEFAULT_TRAILERS
        out["allow_cursor_attribution"] = False
    if "push_task_branch.py" in hooks:
        out["auto_push"] = True
    if "guard_claim_without_run.py" in hooks:
        import guard_claim_without_run as claim
        out["proof_commands"] = claim.DEFAULT_PROOF
    if "require_skills.py" in hooks:
        import require_skills as req
        out["skill_areas"] = req.DEFAULT_AREAS
    if "guard_ui_boundary.py" in hooks:
        import guard_ui_boundary as ui
        out["frontend_src"] = ui.DEFAULT_SRC
        out["ui_set_dir"] = f"{ui.DEFAULT_SRC}/components/ui"
        out["ui_root_dir"] = f"{ui.DEFAULT_SRC}/components"
        out["component_libraries"] = ui.DEFAULT_LIBRARIES
    return out


# --- план и выполнение --------------------------------------------------------

def make_plan(project: Path, chosen: list[dict], everything: list[dict]) -> dict:
    taken = {s["name"] for s in chosen} | {
        p.parent.name for d in PROJECT_DIRS for p in (project / d).glob("*/SKILL.md")}
    known = {s["name"] for s in everything}
    plan = {"new": [], "same": [], "differs": [], "claude_only": [], "twins": [],
            "hooks": [], "hook_files": [], "events": [], "rails": None, "taken": taken}
    for skill in chosen:
        wanted = rendered(skill, taken, known)
        copies = existing_copies(project, skill["name"])
        twins = personal_twins(skill["name"])
        if twins:
            plan["twins"].append((skill["name"], twins))
        if not copies:
            plan["new"].append((skill, wanted))
            continue
        cursor_side = [c for c in copies if c.relative_to(project).parts[0] != ".claude"]
        for copy in cursor_side:
            result = compare(copy, wanted)
            entry = (skill["name"], copy, result)
            plan["differs" if result["changed"] else "same"].append(entry)
        if not cursor_side:
            plan["claude_only"].append((skill["name"], copies[0],
                                        compare(copies[0], wanted)))
        for hook in skill["hooks"]:
            if hook not in plan["hooks"]:
                plan["hooks"].append(hook)
    for skill, _ in plan["new"]:
        for hook in skill["hooks"]:
            if hook not in plan["hooks"]:
                plan["hooks"].append(hook)

    if plan["hooks"]:
        target = project / ".cursor" / "hooks"
        for name in list(RUNTIME) + plan["hooks"]:
            src, dst = HOOKS / name, target / name
            if not dst.exists():
                state = "новый"
            elif src.read_bytes() == dst.read_bytes():
                state = "совпадает"
            else:
                state = "отличается"
            plan["hook_files"].append((name, state))
        events = []
        for hook in plan["hooks"]:
            events += [e for e in guard_events(hook) if e not in events]
        plan["events"] = events

        rails_paths = [project / ".cursor" / "rails.json", project / ".claude" / "rails.json"]
        present = next((p for p in rails_paths if p.is_file()), None)
        defaults = rails_defaults(plan["hooks"])
        if present is None:
            plan["rails"] = ("create", project / ".cursor" / "rails.json", defaults)
        else:
            try:
                have = json.loads(present.read_text(encoding="utf-8"))
            except ValueError:
                have = {}
            missing = {k: v for k, v in defaults.items() if k not in have}
            plan["rails"] = ("exists", present, missing)
    return plan


def merge_hooks_json(project: Path, events: list[str]) -> list[str]:
    path = project / ".cursor" / "hooks.json"
    data = {"version": 1, "hooks": {}}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("hooks", {})
    added = []
    for event in events:
        command = f"{PYTHON} .cursor/hooks/rails.py {event}"
        entries = data["hooks"].setdefault(event, [])
        if any("rails.py" in e.get("command", "") for e in entries):
            continue
        entries.append({"command": command, **EVENT_ENTRY.get(event, {})})
        added.append(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def apply(project: Path, plan: dict, update_hooks: bool) -> list[str]:
    done = []
    for skill, files in plan["new"]:
        target = project / ".cursor" / "skills" / skill["name"]
        target.mkdir(parents=True, exist_ok=True)
        for leaf, text in files.items():
            io.open(target / leaf, "w", encoding="utf-8", newline="\n").write(text)
        done.append(f"скилл {skill['name']} → .cursor/skills/{skill['name']}/")
    for path in refresh_marks(project, plan["taken"]):
        done.append(f"{path}: снята пометка у правил, которые теперь стоят")
    if plan["hooks"]:
        target = project / ".cursor" / "hooks"
        target.mkdir(parents=True, exist_ok=True)
        for name, state in plan["hook_files"]:
            if state == "новый" or (state == "отличается" and update_hooks):
                shutil.copyfile(HOOKS / name, target / name)
                done.append(f"хук {name} → .cursor/hooks/")
        added = merge_hooks_json(project, plan["events"])
        if added:
            done.append(f".cursor/hooks.json: подключены события {', '.join(added)}")
        kind, path, values = plan["rails"]
        if kind == "create":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            done.append(f"{path.relative_to(project).as_posix()} заведён со значениями по умолчанию")
    return done


def show(project: Path, plan: dict, chosen: list[dict]) -> None:
    print(f"Проект: {project}")
    print(f"Выбрано скиллов: {len(chosen)}\n")
    if plan["new"]:
        print("Поставить (новые):")
        for skill, _ in plan["new"]:
            print(f"  + {skill['name']}")
    if plan["same"]:
        print("Уже стоят и совпадают с библиотекой — не трогаю:")
        for name, copy, _ in plan["same"]:
            print(f"  = {name}  ({copy.relative_to(project).as_posix()})")
    if plan["differs"]:
        print("\nСТОЯТ И ОТЛИЧАЮТСЯ — не заменяю, слить вручную с человеком:")
        for name, copy, result in plan["differs"]:
            print(f"  ≠ {name}  ({copy.relative_to(project).as_posix()})")
            for line in result["changed"]:
                print(f"      {line}")
            if result["deviations"]:
                print("      в копии есть записанные отступления — сохранить их")
    if plan["claude_only"]:
        print("\nЕСТЬ ТОЛЬКО КОПИЯ ДЛЯ CLAUDE CODE (.claude/skills) — Cursor читает и её;")
        print("вторая копия в .cursor дала бы два скилла с одним именем. Спросить человека:")
        print("оставить .claude-копию (проектом пользуется и Claude Code) или перенести её")
        print("в .cursor/skills, сохранив отступления:")
        for name, copy, result in plan["claude_only"]:
            state = "совпадает" if not result["changed"] else "; ".join(result["changed"])
            print(f"  ~ {name}  ({state})")
            if result["deviations"]:
                print("      в копии есть записанные отступления")
    if plan["twins"]:
        print("\nЕсть личные тёзки — в проекте действует проектная копия (так скажет и")
        print("перечень на старте сессии; Cursor сам порядок не гарантирует):")
        for name, twins in plan["twins"]:
            print(f"  {name}: {', '.join(twins)}")
    if plan["hooks"]:
        print("\nХуки → .cursor/hooks/:")
        for name, state in plan["hook_files"]:
            note = " (заменю только с --update-hooks)" if state == "отличается" else ""
            print(f"  {name}: {state}{note}")
        print(f"События в .cursor/hooks.json: {', '.join(plan['events'])}")
        kind, path, values = plan["rails"]
        rel = path.relative_to(project).as_posix()
        if kind == "create":
            print(f"Настройки: {rel} будет заведён с ключами {', '.join(values)}")
        elif values:
            print(f"Настройки: {rel} есть; не заданы {', '.join(values)} — действуют значения "
                  f"по умолчанию")
        else:
            print(f"Настройки: {rel} есть, все ключи заданы")
        if any(h in plan["hooks"] for h in ("guard_commit_trailers.py", "push_task_branch.py")):
            print("\nПодпись Cursor в коммитах: выключить в Settings → Git & PRs → Attribution")
            print("(в старых версиях Agent → Attribution) — иначе коммиты агента будут с")
            print("трейлером, и автопуш их не отправит.")
        settings = project / ".claude" / "settings.json"
        if settings.is_file() and "guard_" in settings.read_text(encoding="utf-8", errors="ignore"):
            print("\nВ .claude/settings.json подключены хуки Claude Code. Если в Cursor включена")
            print("загрузка сторонних хуков, проверки сработают дважды — оставить одни.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", type=Path)
    ap.add_argument("--list", action="store_true", help="перечислить скиллы библиотеки")
    ap.add_argument("--area", action="append", help="области через запятую")
    ap.add_argument("--only", action="append", help="имена скиллов через запятую")
    ap.add_argument("--skip", action="append", help="исключить имена через запятую")
    ap.add_argument("--apply", action="store_true", help="выполнить план")
    ap.add_argument("--update-hooks", action="store_true",
                    help="заменить отличающиеся файлы хуков библиотечными")
    args = ap.parse_args()

    everything = library()
    if args.list:
        area = None
        for s in everything:
            if s["area"] != area:
                area = s["area"]
                print(f"\n[{area}]")
            hooks = f"  (хуки: {', '.join(s['hooks'])})" if s["hooks"] else ""
            print(f"  {s['name']}{hooks}\n      {s['description'][:150]}…")
        return 0
    if args.project is None:
        ap.error("нужен путь к проекту или --list")
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"нет такого каталога: {project}")

    chosen = select(everything, args)
    plan = make_plan(project, chosen, everything)
    show(project, plan, chosen)
    if not args.apply:
        print("\nЭто план. Выполнить: тот же запуск с --apply.")
        return 0
    done = apply(project, plan, args.update_hooks)
    print("\nСделано:")
    for line in done or ["ничего — всё уже на месте"]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

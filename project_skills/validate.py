"""Проверка целостности библиотеки под Cursor.

Ловит то, что глазами по шестидесяти файлам не проверяется: ссылку на
переименованное правило, разошедшееся происхождение, лист, до которого нельзя
дойти, хук без своего правила, каталог README, отставший от содержимого, — и
одну поломку, свойственную именно Cursor: описание в одну строку с «: » внутри
он читает как пустое, и скилл перестаёт подхватываться, не сообщая об этом.

Находки делятся по весу так же, как везде (`quality-automated-enforcement`):

- **ошибка** — сломает перенос или загрузку, возврат 1;
- **замечание** — не по стандарту, но работать не мешает, возврат 0.

Запуск:

    py -X utf8 validate.py [--strict]
    py -X utf8 validate.py --dir <проект>/.cursor/skills   # плоский каталог

`--strict` приравнивает замечания к ошибкам.
"""

import argparse
import io
import re
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
SKILLS = LIB / "skills"
HOOKS = LIB / "hooks"
FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
REF = re.compile(r"`([a-z]+-[a-z][a-z-]*)`")
AREAS = ("architecture", "data", "api", "frontend", "quality", "docs", "ops", "process")
REQUIRED = ("name", "description", "source")
DESCRIPTION_LIMIT = 1024
# Индекс читается целиком и каждый раз, поэтому у него есть предел. За ним
# пункты перестают различаться по весу — ровно то, ради чего пакет и заводился.
INDEX_LIMIT = 6000


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warns.append(f"{where}: {msg}")


def frontmatter(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Поля шапки и то, как каждое записано в файле (первая строка значения).

    Многострочное значение (`>-`, `|`) склеивается в строку: так его видит
    читатель, а сырая первая строка нужна для проверки на «: »."""
    m = FM.match(text)
    if not m:
        return {}, {}
    values: dict[str, str] = {}
    raw: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t") and key:
            values[key] = (values[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        raw[key] = value
        values[key] = "" if value in (">", ">-", "|", "|-") else value.strip("'\"")
    return values, raw


def hook_names(value: str) -> list[str]:
    return [h.strip() for h in value.strip("[]").split(",") if h.strip()]


def check_head(report: Report, rel: str, fm: dict, raw: dict) -> None:
    description = fm.get("description", "")
    written = raw.get("description", "")
    if written and written not in (">", ">-", "|", "|-") and ": " in written \
            and written[:1] not in ("'", '"'):
        report.err(rel, "описание в одну строку содержит «: » — Cursor читает его как "
                        "пустое; записать блоком `description: >-`")
    if len(description) > DESCRIPTION_LIMIT:
        report.err(rel, f"описание длиннее {DESCRIPTION_LIMIT} знаков ({len(description)}) — "
                        f"хвост обрезается в каталоге")
    if description and " Use " not in description:
        report.warn(rel, "в описании нет условия срабатывания «Use when…» — "
                         "правило может не подхватиться вовремя")


def check_package(report: Report, path: Path, rel: str, body: str) -> dict[str, str]:
    """Пакет: индекс плюс листья рядом. Возвращает тексты листьев.

    Ловит две поломки, которых глазами не видно: ссылку индекса в
    несуществующий лист и лист, на который из индекса не ссылается никто —
    Cursor листья сам не подгружает, и такой лист не прочтёт никто."""
    folder = path.parent
    files = {p.name: io.open(p, encoding="utf-8").read()
             for p in sorted(folder.glob("*.md")) if p.name != "SKILL.md"}
    linked = set(re.findall(r"\]\(([^)]+\.md)\)", body))
    prefix = rel[:-len("SKILL.md")]

    for target in sorted(linked):
        if target not in files:
            report.err(rel, f"ссылка на лист `{target}`, которого нет")
    for leaf in sorted(files):
        if leaf not in linked:
            report.err(f"{prefix}{leaf}", "лист не назван в индексе — до него нельзя дойти")

    if files:
        if len(body) > INDEX_LIMIT:
            report.warn(rel, f"индекс длиннее {INDEX_LIMIT} знаков ({len(body)})")
        if "## Проверка" in body:
            report.warn(rel, "«Проверка» в индексе — её место в листе")
    for leaf, text in sorted(files.items()):
        where = f"{prefix}{leaf}"
        if FM.match(text):
            report.err(where, "у листа есть шапка — она бывает только у индекса")
        if not text.lstrip().startswith("# "):
            report.warn(where, "лист не начинается с заголовка")
        if "## Проверка" not in text:
            report.warn(where, "в листе нет секции «Проверка»")
    return files


def check_body(report: Report, rel: str, body: str, leaves: dict) -> None:
    if not leaves and "## Проверка" not in body:
        report.warn(rel, "нет секции «Проверка» — правило нельзя ни подтвердить, ни опровергнуть")
    if "## Смежное" not in body:
        report.warn(rel, "нет секции «Смежное»")


def check_skills(report: Report) -> dict[str, dict]:
    skills: dict[str, dict] = {}
    for path in sorted(SKILLS.glob("*/*/SKILL.md")):
        rel = path.relative_to(LIB).as_posix()
        area, short = path.parent.parent.name, path.parent.name
        text = io.open(path, encoding="utf-8").read()
        if not FM.match(text):
            report.err(rel, "нет шапки")
            continue
        fm, raw = frontmatter(text)
        for field in REQUIRED:
            if not fm.get(field):
                report.err(rel, f"в шапке нет `{field}`")
        canon = f"{area}-{short}"
        if fm.get("name") != canon:
            report.err(rel, f"имя `{fm.get('name')}` не совпадает с каноническим `{canon}` — "
                            f"после переноса каталог будет `{canon}`, а Cursor требует совпадения")
        want = f"project_skills/skills/{area}/{short}"
        if fm.get("source") != want:
            report.err(rel, f"происхождение `{fm.get('source')}` разошлось с путём `{want}`")
        if canon in skills:
            report.err(rel, f"имя `{canon}` уже занято {skills[canon]['rel']}")
        check_head(report, rel, fm, raw)
        body = text[FM.match(text).end():]
        leaves = check_package(report, path, rel, body)
        check_body(report, rel, body, leaves)
        skills[canon] = {"rel": rel, "text": text + "".join(leaves.values()),
                         "hooks": hook_names(fm.get("hooks", ""))}
    return skills


def check_refs(report: Report, skills: dict[str, dict]) -> None:
    for name, s in skills.items():
        for ref in sorted(set(REF.findall(s["text"]))):
            if ref.split("-")[0] not in AREAS:
                continue
            if ref not in skills:
                report.err(s["rel"], f"ссылка `{ref}` не ведёт никуда")
            elif ref == name:
                report.warn(s["rel"], "ссылка на себя")


def check_hooks(report: Report, skills: dict[str, dict]) -> None:
    files = {p.name for p in HOOKS.glob("*.py")
             if not p.name.startswith("_") and p.name != "rails.py"}
    for name, s in skills.items():
        for hook in s["hooks"]:
            if hook not in files:
                report.err(s["rel"], f"назван хук `{hook}`, которого нет")
    for hook in sorted(files):
        text = io.open(HOOKS / hook, encoding="utf-8").read()
        owners = re.findall(r"^# skill:\s*(\S+)", text, re.M)
        if not owners:
            report.err(f"hooks/{hook}", "не назван скилл, который он держит (`# skill: <имя>`)")
        for owner in owners:
            if owner not in skills:
                report.err(f"hooks/{hook}", f"держит `{owner}`, которого нет в библиотеке")
            elif hook not in skills[owner]["hooks"]:
                report.err(f"hooks/{hook}", f"скилл `{owner}` его не называет — связь односторонняя")
        if not re.search(r"^# events:\s*\w+", text, re.M):
            report.err(f"hooks/{hook}", "не названы события (`# events: postToolUse, …`)")
        if not re.search(r"^HANDLES\s*=", text, re.M):
            report.err(f"hooks/{hook}", "нет `HANDLES` — диспетчер его не вызовет")
    rails = io.open(HOOKS / "rails.py", encoding="utf-8").read()
    for hook in sorted(files):
        if f'"{hook[:-3]}"' not in rails:
            report.err("hooks/rails.py", f"`{hook}` нет в GUARDS — он не будет вызываться")


def check_catalog(report: Report, skills: dict[str, dict]) -> None:
    """Каталог в README читают, чтобы выбрать, что ставить; отставший каталог
    прячет правило вернее, чем его отсутствие."""
    readme = io.open(LIB / "README.md", encoding="utf-8").read()
    listed = set(re.findall(r"^\|\s*`([a-z-]+)`", readme, re.M))
    for name in sorted(set(skills) - listed):
        report.err("README.md", f"`{name}` есть на диске, но нет в каталоге")
    for name in sorted(listed - set(skills)):
        report.err("README.md", f"`{name}` есть в каталоге, но нет на диске")


def check_flat(report: Report, root: Path) -> dict[str, dict]:
    """Плоский каталог скиллов — проектный или личный. Происхождение не
    обязательно, каталога README нет; остальное проверяется так же."""
    skills: dict[str, dict] = {}
    for path in sorted(root.glob("*/SKILL.md")):
        name = path.parent.name
        rel = f"{name}/SKILL.md"
        text = io.open(path, encoding="utf-8").read()
        if not FM.match(text):
            report.err(rel, "нет шапки")
            continue
        fm, raw = frontmatter(text)
        for field in ("name", "description"):
            if not fm.get(field):
                report.err(rel, f"в шапке нет `{field}`")
        if fm.get("name") != name:
            report.err(rel, f"имя `{fm.get('name')}` не совпадает с каталогом `{name}`")
        check_head(report, rel, fm, raw)
        body = text[FM.match(text).end():]
        leaves = check_package(report, path, rel, body)
        check_body(report, rel, body, leaves)
        skills[name] = {"rel": rel, "text": text + "".join(leaves.values())}
    return skills


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="считать замечания ошибками")
    ap.add_argument("--dir", type=Path, help="проверить плоский каталог скиллов")
    args = ap.parse_args()

    report = Report()
    if args.dir:
        root = args.dir.expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"нет такого каталога: {root}")
        skills = check_flat(report, root)
    else:
        skills = check_skills(report)
        check_refs(report, skills)
        check_hooks(report, skills)
        check_catalog(report, skills)

    for line in report.errors:
        print(f"ОШИБКА  {line}")
    for line in report.warns:
        print(f"замечание  {line}")
    total = f"{len(skills)} правил, {len(report.errors)} ошибок, {len(report.warns)} замечаний"
    if report.errors or (args.strict and report.warns):
        print(f"\n{total} — не проходит")
        return 1
    print(f"\n{total} — чисто" if not report.warns else f"\n{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

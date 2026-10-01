"""Компоненты живут в одном месте, чужая библиотека — внутри них.

# skill: frontend-ui
# events: postToolUse

Правило `frontend-ui` говорит: набор компонентов один, экраны берут элементы
оттуда, своей папки компонентов у экрана нет. Экран, которому «нужно чуть
иначе», заводит рядом свою таблицу, и через месяц их снова двадцать шесть.

Что видно машинно:

1. импорт сторонней библиотеки компонентов вне набора;
2. рукописный блок, для которого в наборе уже есть компонент (галочка,
   таблица);
3. выключенная постраничность без причины в пяти строках над ней;
4. вторая папка компонентов (только при `--scan`).

Проверяется **после** правки и по самому файлу на диске, а не по аргументам
инструмента: правка скриптом в оболочке иначе проходит мимо. Ответ —
предупреждение в контекст беседы, один раз на находку: правка уже сделана,
текст нужен тому, кто правил, чтобы починить в этом же ходу.

Перед слиянием то же правило прогоняется по всему дереву:

    py -X utf8 .cursor/hooks/guard_ui_boundary.py --scan

Раскладка фронтенда и список библиотек — из `rails.json`
(`frontend_src`, `ui_set_dir`, `ui_root_dir`, `component_libraries`).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rails_common as g  # noqa: E402

DEFAULT_SRC = "web/src"

DEFAULT_LIBRARIES = [
    "@radix-ui/", "@tanstack/react-table", "@headlessui/", "antd", "@mui/",
    "@chakra-ui/", "react-select",
]

IMPORT = re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""")

# Узко и по разметке: ложное срабатывание здесь дороже пропуска. Проектные
# признаки добавляются сюда парой «регулярка — чем заменить».
HANDMADE = (
    (re.compile(r"""<input[^>]*type=["']checkbox["']"""), "рукописная галочка",
     "Checkbox из набора, а для выбора строк таблицы — selectionColumn/useRowSelection"),
    (re.compile(r"<table[\s>]"), "рукописная таблица", "таблица из набора компонентов"),
)

PAGINATION_OFF = re.compile(r'pagination=["\']off["\']')
JUSTIFIED = re.compile(r'(?:Постраничность выключена|постраничност[ьи][^\n]{0,80}выключен)')


def layout(request: dict) -> dict[str, str]:
    src = str(g.setting(request, "frontend_src", DEFAULT_SRC)).rstrip("/")
    return {
        "src": src,
        "set": str(g.setting(request, "ui_set_dir", f"{src}/components/ui")).rstrip("/"),
        "root": str(g.setting(request, "ui_root_dir", f"{src}/components")).rstrip("/"),
    }


def unjustified_pagination_off(text: str) -> int:
    lines = text.split("\n")
    count = 0
    for index, line in enumerate(lines):
        if PAGINATION_OFF.search(line) and not JUSTIFIED.search(
                "\n".join(lines[max(0, index - 5):index])):
            count += 1
    return count


def offending_imports(text: str, libraries: list[str]) -> list[str]:
    found = set()
    for module in IMPORT.findall(text):
        for lib in libraries:
            if module == lib.rstrip("/") or module.startswith(lib):
                found.add(module)
                break
    return sorted(found)


def is_screen(rel: str, lay: dict[str, str]) -> bool:
    return (rel.startswith(f"{lay['src']}/") and rel.endswith((".ts", ".tsx", ".js", ".jsx"))
            and not rel.startswith(f"{lay['set']}/"))


def problems_in(text: str, rel: str, libraries: list[str]) -> list[str]:
    problems = []
    modules = offending_imports(text, libraries)
    if modules:
        problems.append(f"{rel}: импорт {', '.join(modules)} вне набора компонентов")
    for pattern, name, instead in HANDMADE:
        if pattern.search(text):
            problems.append(f"{rel}: {name} — вместо этого {instead}")
    unjustified = unjustified_pagination_off(text)
    if unjustified:
        problems.append(f"{rel}: постраничность выключена без причины ({unjustified} шт.) — "
                        f"включить или записать рядом, почему список ограничен по природе")
    return problems


def after_tool(request: dict, state: dict) -> dict | None:
    root = g.project_root(request)
    lay = layout(request)
    edited = g.edited_path(request)
    if edited is not None:
        rel = g.relative(edited, root)
        files = [rel] if rel else []
    elif g.shell_command(request):
        files = g.changed_since_start(request, state)
    else:
        return None
    libraries = g.setting(request, "component_libraries", DEFAULT_LIBRARIES)
    problems: list[str] = []
    for rel in files:
        if not is_screen(rel, lay):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        problems += problems_in(text, rel, libraries)
    fresh = g.once(state, "ui_reported", problems)
    if not fresh:
        return None
    return {"additional_context": (
        "frontend-ui — предупреждение по изменённому:\n  " + "\n  ".join(fresh) +
        "\nТо, что уже есть в наборе, экран не пишет руками, а сторонняя библиотека "
        "остаётся внутри набора. Нужно иначе — параметр в компонент набора, а не "
        "вторая реализация рядом.")}


def scan(root: Path, request: dict) -> list[str]:
    lay = layout(request)
    libraries = g.setting(request, "component_libraries", DEFAULT_LIBRARIES)
    src = root / lay["src"]
    problems: list[str] = []
    if not src.exists():
        return problems
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            if path.name in ("ui", "components") and rel not in (lay["root"], lay["set"]):
                problems.append(f"{rel}: вторая папка компонентов — набор должен быть один")
            continue
        if is_screen(rel, lay):
            problems += problems_in(path.read_text(encoding="utf-8", errors="replace"),
                                    rel, libraries)
    return problems


HANDLES = {"postToolUse": after_tool}


if __name__ == "__main__":
    if "--scan" in sys.argv[1:]:
        root = Path.cwd()
        request = {"cwd": str(root), "workspace_roots": [str(root)]}
        found = scan(root, request)
        for line in found:
            print(line)
        print("нарушений:", len(found))
        raise SystemExit(1 if found else 0)
    print("запуск руками: --scan из корня проекта", file=sys.stderr)

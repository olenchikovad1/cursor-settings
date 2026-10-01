#!/usr/bin/env python3
"""Имя проекта -> путь к нему на ЭТОЙ машине.

Планы в ~/.claude/plans/ ссылаются на проект по имени, а не по пути: пути на
домашней и офисной машине разные (`D:/Проекты/Laretto/assemblage-point` против
`C:/Projects/assemblage-point`). Разрешение — здесь.

    py resolve_project.py <name>              путь на stdout, код 0
    py resolve_project.py <name> --json       + remote/branch/источник
    py resolve_project.py --list              все проекты и их состояние
    py resolve_project.py --rescan [<name>]   сбросить кэш и обойти корни
    py resolve_project.py --register <name> <path>

Коды возврата: 0 — нашлось; 2 — не нашлось; 3 — неоднозначно (несколько
клонов одного remote), список кандидатов на stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _infra_common import (  # noqa: E402
    git,
    load_local,
    load_manifests,
    normalize_remote,
    save_local,
)

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".idea", ".vscode", "site-packages",
}


def repo_remote(repo: Path) -> str:
    rc, out, _ = git("remote", "get-url", "origin", cwd=repo)
    return out if rc == 0 else ""


def is_main_repo(path: Path) -> bool:
    """.git должен быть КАТАЛОГОМ.

    У ворктри `.git` — файл-ссылка `gitdir: ...`, а `origin` тот же, что у
    основного репозитория. Ворктри мы больше не создаём, но старые могли
    остаться от прошлых планов, и без этой проверки резолвер вернул бы
    ворктри вместо корня проекта.
    """
    return (path / ".git").is_dir()


def scan_roots(roots: list[str], depth: int) -> list[Path]:
    """Все основные git-репозитории под корнями, не глубже depth."""
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack:
            cur, lvl = stack.pop()
            if is_main_repo(cur):
                found.append(cur)
                continue  # внутрь репозитория не спускаемся
            if lvl >= depth:
                continue
            try:
                entries = list(cur.iterdir())
            except OSError:
                continue
            for e in entries:
                if not e.is_dir() or e.is_symlink():
                    continue
                if e.name in SKIP_DIRS or e.name.startswith("$"):
                    continue
                stack.append((e, lvl + 1))
    return found


def cache_hit(local: dict, name: str, want_remote: str) -> Path | None:
    p = local["cache"].get(name)
    if not p:
        return None
    path = Path(p)
    if not is_main_repo(path):
        return None
    if want_remote and normalize_remote(repo_remote(path)) != want_remote:
        return None  # по этому пути теперь лежит другой проект
    return path


def resolve(name: str, *, rescan: bool = False) -> tuple[str, Path | None, list[Path]]:
    """-> (источник, путь, кандидаты). Источник: fixed|cache|scan|none|ambiguous."""
    local = load_local()
    manifests = load_manifests()
    man = manifests.get(name)
    want = normalize_remote(man.get("remote", "")) if man else ""

    # path_expr — путь, одинаковый на всех машинах (например ~/.claude).
    # Такой проект незачем искать сканом: он лежит вне корней с проектами,
    # а добавлять в roots домашнюю папку только ради него — значит обходить
    # весь профиль пользователя на каждом rescan.
    if man and man.get("path_expr"):
        p = Path(os.path.expanduser(str(man["path_expr"])))
        if is_main_repo(p):
            return "fixed", p, [p]

    if not rescan:
        hit = cache_hit(local, name, want)
        if hit is not None:
            return "cache", hit, [hit]

    if not want:
        return "none", None, []

    matches = [r for r in scan_roots(local["roots"], int(local["depth"]))
               if normalize_remote(repo_remote(r)) == want]

    if len(matches) == 1:
        local["cache"][name] = str(matches[0]).replace("\\", "/")
        save_local(local)
        return "scan", matches[0], matches
    if len(matches) > 1:
        return "ambiguous", None, matches
    return "none", None, []


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("name", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--register", nargs=2, metavar=("NAME", "PATH"))
    a = ap.parse_args()

    if a.register:
        name, path = a.register
        p = Path(path.replace("\\", "/"))
        if not is_main_repo(p):
            print(f"нет каталога .git по пути: {p}", file=sys.stderr)
            return 2
        local = load_local()
        local["cache"][name] = str(p).replace("\\", "/")
        save_local(local)
        print(str(p).replace("\\", "/"))
        return 0

    if a.list:
        local = load_local()
        manifests = load_manifests()
        print(f"host: {local['host']}   roots: {', '.join(local['roots']) or '(нет)'}")
        for name in sorted(manifests):
            src, path, cands = resolve(name, rescan=a.rescan)
            if src == "ambiguous":
                print(f"  {name:24} НЕОДНОЗНАЧНО ({len(cands)} клонов)")
                for c in cands:
                    print(f"      {c}")
            elif path is None:
                print(f"  {name:24} не найден")
            else:
                rc, branch, _ = git("branch", "--show-current", cwd=path)
                print(f"  {name:24} {path}  [{branch or '?'}]  ({src})")
        return 0

    if not a.name:
        ap.print_help()
        return 2

    src, path, cands = resolve(a.name, rescan=a.rescan)

    if src == "ambiguous":
        print(f"проект {a.name}: найдено {len(cands)} клонов, выбрать нельзя:",
              file=sys.stderr)
        for c in cands:
            print(f"  {c}", file=sys.stderr)
        print("нужный зафиксировать: resolve_project.py --register "
              f"{a.name} <path>", file=sys.stderr)
        return 3

    if path is None:
        manifests = load_manifests()
        if a.name not in manifests:
            print(f"нет манифеста projects.d/{a.name}.md", file=sys.stderr)
        else:
            local = load_local()
            print(f"проект {a.name} не найден под корнями: "
                  f"{', '.join(local['roots']) or '(корни не заданы)'}",
                  file=sys.stderr)
            print("указать путь: resolve_project.py --register "
                  f"{a.name} <path>", file=sys.stderr)
        return 2

    out = str(path).replace("\\", "/")
    if a.json:
        rc, branch, _ = git("branch", "--show-current", cwd=path)
        man = load_manifests().get(a.name, {})
        print(json.dumps({
            "name": a.name,
            "path": out,
            "source": src,
            "branch": branch,
            "remote": repo_remote(path),
            "base_branch": man.get("default_base_branch"),
            "branch_prefix": man.get("branch_prefix"),
        }, ensure_ascii=False))
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

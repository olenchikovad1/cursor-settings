"""Общие примитивы инфраструктуры ~/.claude.

Здесь живёт всё, что нужно и резолверу путей, и синку состояния, и
автокоммиту: чтение local.json, чтение манифестов projects.d/, нормализация
git-remote и вычисление слага папки транскриптов.

Ничего специфичного для одной задачи тут быть не должно — иначе файл
превратится в свалку.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
from pathlib import Path

CLAUDE_HOME = Path(os.path.expanduser("~")) / ".cursor"
LOCAL_JSON = CLAUDE_HOME / "local.json"
PROJECTS_D = CLAUDE_HOME / "projects.d"
PROJECTS_DIR = CLAUDE_HOME / "projects"
PLANS_DIR = CLAUDE_HOME / "plans"
TRANSCRIPTS_DIR = CLAUDE_HOME / "transcripts"
MEMORY_DIR = CLAUDE_HOME / "memory"

GIT_TIMEOUT = 20


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(*args: str, cwd: Path | str | None = None, timeout: int = GIT_TIMEOUT):
    """Запустить git. Возвращает (returncode, stdout, stderr), не бросает."""
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", str(e)


def normalize_remote(url: str) -> str:
    """Привести git-remote к сравнимому виду.

    git@github.com:owner/repo.git и https://github.com/owner/repo/ должны
    сравниваться как равные — иначе резолвер не найдёт проект только из-за
    того, что манифест писали с одним синтаксисом, а клонировали с другим.
    """
    if not url:
        return ""
    u = url.strip()
    u = re.sub(r"^ssh://", "", u)
    u = re.sub(r"^git\+", "", u)
    u = re.sub(r"^https?://", "", u)
    # git@host:owner/repo -> host/owner/repo
    u = re.sub(r"^([^/@]+)@([^:/]+):", r"\2/", u)
    u = re.sub(r"^([^/@]+)@", "", u)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.lower()


# --------------------------------------------------------------------------
# local.json
# --------------------------------------------------------------------------

def host() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


# Роль машины: office | home. Хранится в local.json — она МАШИННАЯ, как и
# пути, и в git не едет. От роли зависит имя личной страховочной ветки.
#
# Зачем роль, а не хост. Хост (`RBB-0001`, `DESKTOP-FA9EUCC`) — это железо, он
# сменится при переустановке или новом ноутбуке, и вместе с ним осиротеет
# ветка. Роль — это место, где человек сидит, и она переживает смену машины.
KNOWN_ROLES = ("office", "home")

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")

# Префикс личных веток. Слеш намеренный: `git branch --list 'wip/*'` и
# `refs/heads/wip/*` в рефспеке отбирают их одним шаблоном, не задевая
# master, transcripts и calibration.
WIP_PREFIX = "wip/"


def role() -> str | None:
    """Роль ЭТОЙ машины или None, если ещё не задана.

    None — это не ошибка, а «машина не настроена»: так выглядит свежий клон
    до `machine.py --init`. Вызывающий код обязан в этом случае вести себя
    как до появления ролей, а не падать и не угадывать.
    """
    r = (load_local().get("role") or "").strip().lower()
    return r if r and _ROLE_RE.match(r) else None


def wip_branch(r: str | None = None) -> str | None:
    """Имя личной страховочной ветки этой (или указанной) роли."""
    r = (r or role() or "").strip().lower()
    if not r or not _ROLE_RE.match(r):
        return None
    return f"{WIP_PREFIX}{r}"


def role_of_wip(branch: str) -> str:
    """`wip/office` (или `origin/wip/office`) -> `office`."""
    b = branch.strip()
    for pref in (f"origin/{WIP_PREFIX}", f"refs/remotes/origin/{WIP_PREFIX}",
                 f"refs/heads/{WIP_PREFIX}", WIP_PREFIX):
        if b.startswith(pref):
            return b[len(pref):]
    return b


def load_local() -> dict:
    if not LOCAL_JSON.exists():
        return {"host": host(), "roots": [], "depth": 3, "cache": {}}
    try:
        with io.open(LOCAL_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"host": host(), "roots": [], "depth": 3, "cache": {}}
    data.setdefault("host", host())
    data.setdefault("roots", [])
    data.setdefault("depth", 3)
    data.setdefault("cache", {})
    return data


def save_local(data: dict) -> None:
    tmp = LOCAL_JSON.with_suffix(".json.tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(LOCAL_JSON)


# --------------------------------------------------------------------------
# манифесты projects.d/
# --------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def parse_frontmatter(text: str) -> dict:
    """Плоский YAML-frontmatter: key: value, без вложенности и списков.

    Полноценный YAML-парсер тут не нужен и не завозится намеренно: манифесты
    и планы пишутся руками по одному шаблону, а лишняя зависимость на двух
    машинах — лишний способ сломаться.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v in ("null", "~", ""):
            out[k] = None
        elif v in ("true", "false"):
            out[k] = v == "true"
        else:
            out[k] = v.strip("'\"")
    return out


def load_manifests() -> dict[str, dict]:
    """{name: {**frontmatter, '_path': Path, '_body': str}}"""
    out: dict[str, dict] = {}
    if not PROJECTS_D.is_dir():
        return out
    for p in sorted(PROJECTS_D.glob("*.md")):
        try:
            text = io.open(p, encoding="utf-8").read()
        except OSError:
            continue
        fm = parse_frontmatter(text)
        name = fm.get("name") or p.stem
        fm["_path"] = p
        fm["_body"] = _FM_RE.sub("", text, count=1)
        out[name] = fm
    return out


# --------------------------------------------------------------------------
# слаг папки транскриптов
# --------------------------------------------------------------------------

def path_to_slug(path: str | Path) -> str:
    """Путь -> имя папки в ~/.claude/projects/.

    Правило выведено из трёх настоящих папок, а не угадано:

        C:/Projects/assemblage-point        -> C--Projects-assemblage-point
        C:/Users/User/.claude               -> C--Users-User-.claude
        D:/Проекты/Laretto/assemblage-point -> D----------Laretto-assemblage-point

    Третий случай и объясняет правило: КАЖДЫЙ символ, кроме букв, цифр и
    точки, заменяется на один дефис. Отсюда и `C:/` -> `C--` (двоеточие и
    слеш — по дефису), и сохранившаяся точка в `.claude`, и десять дефисов
    вместо `:/Проекты/` (двоеточие, слеш, семь букв, слеш).

    Догадаться до этого было нельзя: пока не появилась папка кириллического
    пути, правило выглядело как «разделители в дефисы». Поэтому вызывающий
    код всё равно обязан идти через find_slug_dir() — существующая папка
    всегда важнее вычисленной.
    """
    s = str(path).replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9.]", "-", s)


def all_slug_dirs(path: str | Path) -> list[Path]:
    """ВСЕ папки projects/, относящиеся к этому проекту, а не только своя.

    Один и тот же проект на двух машинах даёт две папки:
    `C--Projects-assemblage-point` (офис) и
    `D----------Laretto-assemblage-point` (дом). Память, записанная в офисе,
    лежит в офисной папке и на домашней машине по «своему» слагу не находится
    — то есть синк, смотрящий только на локальный слаг, её бы не увидел и
    молча потерял. Отбор по имени каталога проекта («хвосту» пути) собирает
    оба варианта.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    leaf = Path(str(path).replace("\\", "/")).name.lower()
    if not leaf:
        return []
    # Claude Code кодирует путь в имя папки, заменяя разделители И ТОЧКИ на
    # дефис: `C:/Users/User/.claude` даёт `C--Users-User--claude`. Сравнение с
    # сырым хвостом `.claude` такую папку не находит — находит только пустую
    # `C--Users-User-.claude`, и сессии самого ~/.claude молча не синхронизи-
    # руются между машинами. Поэтому хвост сравнивается в обоих написаниях.
    leaves = {leaf, leaf.replace(".", "-")}
    out = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and any(d.name.lower().endswith(x) for x in leaves):
            out.append(d)
    return out


def find_slug_dir(path: str | Path) -> Path:
    """Папка транскриптов для проекта по данному пути.

    Сначала — точное совпадение с вычисленным слагом. Затем — поиск среди
    существующих папок по «хвосту» пути: имя проекта и его родитель. Это
    страховка от того, что правило слага изменится или не совпадёт на
    кириллице; без неё синк молча создал бы вторую папку и --resume не
    увидел бы ничего.
    """
    guess = PROJECTS_DIR / path_to_slug(path)
    if guess.is_dir():
        return guess
    if PROJECTS_DIR.is_dir():
        p = Path(str(path).replace("\\", "/"))
        tail = f"{p.parent.name}-{p.name}".lower()
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.lower().endswith(tail):
                return d
    return guess

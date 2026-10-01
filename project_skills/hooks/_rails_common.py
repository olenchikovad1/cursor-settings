"""Общее для хуков библиотеки под Cursor: запрос, настройка проекта, состояние беседы.

Вынесено в один модуль намеренно. Разбор командной строки, определённый в двух
хуках независимо, совпадает ровно до первой правки одного из них, после чего
расходится молча — а расхождение здесь означает, что один хук ловит то, что
второй пропускает.

Отличия от заготовки под Claude Code, из которой это выросло:

- запрос приходит в формате Cursor: у `beforeShellExecution` команда лежит в
  `command`, у `postToolUse` — в `tool_input.command`, инструмент оболочки
  называется `Shell`, а не `Bash`;
- настройка ищется в `.cursor/rails.json`, а за ним в `.claude/rails.json`:
  проект, в котором работают обоими инструментами, держит одну настройку;
- у Cursor нет одного транскрипта, который удобно разбирать, поэтому то, что
  нужно помнить между событиями одной беседы, хранится в файле состояния во
  временном каталоге, по `conversation_id`.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Разделители, за которыми начинается следующая команда. Нужны потому, что
# `git add -A && git push origin main` приходит хуку одной строкой.
SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}

# Инструменты, которые меняют файл. Cursor называет их по-разному в разных
# версиях, поэтому список широкий, а окончательное решение — по наличию пути
# в аргументах.
EDIT_TOOLS = {"Write", "StrReplace", "Edit", "MultiEdit", "EditNotebook",
              "Delete", "ApplyPatch"}
READ_TOOLS = {"Read", "ReadFile"}

# Подписи, которые Cursor сам дописывает к коммитам агента. Модель их в
# команде не пишет — их добавляет клиент, поэтому видно их только в уже
# записанном коммите.
CURSOR_ATTRIBUTION = ("cursoragent@cursor.com", "Made with Cursor",
                      "Made-with: Cursor")
DEFAULT_TRAILERS = ["Co-Authored-By"]
DEFAULT_SHARED = ["main", "master", "develop"]


def read_request(timeout: float = 5.0) -> dict:
    """Запрос со стандартного ввода. Пустой или неразбираемый — пустой словарь.

    Неразбираемый вход — не повод падать: хук, роняющий команду, мешает
    работать сильнее, чем пропущенное нарушение.

    Ждать ввод бесконечно нельзя: хук запускают и руками, и тогда ввода нет.
    """
    if sys.stdin is None or sys.stdin.closed:
        return {}
    try:
        if sys.stdin.isatty():
            return {}
    except (ValueError, OSError):
        return {}

    result: list[str] = []

    def _read() -> None:
        try:
            data = sys.stdin.buffer.read()
            result.append(data.decode("utf-8", errors="replace"))
        except (ValueError, OSError, AttributeError):
            result.append("")

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout)
    raw = result[0] if result else ""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _tool_input(request: dict) -> dict:
    value = request.get("tool_input")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def shell_command(request: dict) -> str:
    """Команда оболочки из запроса любого события. Не оболочка — пусто."""
    tool = request.get("tool_name")
    if tool in ("Shell", "Bash"):
        return str(_tool_input(request).get("command") or "")
    if tool is None and isinstance(request.get("command"), str):
        return request["command"]
    return ""


def native_path(value: str) -> Path:
    """Путь из запроса в виде, который открывает Python на этой машине.

    Cursor присылает корни рабочего пространства как `/c:/dev/...`, оболочка
    MSYS пишет `/c/dev/...`. Ни то, ни другое `Path` на Windows не открывает:
    `is_dir()` отвечает «нет», и хук смотрит не туда.
    """
    text = str(value)
    m = re.match(r"^/([A-Za-z]):?/(.*)$", text)
    if m and os.name == "nt":
        return Path(f"{m.group(1).upper()}:/{m.group(2)}")
    return Path(text)


def project_root(request: dict) -> Path:
    env = os.environ.get("CURSOR_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return native_path(env)
    roots = request.get("workspace_roots") or []
    if roots:
        return native_path(roots[0])
    return Path.cwd()


def command_dir(request: dict) -> Path:
    """Каталог, в котором выполнялась команда."""
    for value in (request.get("cwd"), _tool_input(request).get("working_directory")):
        if value:
            path = native_path(value)
            if path.is_dir():
                return path
    return project_root(request)


def _rails_files(base: Path) -> list[Path]:
    return [base / ".cursor" / "rails.json", base / ".claude" / "rails.json"]


def setting(request: dict, key: str, default):
    """Значение из `rails.json` проекта.

    Условия срабатывания не зашиваются в хук: общая ветка где-то называется
    `master`, где-то `prod`, а заготовка одна на все проекты. Настройка
    **заменяет** умолчание целиком, а не дополняет: иначе от списка нельзя
    отказаться.
    """
    start = command_dir(request)
    candidates = [project_root(request), start, *start.parents]
    seen: set[str] = set()
    for base in candidates[:14]:
        if str(base) in seen:
            continue
        seen.add(str(base))
        for path in _rails_files(base):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get(key)
            except (ValueError, OSError):
                continue
            if value is not None:
                return value
    return default


def commands(command: str) -> list[list[str]]:
    """Разбить строку на отдельные команды и разобрать каждую на слова.

    Разбор, а не поиск подстроки: `echo 'git push origin main'` не является
    пушем, и останавливать его нельзя. Хук, срабатывающий на упоминание,
    выключают целиком — вместе с той частью, что стоит на опасном.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []  # незакрытая кавычка: разобрать нельзя, значит молчим

    out, current = [], []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def git_subcommand(words: list[str], name: str) -> list[str] | None:
    """Слова после `git <name>`, если команда именно такая.

    Учитывается `VAR=x git ...` и `git -C <путь> ...`: и то и другое —
    обычный способ вызова, и пропускать его нельзя.
    """
    i = 0
    while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
        i += 1
    if i >= len(words) or Path(words[i]).name.lower() not in ("git", "git.exe"):
        return None
    i += 1
    while i < len(words) and words[i].startswith("-"):
        if words[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        else:
            i += 1
    if i >= len(words) or words[i] != name:
        return None
    return words[i + 1:]


def is_git(command: str, name: str) -> bool:
    return any(git_subcommand(words, name) is not None for words in commands(command))


def git(*args: str, cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def current_branch(cwd: Path) -> str | None:
    """`branch --show-current`, а не `rev-parse --abbrev-ref HEAD`: второй не
    отвечает на ветке без коммитов, а свежая ветка задачи — обычное состояние."""
    try:
        result = git("branch", "--show-current", cwd=cwd, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def toplevel(cwd: Path) -> Path | None:
    try:
        result = git("rev-parse", "--show-toplevel", cwd=cwd, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return native_path(result.stdout.strip()).resolve()


def shell_succeeded(request: dict) -> bool:
    """Удалась ли команда оболочки, по ответу инструмента.

    Отсутствие признака считаем успехом — иначе хук молчит там, где должен
    работать.
    """
    raw = request.get("tool_output")
    answer = raw
    if isinstance(raw, str):
        try:
            answer = json.loads(raw)
        except ValueError:
            return True
    if isinstance(answer, dict):
        for key in ("exitCode", "exit_code", "returncode"):
            if key in answer and answer[key] is not None:
                try:
                    return int(answer[key]) == 0
                except (TypeError, ValueError):
                    return True
        if answer.get("is_error") or answer.get("isError"):
            return False
    return True


CD_WORDS = {"cd", "-C", "Set-Location", "sl", "pushd", "Push-Location", "chdir"}


def touched_repositories(request: dict) -> list[Path]:
    """Все репозитории, которых команда коснулась.

    Команда часто начинается с перехода в соседний репозиторий
    (`cd ../portal && git commit ...`), а одна команда умеет закоммитить в
    двух репозиториях подряд. Поэтому собираются все переходы вместе с
    каталогом команды, и каждый сводится к корню своего репозитория.
    """
    start = command_dir(request)
    seen: list[Path] = []
    walking = start
    for words in commands(shell_command(request)):
        for index, word in enumerate(words):
            if word not in CD_WORDS or index + 1 >= len(words):
                continue
            target = words[index + 1]
            if target.startswith("-"):
                continue
            path = native_path(target)
            if not path.is_absolute():
                path = walking / target
            if path.is_dir():
                walking = path.resolve()
                seen.append(walking)

    roots: list[Path] = []
    for candidate in [start, *seen]:
        root = toplevel(candidate)
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def forbidden_in_last_commit(request: dict, repo: Path) -> str | None:
    """Служебная подпись в последнем коммите репозитория, если она есть."""
    try:
        result = git("log", "-1", "--format=%B", cwd=repo, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    message = result.stdout
    for name in setting(request, "forbidden_trailers", DEFAULT_TRAILERS):
        if re.search(rf"^[ \t]*{re.escape(name)}[ \t]*:", message,
                     re.IGNORECASE | re.MULTILINE):
            return name
    if not setting(request, "allow_cursor_attribution", False):
        for mark in CURSOR_ATTRIBUTION:
            if mark.lower() in message.lower():
                return mark
    return None


def edited_path(request: dict) -> Path | None:
    """Путь файла, который правил инструмент. Нет пути — не правка."""
    if request.get("tool_name") in READ_TOOLS:
        return None
    payload = _tool_input(request)
    for key in ("file_path", "path", "target_file", "filePath", "target_notebook"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            path = native_path(value)
            if not path.is_absolute():
                path = project_root(request) / path
            return path
    if request.get("file_path"):
        return native_path(request["file_path"])
    return None


def read_path(request: dict) -> Path | None:
    if request.get("tool_name") not in READ_TOOLS:
        return None
    payload = _tool_input(request)
    for key in ("path", "file_path", "target_file"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return native_path(value)
    return None


def relative(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def dirty_files(root: Path) -> dict[str, float]:
    """Изменённые относительно HEAD и неотслеживаемые файлы с временем правки."""
    names: set[str] = set()
    for args in (["diff", "--name-only", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        try:
            result = git(*args, cwd=root, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        for name in result.stdout.splitlines():
            if name.strip():
                names.add(name.strip().replace("\\", "/"))
    out: dict[str, float] = {}
    for name in names:
        try:
            out[name] = (root / name).stat().st_mtime
        except OSError:
            out[name] = 0.0
    return out


def changed_since_start(request: dict, state: dict) -> list[str]:
    """Файлы, изменённые с начала беседы, включая правки скриптом в оболочке.

    Содержательная проверка смотрит на то, что изменилось, а не на аргументы
    инструмента: правка через оболочку (`sed -i`, `python - <<EOF`) иначе до
    неё не доходит. Грязное дерево до начала беседы не считается — это не
    правка этой беседы.
    """
    root = project_root(request)
    now = dirty_files(root)
    base = state.setdefault("baseline", None)
    if base is None:
        state["baseline"] = now
        return []
    return sorted(name for name, mtime in now.items()
                  if name not in base or base[name] != mtime)


# --- состояние беседы ---------------------------------------------------

STATE_DIR = Path(tempfile.gettempdir()) / "cursor-rails"


def _state_file(request: dict) -> Path:
    conv = str(request.get("conversation_id") or request.get("session_id") or "default")
    return STATE_DIR / (re.sub(r"[^\w.-]", "_", conv) + ".json")


def load_state(request: dict) -> dict:
    try:
        return json.loads(_state_file(request).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(request: dict, state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_file(request)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def prune_states(days: int = 7) -> None:
    if not STATE_DIR.is_dir():
        return
    limit = time.time() - days * 86400
    for path in STATE_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            pass


def once(state: dict, key: str, items: list[str]) -> list[str]:
    """Оставить только то, о чём в этой беседе ещё не говорили.

    Повтор одного и того же предупреждения на каждую команду перестают читать
    за час, и тогда правило не действует вовсе.
    """
    said = set(state.setdefault(key, []))
    fresh = [item for item in items if item not in said]
    state[key] = sorted(said | set(fresh))
    return fresh

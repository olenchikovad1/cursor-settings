"""Хранилище журнала калибровки, устойчивое к отложенному пушу.

Раньше всё лежало в одном `task-estimates.json`: словарь с `matrix`,
`references` и тремя append-only списками, куда дописывается **каждый ход**.
На одной машине это работало, на двух — нет.

Причина не в параллельной работе (её не бывает, пользователь работает
единовременно только за одной машиной), а в **отложенном пуше**: в офисе
пропадает интернет, коммиты копятся локально, вечером работа продолжается
дома — и дом тоже коммитит. Ветки разошлись, а один и тот же JSON-массив
изменён с обеих сторон. Git такое слить не может: массив в JSON — это одна
логическая структура, и любое добавление элемента с двух сторон даёт
конфликт.

Раскладка теперь такая:

    matrix.json          matrix + references — меняются редко, конфликт тут
                         был бы осмысленным и его надо решать руками
    records/*.jsonl      history / plans / workflows, по записи на строку
    .pending.json        pending* — транзиентное, в .gitignore

Списки — построчный JSONL плюс `merge=union` в `.gitattributes`. Это
встроенный драйвер git: при расхождении он берёт строки обеих сторон вместо
конфликта. Для append-only журнала это в точности верное поведение — записи с
двух машин разные. На чтении всё равно делается дедупликация: union может
продублировать строку, если одна и та же запись сливалась дважды.

`pending` вынесен из синка сознательно. Он живёт от вызова
`calibrate.py estimate` до Stop-хука того же хода, то есть минуты. В общем
файле он давал бы конфликты на каждом переезде между машинами, ничего не
принося: незакрытый ход одной машины другой машине не нужен.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

DIR = Path.home() / ".cursor" / "time-analysis"
MATRIX_PATH = DIR / "matrix.json"
RECORDS_DIR = DIR / "records"
PENDING_PATH = DIR / ".pending.json"
LEGACY_PATH = DIR / "task-estimates.json"

LIST_KEYS = ("history", "plans", "workflows", "stories")
PENDING_KEYS = ("pending", "pending_sp", "pending_label")


def set_dir(path) -> None:
    """Перенаправить хранилище в другой каталог.

    Нужно тестам: раньше они подменяли `calibrate.JOURNAL_PATH` на временный
    файл, и этого хватало, потому что состояние было одним файлом. Теперь
    файлов несколько, и подменять надо каталог целиком — иначе прогон тестов
    писал бы в настоящую историю замеров и портил шкалу.
    """
    global DIR, MATRIX_PATH, RECORDS_DIR, PENDING_PATH, LEGACY_PATH
    DIR = Path(path)
    MATRIX_PATH = DIR / "matrix.json"
    RECORDS_DIR = DIR / "records"
    PENDING_PATH = DIR / ".pending.json"
    LEGACY_PATH = DIR / "task-estimates.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _record_key(rec: dict) -> str:
    """Ключ дедупликации записи.

    Полей с гарантированной уникальностью в записях нет, поэтому ключ —
    канонический JSON самой записи. Две по-настоящему разные записи с
    полностью совпадающими полями (та же оценка, тот же факт, та же метка) для
    статистики неразличимы, так что склеить их не потеря.
    """
    try:
        return json.dumps(rec, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(rec)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # битая строка (например обрыв записи) — молча мимо
                if not isinstance(rec, dict):
                    continue
                k = _record_key(rec)
                if k in seen:
                    continue
                seen.add(k)
                out.append(rec)
    except OSError:
        return out
    return out


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _empty() -> dict:
    return {"matrix": {}, "references": [], "history": [], "workflows": [],
            "plans": []}


def read_state() -> dict:
    """Собрать состояние из разложенных файлов.

    Если новой раскладки ещё нет, а старый `task-estimates.json` есть —
    читается он. Так миграция не обязана быть атомарной: до её выполнения
    всё работает по-старому.
    """
    if not MATRIX_PATH.exists() and LEGACY_PATH.exists():
        data = _read_json(LEGACY_PATH, None)
        if isinstance(data, list):
            state = _empty()
            state["history"] = data
            return state
        if isinstance(data, dict):
            state = _empty()
            state.update(data)
            return state

    state = _empty()
    matrix_blob = _read_json(MATRIX_PATH, {})
    if isinstance(matrix_blob, dict):
        state["matrix"] = matrix_blob.get("matrix") or {}
        state["references"] = matrix_blob.get("references") or []
    for key in LIST_KEYS:
        state[key] = _read_jsonl(RECORDS_DIR / f"{key}.jsonl")
    pending = _read_json(PENDING_PATH, {})
    if isinstance(pending, dict):
        for k in PENDING_KEYS:
            if k in pending:
                state[k] = pending[k]
    return state


def write_state(state: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)

    MATRIX_PATH.write_text(
        json.dumps({"matrix": state.get("matrix") or {},
                    "references": state.get("references") or []},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for key in LIST_KEYS:
        _write_jsonl(RECORDS_DIR / f"{key}.jsonl", state.get(key) or [])

    pending = {k: state[k] for k in PENDING_KEYS if k in state}
    if pending:
        PENDING_PATH.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif PENDING_PATH.exists():
        try:
            PENDING_PATH.unlink()
        except OSError:
            pass


def migrate_from_legacy() -> dict:
    """Разложить task-estimates.json по новым файлам. Идемпотентна."""
    data = _read_json(LEGACY_PATH, None)
    if data is None:
        return {"migrated": False, "reason": "нет task-estimates.json"}
    state = _empty()
    if isinstance(data, list):
        state["history"] = data
    else:
        state.update(data)
    write_state(state)
    return {
        "migrated": True,
        "counts": {k: len(state.get(k) or []) for k in
                   ("history", "plans", "workflows", "references")},
    }

"""Журнал фактов о шагах и указатель эталонных шагов.

Раскладка та же, что у остального журнала калибровки: построчный JSONL с
`merge=union` в `.gitattributes`, живёт на ветке `calibration`, синхронизируется
`hooks/sync_calibration.py`.

Почему JSONL, а не словарь `{номер эталона: [времена]}`, как предлагалось.
Данные те же, но словарь — единая структура, и две машины, дописавшие в него
из офиса и из дома, дают конфликт. Ровно на этом уже спотыкался старый
`task-estimates.json`. Построчная запись сливается сама, а свернуть её в
`{ref: [времена]}` можно при чтении, это дешёвая операция.

Что пишется:

* шаг совпал с эталоном — только ссылка и факт;
* не совпал — плюс подробности, но лишь те, что реально влияют на время:
  сколько файлов правлено и создано, сколько команд, какого вида работа.
  Имена файлов не пишем: на время они не влияют, а объём журнала раздувают.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

DIR = Path.home() / ".cursor" / "time-analysis"
RECORDS = DIR / "records"
STEPS_PATH = RECORDS / "steps.jsonl"
REFS_DIR = Path.home() / ".cursor" / "references" / "steps"


def set_dir(path) -> None:
    """Перенаправить каталог записей — для тестов.

    Тот же приём, что в journal_store: прогон тестов не должен дописывать
    мусор в настоящую историю замеров.
    """
    global DIR, RECORDS, STEPS_PATH
    DIR = Path(path)
    RECORDS = DIR / "records"
    STEPS_PATH = RECORDS / "steps.jsonl"

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def load_step_refs() -> dict[str, dict]:
    """{вид деятельности: {id, median_sec, p25_sec, p75_sec, samples}}"""
    out: dict[str, dict] = {}
    if not REFS_DIR.is_dir():
        return out
    for p in sorted(REFS_DIR.glob("STEP-*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = FM_RE.match(text)
        if not m:
            continue
        fm: dict = {}
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            fm[k] = int(v) if v.isdigit() else v
        kind = fm.get("kind")
        if kind:
            out[kind] = fm
    return out


def append(records: list[dict]) -> int:
    """Дописать факты. Возвращает сколько записано."""
    if not records:
        return 0
    RECORDS.mkdir(parents=True, exist_ok=True)
    with io.open(STEPS_PATH, "a", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def read_all() -> list[dict]:
    if not STEPS_PATH.exists():
        return []
    out = []
    for line in io.open(STEPS_PATH, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def by_ref() -> dict[str, list[float]]:
    """Свернуть журнал в {эталон: [времена]} — то самое, при чтении."""
    out: dict[str, list[float]] = {}
    for r in read_all():
        key = r.get("ref") or f"?{r.get('kind', 'прочее')}"
        out.setdefault(key, []).append(r.get("sec", 0))
    return out


def to_record(step: dict, refs: dict[str, dict]) -> dict:
    """Запись о шаге: со ссылкой на эталон или с подробностями."""
    kind = step.get("kind", "прочее")
    ref = refs.get(kind, {}).get("id")
    rec: dict = {
        "sec": step.get("sec"),
        "kind": kind,
        "ref": ref,
        "ts": step.get("started"),
        "host": step.get("host"),
    }
    # Режим пишется, только если он известен. Пустое поле и отсутствие поля —
    # разные вещи: по второму видно, что замер снят до появления разметки,
    # по первому — что транскрипта под него не нашлось.
    for key in ("model", "effort", "session"):
        if step.get(key):
            rec[key] = step[key]
    if not ref:
        # Эталона нет — сохраняем то, что влияет на время. Фраза нужна, чтобы
        # потом понять, что это была за работа, и завести эталон.
        rec["phrase"] = step.get("phrase")
        rec["scale"] = {
            "edited": step.get("files_edited", 0),
            "created": step.get("files_created", 0),
            "cmds": step.get("commands", 0),
            "tools": step.get("tools", {}),
        }
    else:
        scale = {k: v for k, v in (
            ("edited", step.get("files_edited", 0)),
            ("created", step.get("files_created", 0)),
            ("cmds", step.get("commands", 0))) if v}
        if scale:
            rec["scale"] = scale
    return rec

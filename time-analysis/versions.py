"""Версии эталонов (план 086).

Версия — граница, которую объявляет владелец: «с этой даты оценки новые» и
причина одной строкой. Смена модели, уровня усилий по умолчанию или приёма
работы сдвигает время шагов и историй разом, а журнал до сих пор считался
одним куском, и тысячи старых замеров топили сотни новых.

Принадлежность замера версии выводится из его времени, а не пишется в сам
замер. Отсюда две вещи: объявление задним числом переносит уже снятые замеры
без правки журнала фактов, и ни один факт не удаляется — выпавшие из расчёта
версии просто не читаются.

Список версий живёт в master (`references/versions.json`), журнал — на ветке
calibration: объявление доезжает до второй машины обычным синком.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, tzinfo
from pathlib import Path

VERSIONS_PATH = Path.home() / ".cursor" / "references" / "versions.json"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load(path: Path = VERSIONS_PATH) -> list[dict]:
    """Версии по возрастанию номера. Файла нет — одна версия без начала."""
    entries = _read(path).get("versions") or []
    if not entries:
        return [{"n": 1, "start": None, "reason": "версии не объявлялись"}]
    return sorted(entries, key=lambda v: v["n"])


def version_of(ts: str | None, vs: list[dict]) -> int | None:
    """Номер версии, в которую попадает момент `ts`. Без времени — None:
    версию по догадке запись не получает."""
    moment = _parse(ts)
    if moment is None:
        return None
    found = vs[0]["n"]
    for v in vs:
        start = _parse(v.get("start"))
        if start is None or moment >= start:
            found = v["n"]
    return found


def record_version(record: dict, vs: list[dict]) -> int | None:
    """Версия записи журнала: у шага время в `ts`, у истории — в `started`."""
    return version_of(record.get("ts") or record.get("started"), vs)


def active(vs: list[dict]) -> tuple[int, int | None]:
    """Текущая и предыдущая версии — только они идут в расчёт."""
    current = vs[-1]["n"]
    previous = vs[-2]["n"] if len(vs) > 1 else None
    return current, previous


def declare(path: Path, start: str, reason: str,
            tz: tzinfo | None = None) -> dict:
    """Объявить новую версию с начала дня `start` (ГГГГ-ММ-ДД) по местному
    времени. Причину пишет владелец своими словами; её содержание не
    проверяется, но пустой она быть не может — иначе через месяц не понять,
    зачем граница стоит."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("у версии должна быть причина")
    zone = tz or datetime.now().astimezone().tzinfo
    day = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=zone)
    data = _read(path)
    vs = load(path) if data.get("versions") else [
        {"n": 1, "start": None, "reason": "всё до первой объявленной версии"}]
    last_start = _parse(vs[-1].get("start"))
    if last_start is not None and day <= last_start:
        raise ValueError(f"граница {start} не позже начала текущей версии "
                         f"{vs[-1]['start']}")
    new = {"n": vs[-1]["n"] + 1, "start": day.isoformat(), "reason": reason,
           "declared": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    data["versions"] = vs + [new]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return new


def summary(steps: list[dict], stories: list[dict], vs: list[dict]) -> dict:
    """Сколько замеров шагов и закрытых историй в каждой версии и какие идут
    в расчёт. Сумма по версиям плюс записи без времени равна журналу."""
    current, previous = active(vs)
    rows = {v["n"]: {**v, "steps": 0, "stories": 0,
                     "in_calculation": v["n"] in (current, previous)}
            for v in vs}
    no_time_steps = 0
    for r in steps:
        n = record_version(r, vs)
        if n is None:
            no_time_steps += 1
        else:
            rows[n]["steps"] += 1
    no_time_stories = 0
    for s in stories:
        if not s.get("actual_seconds"):
            continue
        n = record_version(s, vs)
        if n is None:
            no_time_stories += 1
        else:
            rows[n]["stories"] += 1
    return {"current": current, "previous": previous,
            "versions": [rows[v["n"]] for v in vs],
            "steps_without_time": no_time_steps,
            "stories_without_time": no_time_stories}


# --- эталоны шагов из двух версий (US-0619) ----------------------------------
#
# Все числа ниже предварительные: сняты с мини-теста 26.09.2026 (792 замера
# новой версии против 8746 старой) и уточняются по накопленному.
#
# Вес новой версии — n/(n+K): при пяти замерах половина, при пятнадцати три
# четверти, при сорока пяти девять десятых. Плавно, без порога «ещё нет / уже
# есть», на котором оценка прыгала бы.
BLEND_K = 5
# Вид участвует в коэффициенте версии, если в каждой версии у него не меньше
# стольких замеров: медиана по двум случаям — не медиана.
MIN_KIND_SAMPLES = 5
# Меньше стольких видов с данными в обеих версиях — отказ от коэффициента, а не
# правдоподобное число.
MIN_KINDS_FOR_COEFFICIENT = 5
# Вид, чьё отношение «новое к старому» вне коридора, в коэффициент не входит:
# весь сьют подорожал втрое из-за смены проектов, а не модели, и размазывать
# этот сдвиг на соседей нельзя.
RATIO_CORRIDOR = (0.5, 2.0)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _dedup(records: list[dict]) -> list[dict]:
    """Журнал сливается построчным union, и дубли в нём бывают. Для медианы
    дубль — лишний голос, поэтому здесь он снимается тем же ключом, что в
    сводке calibrate.py modes."""
    seen: set = set()
    out = []
    for r in records:
        key = (r.get("ts"), r.get("sec"), r.get("host"), r.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def step_references(steps: list[dict], vs: list[dict]) -> dict:
    """Эталон каждого вида шага в минутах и откуда он взят.

    Источник — одно из четырёх: «новое» (старых замеров нет), «смешано» (доля
    новой версии в share_new), «старое с поправкой» (новых нет, старая медиана
    умножена на коэффициент версии), «старое как есть» (коэффициента нет).
    """
    current, previous = active(vs)
    buckets: dict[str, dict[str, list[float]]] = {}
    for r in _dedup(steps):
        kind, sec = r.get("kind"), r.get("sec")
        # Ход Cursor целиком — другая единица. В медиану шага он не входит:
        # один такой замер двигает вид в разы.
        if r.get("unit") == "turn":
            continue
        if not kind or not isinstance(sec, (int, float)) or sec <= 0:
            continue
        n = record_version(r, vs)
        side = "new" if n == current else "old" if n == previous else None
        if side:
            buckets.setdefault(kind, {"new": [], "old": []})[side].append(float(sec))

    ratios, excluded = [], []
    for kind, b in buckets.items():
        if len(b["new"]) >= MIN_KIND_SAMPLES and len(b["old"]) >= MIN_KIND_SAMPLES:
            ratio = _median(b["new"]) / _median(b["old"])
            if RATIO_CORRIDOR[0] <= ratio <= RATIO_CORRIDOR[1]:
                ratios.append(ratio)
            else:
                excluded.append(kind)
    if previous is None:
        coefficient, why = None, "предыдущей версии нет"
    elif len(ratios) < MIN_KINDS_FOR_COEFFICIENT:
        coefficient = None
        why = (f"видов с данными в обеих версиях {len(ratios)}, нужно от "
               f"{MIN_KINDS_FOR_COEFFICIENT} — мало, коэффициент не даётся")
    else:
        coefficient = round(_median(ratios), 3)
        why = f"медиана отношений «новое к старому» по {len(ratios)} видам"

    kinds = {}
    for kind, b in buckets.items():
        n_new, n_old = len(b["new"]), len(b["old"])
        old_sec = _median(b["old"]) * (coefficient or 1.0) if n_old else None
        if n_new and not n_old:
            sec, share, source = _median(b["new"]), 1.0, "новое"
        elif n_new:
            share = n_new / (n_new + BLEND_K)
            sec = share * _median(b["new"]) + (1 - share) * old_sec
            source = "смешано"
        else:
            sec, share = old_sec, 0.0
            source = "старое с поправкой" if coefficient else "старое как есть"
        kinds[kind] = {"minutes": round(sec / 60, 3), "share_new": round(share, 3),
                       "source": source, "n_new": n_new, "n_old": n_old}
    return {"current": current, "previous": previous,
            "coefficient": coefficient, "coefficient_why": why,
            "excluded_kinds": sorted(excluded), "kinds": kinds}


def read_journal(path: Path) -> list[dict]:
    """Журнал шагов построчно; битые строки пропускаются."""
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --- полки историй из двух версий (US-0620) ----------------------------------
#
# Старая сторона полки — фонд references/sp-scale.json: он снят целиком в
# предыдущих версиях, и истории прошлых версий в нём уже учтены. Новая сторона —
# закрытые истории текущей версии, размер которых поставлен сравнением: размер
# на глаз — метка, и шкала на таких догоняет собственную ошибку (sp_drift).
#
# SP историй между версиями не меняются: размеры относительные, двигается
# только цена одного SP в минутах.
MIN_SHELF_STORIES = 3          # полка участвует в коэффициенте версии
MIN_SHELVES_FOR_COEFFICIENT = 3


def shelf_references(fund: dict[float, tuple[float, float]], stories: list[dict],
                     vs: list[dict], reject=None) -> dict:
    """Минуты и запас каждой полки SP и откуда они взяты.

    `fund` — {sp: (центр полки, полуширина)} в минутах, как его отдаёт
    calibrate.story_shelves. Источники те же, что у шагов.

    `reject(story) -> причина | None` — тот же отбор, что у фонда
    (`calibrate.shelf_rejection`): простой, оставленная на ночь сессия и
    заблокированная история полку не двигают. Передаётся снаружи, потому что
    правило живёт в calibrate, а calibrate импортирует этот модуль.
    """
    current, _ = active(vs)
    new: dict[float, list[float]] = {}
    for s in stories:
        if (s.get("sized_by") != "comparison" or not s.get("actual_seconds")
                or s.get("sp") is None or record_version(s, vs) != current
                or len(vs) < 2):
            continue
        if reject is not None and reject(s):
            continue
        sp = float(s["sp"])
        if sp in fund:
            new.setdefault(sp, []).append(float(s["actual_seconds"]) / 60)

    ratios = [_median(v) / fund[sp][0] for sp, v in new.items()
              if len(v) >= MIN_SHELF_STORIES and fund[sp][0]]
    if len(ratios) < MIN_SHELVES_FOR_COEFFICIENT:
        coefficient = None
        why = (f"полок с {MIN_SHELF_STORIES}+ историями новой версии "
               f"{len(ratios)}, нужно от {MIN_SHELVES_FOR_COEFFICIENT} — мало, "
               "коэффициент не даётся")
    else:
        coefficient = round(_median(ratios), 3)
        why = f"медиана отношений «факт новой версии к полке фонда» по {len(ratios)} полкам"

    shelves = {}
    for sp, (centre, half) in fund.items():
        facts = new.get(sp, [])
        old = centre * (coefficient or 1.0)
        if facts:
            share = len(facts) / (len(facts) + BLEND_K)
            minutes = share * _median(facts) + (1 - share) * old
            source = "смешано"
        else:
            share, minutes = 0.0, old
            source = "старое с поправкой" if coefficient else "старое как есть"
        scale = minutes / centre if centre else 1.0
        shelves[sp] = {"minutes": round(minutes, 3), "buffer": round(half * scale, 3),
                       "share_new": round(share, 3), "n_new": len(facts),
                       "source": source}
    _level(shelves)
    return {"current": current, "coefficient": coefficient,
            "coefficient_why": why, "shelves": shelves}


def _level(shelves: dict[float, dict]) -> None:
    """Полки не убывают с ростом размера: соседние нарушители сливаются в
    среднее с весом полки (pool adjacent violators). Без этого одна длинная
    история на полке 0.5 делала её дороже 1 SP, и больший размер обещал
    меньше минут."""
    order = sorted(shelves)
    blocks = []                       # [сумма минут×вес, вес, [sp]]
    for sp in order:
        w = shelves[sp]["n_new"] + BLEND_K
        blocks.append([shelves[sp]["minutes"] * w, w, [sp]])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, w2, sps = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += w2
            blocks[-1][2] += sps
    for total, w, sps in blocks:
        for sp in sps:
            row = shelves[sp]
            row["levelled"] = len(sps) > 1
            if row["levelled"]:
                scale = (total / w) / row["minutes"] if row["minutes"] else 1.0
                row["minutes"] = round(total / w, 3)
                row["buffer"] = round(row["buffer"] * scale, 3)


# --- предложение завести версию (US-0621) ------------------------------------
#
# Систематическое расхождение — серия, а не среднее: 15 закрытых историй или
# 300 шагов подряд, медиана факт/оценка вне коридора, и почти все по одну
# сторону. Плохой день из трёх историй вопроса не даёт, иначе его начнут
# отклонять не читая. Числа предварительные.
STORY_SERIES = 15
STEP_SERIES = 300
DRIFT_CORRIDOR = (0.7, 1.4)
SAME_SIDE_SHARE = 0.75


def decline(path: Path, at: str | None = None) -> str:
    """«Не сейчас»: тот же вопрос не повторяется, пока после этого момента не
    наберётся ещё одна полная серия."""
    data = _read(path)
    vs = data.get("versions") or []
    if not vs:
        raise ValueError("версий нет — отклонять нечего")
    moment = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    vs[-1].setdefault("declined", []).append(moment)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return moment


def _cutoff(vs: list[dict]) -> datetime | None:
    """С какого момента копится серия: после начала и объявления текущей
    версии и после последнего «не сейчас». Записи раньше этого уже учтены
    версией или уже были предметом вопроса."""
    cur = vs[-1]
    moments = [_parse(cur.get("start")), _parse(cur.get("declared"))]
    moments += [_parse(x) for x in cur.get("declined") or []]
    moments = [m for m in moments if m is not None]
    return max(moments) if moments else None


def _series(pairs: list[tuple[datetime, float]], size: int) -> dict | None:
    """Последние `size` отношений факт/оценка — серия, если сдвиг
    систематический. None — серии нет."""
    if len(pairs) < size:
        return None
    pairs = sorted(pairs)[-size:]
    ratios = [r for _, r in pairs]
    med = _median(ratios)
    if DRIFT_CORRIDOR[0] <= med <= DRIFT_CORRIDOR[1]:
        return None
    faster = med < 1
    same = sum(1 for r in ratios if (r < 1) == faster) / len(ratios)
    if same < SAME_SIDE_SHARE:
        return None
    return {"n": len(ratios), "median_ratio": round(med, 3),
            "side": "быстрее" if faster else "медленнее",
            "same_side_share": round(same, 2), "since": pairs[0][0].isoformat()}


def drift(stories: list[dict], steps: list[dict], step_kinds: dict,
          shelves: dict[float, float], vs: list[dict]) -> dict:
    """Есть ли систематическое расхождение факта с оценкой, учитывающей смесь
    версий. `step_kinds` — kinds из step_references, `shelves` — {sp: минуты}."""
    cutoff = _cutoff(vs)

    def after(ts: str | None) -> datetime | None:
        m = _parse(ts)
        return m if m is not None and (cutoff is None or m > cutoff) else None

    story_pairs = []
    for s in stories:
        m = after(s.get("started"))
        sp = float(s["sp"]) if s.get("sp") is not None else None
        if m and s.get("actual_seconds") and sp in shelves and shelves[sp]:
            story_pairs.append((m, s["actual_seconds"] / 60 / shelves[sp]))
    step_pairs = []
    for r in _dedup(steps):
        m = after(r.get("ts"))
        ref = step_kinds.get(r.get("kind"))
        if m and ref and ref.get("minutes") and r.get("sec"):
            step_pairs.append((m, r["sec"] / 60 / ref["minutes"]))

    st = _series(story_pairs, STORY_SERIES)
    sp_ = _series(step_pairs, STEP_SERIES)
    since = min((x["since"] for x in (st, sp_) if x), default=None)
    return {"propose": bool(st or sp_), "stories": st, "steps": sp_,
            "since": since, "current": vs[-1]["n"],
            "counted_after": cutoff.isoformat() if cutoff else None}

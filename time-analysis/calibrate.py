"""
CLI-утилита калибровки оценок времени (skill time-estimate-calibration).
Не хук — вызывается вручную (Bash) в начале задачи и перед каждым шагом,
вместо ручного усреднения "в уме" по истории (ненадёжно на маленьких
выборках, см. note в нескольких старых записях task-estimates.json).

Формат файла — словарь {"matrix": {...}, "history": [...]}. Если на диске
всё ещё старый формат (голый список) — read_state() сама оборачивает его.

matrix НЕ хранит накопительное состояние отдельно от history: она всегда
пересчитывается с нуля из текущего history при каждом вызове estimate/
recompute и перезаписывается в файл как кэш для чтения человеком/собой —
это устраняет целый класс ошибок "забыла пересчитать" или "матрица разошлась
с историей". history обрезается до последних MAX_HISTORY задач перед каждой
записью — это и есть "удали самую старую, если уже 20" из требования.

Бакеты — по estimated_seconds (сырая, до калибровки) самой пары
estimated/actual, а не по calibrated: иначе бакет "плывёт" при каждом
изменении фактора и сравнивать через итерации становится нечем.

ГЛАВНОЕ (2026-08-27). Механизм "сырая оценка в минутах x поправочный
коэффициент" признан негодным: он требует от меня абсолютной оценки
длительности, которой у меня нет — я подставляю фольклор про живого
программиста ("сервис с тестами это полдня") и промахиваюсь на порядок. На
плане 010 это дало 22 ч против фактических 2,9 ч, а бакетный фактор,
притянутый сжатием к 0,7, ошибку почти не исправил.

Взамен — оценка сторипоинтами: я определяю только ОТНОСИТЕЛЬНЫЙ размер задачи
(шкала Фибоначчи, сравнение с эталонами), а перевод в минуты делает история
фактических замеров. Разделение труда: порядок задаю я, масштаб задают данные.
Сравнивать "эта задача больше вот той" я умею честно; называть минуты — нет.

Матрица факторов оставлена для совместимости со старыми записями без sp, но
основной путь — sp-table/estimate --sp.
"""

import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journal_store as _store  # noqa: E402

# Оставлено для совместимости: на путь ссылались хуки и старые записи. Само
# состояние с миграции 014 лежит не здесь, а в matrix.json + records/*.jsonl.
JOURNAL_PATH = _store.LEGACY_PATH
# Планы — источник размера и названия истории. Дублировать их в записи факта
# нельзя: две записи одной величины расходятся при первой правке плана.
PLANS_DIR = Path.home() / ".cursor" / "plans"


def extra_plan_dirs() -> list[Path]:
    """Каталоги планов вне ~/.cursor/plans — из машинного local.json
    (`extra_plans_dirs`, не в git): приватные планы, которые не должны
    уезжать синхронизацией, считаются планами на этой машине."""
    try:
        data = json.loads((Path.home() / ".cursor" / "local.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(p) for p in data.get("extra_plans_dirs", []) if Path(p).is_dir()]


def plan_dirs() -> list[Path]:
    # ~/.claude/plans сюда не входит: нумерация в Cursor начата заново с 001,
    # и номер «001» там — другой план. Поиск по обоим каталогам находил бы
    # чужой файл первым (".claude" по алфавиту раньше ".cursor").
    return [PLANS_DIR, *extra_plan_dirs()]
# Было 20. Шкале сторипоинтов нужна плотность: чтобы полка считалась по медиане,
# на каждое значение SP нужно минимум SP_MIN_SAMPLES замеров, а значений в шкале
# девять. При лимите 20 записей засев вымывался бы за пару сессий. Файл маленький,
# держать двести пар дешевле, чем каждый раз начинать шкалу заново.
MAX_HISTORY = 200
# Вес опорного значения при сжатии фактора (см. shrunk_factor). 3 ≈ "одному
# замеру верим на четверть, десяти — почти полностью".
PRIOR_WEIGHT = 3
MIN_FACTOR = 0.2
MAX_FACTOR = 3.0

# (min_seconds, max_seconds_exclusive_or_None). Шаги — мельче и чаще, бакеты
# уже; задачи — грубее, совпадают с уже использовавшимися XS/S/M/L/XL.
TASK_BUCKETS = [(0, 60), (60, 300), (300, 1200), (1200, 3600), (3600, None)]
STEP_BUCKETS = [(0, 30), (30, 60), (60, 180), (180, 600), (600, None)]
# Фоновые Workflow (несколько субагентов, TDD-цикл с ретраями) обычно идут
# существенно дольше шага/задачи чата — свои, более широкие бакеты.
WORKFLOW_BUCKETS = [(0, 300), (300, 900), (900, 1800), (1800, 3600), (3600, None)]

# Шкала сторипоинтов. Фибоначчи даёт монотонный рост с растущим шагом: чем
# крупнее задача, тем шире её "полка", что и отражает рост погрешности. 0.5 —
# вне ряда, для совсем мелких правок, где иначе всё слипается в единицу.
SP_SCALE = [0.5, 1, 2, 3, 5, 8, 13, 21, 34]

# Верх шкалы. Значение больше — это не размер одной задачи, а сумма очков плана:
# такую пару нельзя кормить полкам (полка описывает ОДНУ задачу данного размера),
# и она уходит в plans, см. cmd_record_plan.
SP_MAX = SP_SCALE[-1]


def snap_to_scale(sp: float) -> float:
    """Ближайшее значение шкалы — по логарифму, а не по разнице.

    Оценивая, я иногда называю размер вне шкалы (3.5, 6, 11, 17). Раньше такие
    записи попадали в историю как есть и молча выпадали из расчёта полок:
    sp_samples искала точное совпадение со SP_SCALE. Четыре замера из тридцати
    шести (11% выборки) не участвовали ни в одной полке — при том что данных и
    так мало, а верх шкалы вообще пустой.

    Логарифм, а не разница: шкала геометрическая, и 17 одинаково далеко от 13 и
    21 по расстоянию, но по смыслу ("во сколько раз больше") ближе к 21 —
    геометрическая середина отрезка 13..21 равна sqrt(13*21) ≈ 16.5.
    """
    value = float(sp)
    if value <= SP_SCALE[0]:
        return SP_SCALE[0]
    if value >= SP_MAX:
        return SP_MAX
    return min(SP_SCALE, key=lambda s: abs(math.log(s) - math.log(value)))

# Сколько замеров на одно значение SP нужно, чтобы верить его медиане. Меньше —
# берём соседние значения и интерполируем: одна случайная задача не должна
# задавать полку целиком.
SP_MIN_SAMPLES = 3

# Доля выборки, отсекаемая с КАЖДОГО края перед расчётом полки. Убирает аномалии
# с двух сторон: и ход, где я половину времени переписывала тесты, и ход, где
# задача оказалась однострочником. Считается как floor(n * доля), поэтому
# начинает работать с десяти замеров на полку — на меньшей выборке аномалию не
# отличить от нормы, и отсекать значило бы просто терять данные.
SP_TRIM_RATIO = 0.10

# Запас — не константа, а функция уверенности: (порог замеров, доля от медианы).
# Чем больше замеров в полке, тем меньше добавка сверх наблюдённого разброса.
# Первая строка (n < 3) — это и есть случай "считать разброс не на чем": там
# добавка максимальная, потому что мы про полку почти ничего не знаем.
#
# Почему не просто наблюдённый разброс: на трёх-четырёх замерах 80-й процентиль
# систематически недооценивает хвост — выборка ещё не видела плохих случаев. И
# почему не фиксированные 30%: это выдуманное число, а не измеренное; по мере
# накопления истории добавка должна сама сжиматься, иначе запас навсегда
# остаётся размытым.
SP_BUFFER_FLOORS = [(3, 0.30), (6, 0.20), (11, 0.12), (None, 0.08)]


def buffer_floor_ratio(n: int) -> float:
    for threshold, ratio in SP_BUFFER_FLOORS:
        if threshold is None or n < threshold:
            return ratio
    return SP_BUFFER_FLOORS[-1][1]

# Опорная таблица на случай пустой истории: снята с фактических зазоров между
# 18 коммитами плана 010 (2026-08-27). ВАЖНО: те замеры сделаны при плохой
# дисциплине проверок — тяжёлый repo-сьют гонялся почти после каждого шага по
# 6-7 минут. При нормальной дисциплине (быстрые тесты в цикле, тяжёлые один раз
# на блок) те же задачи стоят меньше, поэтому таблицу надо перемерить, а не
# считать истиной.
SP_SEED_MINUTES = {
    0.5: 0.5,
    1: 1.5,
    2: 3.5,
    3: 6,
    5: 10,
    8: 17,
    13: 30,
    21: 50,
    34: 90,
}


# Состав состояния (кто где лежит — см. journal_store):
#   matrix      — посчитанные полки, matrix.json
#   references  — постоянная линейка размеров: сто настоящих задач с известным
#                 SP и длительностью, снятые с зазоров между коммитами. Отдельно
#                 от history потому, что их нельзя обрезать по MAX_HISTORY
#                 (иначе шкала растворится за пару сессий) и сравнивать новую
#                 задачу нужно именно с ними, а не с последними двадцатью ходами,
#                 которые могли быть все одного размера. Лежит в matrix.json.
#   history     — пары «оценка/факт» по задачам, records/history.jsonl
#   workflows   — то же для фоновых пайплайнов, records/workflows.jsonl
#   plans       — пары по планам целиком. Отдельно от history: план — это не
#                 задача, его нельзя положить на полку, но именно на планах
#                 проверяется правило сложения полок. Не обрезаются по
#                 MAX_HISTORY: их единицы, а стоят они дорого.
#                 records/plans.jsonl
#   pending*    — транзиентный размер текущего хода, .pending.json (вне git)
read_state = _store.read_state
write_state = _store.write_state


def bucket_label(lo: int, hi) -> str:
    return f"{lo}-{hi}s" if hi is not None else f"{lo}s+"


def shrunk_factor(ratios: list[float], prior: float) -> float:
    """Средний ratio, притянутый к опорному значению; вес притяжения — PRIOR_WEIGHT.

    Простое среднее по бакету с n=1..2 — это не оценка, а один случайный замер:
    фактор скакал с 0.4 на 2.4 от одной новой задачи и тянул за собой все
    последующие оценки. Сжатие даёт мягкий переход: на одном замере фактор
    сдвигается от опорного примерно на четверть расстояния, на десяти — почти
    полностью ему доверяет.

    Ограничение [MIN_FACTOR, MAX_FACTOR] — против одиночных выбросов вида
    "заложила минуту, провозилась час": такой ratio=60 иначе сделал бы все
    следующие оценки в бакете абсурдными.
    """
    if not ratios:
        return round(prior, 3)
    total = sum(ratios) + PRIOR_WEIGHT * prior
    factor = total / (len(ratios) + PRIOR_WEIGHT)
    return round(min(max(factor, MIN_FACTOR), MAX_FACTOR), 3)


def compute_buckets(pairs: list[tuple[float, float]], buckets: list[tuple[int, object]]) -> list[dict]:
    """Факторы по бакетам. Пустой бакет наследует общий фактор шкалы, а не 1.0.

    Раньше бакет без данных отдавал ровно 1.0, то есть "оценка идеальна" — самое
    вредное значение по умолчанию: систематический перекос, уже видимый по всем
    остальным бакетам, в новом диапазоне игнорировался.
    """
    all_ratios = [a / e for e, a in pairs if e]
    global_factor = shrunk_factor(all_ratios, 1.0)
    out = []
    for lo, hi in buckets:
        ratios = [a / e for e, a in pairs if e and e >= lo and (hi is None or e < hi)]
        out.append({
            "range": bucket_label(lo, hi),
            "min": lo,
            "max": hi,
            "factor": shrunk_factor(ratios, global_factor),
            "n": len(ratios),
            # Сколько замеров стоит за фактором — видно, насколько ему верить
            "scale_n": len(all_ratios),
        })
    return out


def recompute(state: dict) -> dict:
    history = state["history"][-MAX_HISTORY:]
    workflows = state.get("workflows", [])[-MAX_HISTORY:]
    task_pairs = [
        (e["estimated_seconds"], e["actual_seconds"])
        for e in history
        if "actual_seconds" in e and e.get("estimated_seconds")
    ]
    step_pairs = [
        (s["estimated_seconds"], s["actual_seconds"])
        for e in history
        for s in e.get("steps", [])
        if "actual_seconds" in s and s.get("estimated_seconds")
    ]
    workflow_pairs = [
        (w["estimated_seconds"], w["actual_seconds"])
        for w in workflows
        if "actual_seconds" in w and w.get("estimated_seconds")
    ]
    state["history"] = history
    state["workflows"] = workflows
    state["matrix"] = {
        "task": compute_buckets(task_pairs, TASK_BUCKETS),
        "step": compute_buckets(step_pairs, STEP_BUCKETS),
        "workflow": compute_buckets(workflow_pairs, WORKFLOW_BUCKETS),
    }
    return state


def find_bucket(matrix_scale: list[dict], seconds: float) -> dict:
    for b in matrix_scale:
        if seconds >= b["min"] and (b["max"] is None or seconds < b["max"]):
            return b
    return matrix_scale[-1]


def cmd_recompute() -> None:
    state = recompute(read_state())
    write_state(state)
    print(json.dumps(state["matrix"], ensure_ascii=False, indent=2))


def cmd_estimate(scale: str, seconds: float) -> None:
    state = recompute(read_state())
    write_state(state)
    b = find_bucket(state["matrix"][scale], seconds)
    calibrated = round(seconds * b["factor"])
    print(json.dumps({
        "raw_seconds": seconds,
        "calibrated_seconds": calibrated,
        "bucket": b["range"],
        "factor": b["factor"],
        "n": b["n"],
    }, ensure_ascii=False))



def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def calibration_rows(state: dict) -> list[dict]:
    """Что кормит полки: эталоны плюс замеры текущих сессий.

    Эталоны дают плотность и стабильность (сто пар размер-длительность), история —
    свежесть: если темп изменится, новые замеры сдвинут медиану, не дожидаясь
    пересбора эталонов.
    """
    return list(state.get("references", [])) + list(state.get("history", []))


def enforce_monotone(rows: list[dict]) -> list[dict]:
    """Полка не может быть быстрее меньшей полки.

    Медианы на реальных данных местами проседают: задача на 8 SP иногда выходит
    быстрее задачи на 5 SP, потому что длительность у меня определяется числом
    проверок, а не объёмом кода, и шум на пятнадцати замерах это перебивает.
    Немонотонная таблица непригодна как линейка, поэтому полка подтягивается до
    предыдущей — вверх, никогда вниз.
    """
    floor = 0.0
    for row in rows:
        if row["minutes"] < floor:
            row["minutes"] = floor
            row["lifted"] = True
        floor = row["minutes"]
    return rows


# Разумные границы длительности одной записи. Нижняя — замер короче десяти
# секунд это не задача, а промах разметки; верхняя — четыре часа: в журнале
# лежит ход на 21 очко длиной 920 минут, то есть сессия, оставленная открытой на
# ночь, и такая запись задаёт полке и центр, и разброс.
SHELF_MIN_SECONDS = 10
SHELF_MAX_SECONDS = 4 * 3600


# Во сколько раз факт может превысить полку, оставаясь фактом. Выше — это уже
# не медленная работа, а простой: сессия, оставленная открытой, история,
# прождавшая решения владельца. Абсолютная граница (240 минут) такое пропускает,
# потому что смотрит на длительность, а не на соотношение.
#
# 24.09.2026 US-0443 простояла открытой с утра и закрылась с отношением 10.08 —
# выброс поехал в полки и утянул бы полку 2 SP вдесятеро.
#
# Число предварительное: взято решением, а не наблюдением. Настоящее придёт из
# распределения отношений, когда их накопится, — сейчас в журнале есть и 0.12, и
# 1.89, и разделить промах оценки от простоя по одному дню нельзя.
SHELF_MAX_RATIO = 5.0
SHELF_MAX_RATIO_PROVISIONAL = True


def shelf_rejection(record: dict) -> str | None:
    """Почему запись не двигает полку. None — двигает.

    Главная причина — размер, названный без сравнения с эталоном. Такой размер
    не предсказание, а метка, и полку он бы не уточнял, а портил: раздутые очки
    тянут минуты на очко вниз, по опущенной полке следующая оценка раздувается
    сильнее, и шкала начинает догонять собственную ошибку. На планах 030 и 034
    это дало факт/полка 0.19 и 0.27.

    Записи без пометки `sized_by` проходят: правило смотрит вперёд, а задним
    числом размеры не переставляются.
    """
    # Заблокированная история ничего не измерила: прошедшее время — ожидание
    # владельца или отказ среды, а не работа. В полки такому нельзя.
    if record.get("blocked"):
        kind = (record["blocked"] or {}).get("kind", "?")
        return f"история заблокирована ({kind}) — ожидание работой не считается"
    sized_by = record.get("sized_by")
    if sized_by is not None and sized_by != "comparison":
        return "размер назван без сравнения с эталоном"
    actual = record.get("actual_seconds")
    if actual and not (SHELF_MIN_SECONDS <= float(actual) <= SHELF_MAX_SECONDS):
        return "длительность вне правдоподобного диапазона"
    # Названный вручную факт не проверяется на соотношение: человек уже вычел
    # паузы, и не нам объявлять его невозможным.
    shelf = record.get("shelf_seconds")
    if (actual and shelf and record.get("source") != "manual"
            and float(actual) / float(shelf) > SHELF_MAX_RATIO):
        return (f"похоже на простой, а не на работу: факт вышел за полку "
                f"более чем в {SHELF_MAX_RATIO:.0f} раз")
    if record.get("sp") is not None and float(record["sp"]) > SP_MAX:
        return "размер вне шкалы — похоже на сумму очков плана"
    return None


def sp_samples(history: list[dict]) -> dict[float, list[float]]:
    """Фактические длительности, сгруппированные по значению sp.

    Записи без sp игнорируются: их 20 штук от прежнего механизма, длительности
    там настоящие, но размер задачи неизвестен — сопоставить не с чем.

    Значение sp приводится к шкале через snap_to_scale, иначе замер с размером
    3.5 или 11 не попадает ни в одну полку. Записи с sp > SP_MAX отбрасываются
    целиком: такое число — сумма очков плана, приклеенная к длительности одного
    хода (ровно так в историю попали sp=45/46/48), и полку она бы испортила.
    Многозадачные ходы (sp_points длиной >1) в полки тоже не идут: полка — про
    одну задачу, сумма очков на шкале смысла не имеет.
    """
    by_sp: dict[float, list[float]] = {}
    for record in history:
        sp = record.get("sp")
        actual = record.get("actual_seconds")
        if sp is None or not actual:
            continue
        if shelf_rejection(record):
            continue
        points = record.get("sp_points")
        if points is not None and len(points) != 1:
            continue
        by_sp.setdefault(snap_to_scale(sp), []).append(float(actual))
    return by_sp


def trim_sample(values: list[float]) -> tuple[list[float], int]:
    """Отсечь по SP_TRIM_RATIO с каждого края. Возвращает (выборка, сколько убрано).

    Гарантия: после отсечения остаётся не меньше SP_MIN_SAMPLES элементов —
    иначе усечение из инструмента против выбросов превращается в способ
    выбросить почти всё. Поэтому число отсекаемых с каждой стороны берётся как
    минимум из "доля от n" и "сколько можно снять, не опустившись ниже порога".
    """
    ordered = sorted(values)
    n = len(ordered)
    if n <= SP_MIN_SAMPLES:
        return ordered, 0
    per_side = min(int(n * SP_TRIM_RATIO), (n - SP_MIN_SAMPLES) // 2)
    if per_side <= 0:
        return ordered, 0
    return ordered[per_side : n - per_side], per_side * 2


def sp_table(history: list[dict]) -> list[dict]:
    """Таблица "сторипоинт -> минуты" с запасом, посчитанная из фактов.

    Перед расчётом выборка усекается по SP_TRIM_RATIO с каждого края — так из
    полки уходят аномалии с обеих сторон: и ход, где половина времени ушла на
    правку чужих тестов, и ход, где задача внезапно оказалась однострочником.

    Медиана, а не среднее: один затянувшийся прогон не должен утаскивать полку.
    Запас — максимум из двух величин: наблюдённого разброса (медиана до 80-го
    процентиля этой же полки) и порога по уверенности (доля от медианы, тем
    меньше, чем больше замеров). Первое отражает "насколько косячные оценки
    внутри этого размера", второе — "насколько мало мы про этот размер знаем".
    Брать только первое нельзя: на трёх замерах 80-й процентиль ещё не видел
    плохих случаев и запас выходит обманчиво узким.

    Полка без достаточного числа замеров не выдумывается, а интерполируется по
    логарифму между ближайшими наполненными: рост длительности по шкале
    Фибоначчи ближе к геометрическому, чем к линейному.
    """
    samples = sp_samples(history)
    known: dict[float, float] = {}
    spread: dict[float, float] = {}
    trimmed_off: dict[float, int] = {}
    for sp, values in samples.items():
        if len(values) < SP_MIN_SAMPLES:
            continue
        # сначала отсекаем края, и медиану с разбросом считаем уже внутри
        # оставшихся: иначе один затянувшийся ход задаёт и центр, и разброс
        core, dropped = trim_sample(values)
        trimmed_off[sp] = dropped
        med = _median(core)
        known[sp] = med
        p80 = core[min(len(core) - 1, int(round(0.8 * (len(core) - 1))))]
        spread[sp] = max(p80 - med, 0.0)

    rows = []
    for sp in SP_SCALE:
        seconds = known.get(sp)
        source = "measured"
        if seconds is None:
            seconds = _interpolate(sp, known)
            source = "interpolated" if known else "seed"
        n = len(samples.get(sp, []))
        # берём максимум из наблюдённого разброса и порога по уверенности:
        # наблюдённый может быть честно мал, но на малой выборке ему рано верить
        observed = spread.get(sp) or 0.0
        buffer_seconds = max(observed, seconds * buffer_floor_ratio(n))
        rows.append({
            "sp": sp,
            "minutes": round(seconds / 60, 1),
            "buffer_minutes": round(buffer_seconds / 60, 1),
            "n": n,
            # видно, сработало ли усечение: на малых выборках это ноль, и полку
            # надо читать как "по всем замерам", а не "по очищенным"
            "trimmed": trimmed_off.get(sp, 0),
            "source": source,
        })
    return enforce_monotone(rows)


def _interpolate(sp: float, known: dict[float, float]) -> float:
    """Длительность для полки без своих замеров.

    Нет данных вообще — опорная таблица. Есть данные только с одной стороны —
    масштабируем ближайшую известную полку пропорционально самой шкале, а не
    повторяем её значение: иначе 21 SP и 3 SP оказались бы одинаковыми.
    """
    if not known:
        return SP_SEED_MINUTES.get(sp, sp * 2) * 60

    lower = max((k for k in known if k < sp), default=None)
    upper = min((k for k in known if k > sp), default=None)
    if lower is not None and upper is not None:
        # линейно по логарифму: шкала Фибоначчи растёт геометрически
        span = (upper - lower) or 1
        weight = (sp - lower) / span
        return known[lower] ** (1 - weight) * known[upper] ** weight
    anchor = lower if lower is not None else upper
    return known[anchor] * (sp / anchor)


SP_SCALE_PATH = Path.home() / ".cursor" / "references" / "sp-scale.json"

# Как называется линейка, по которой посчитано. Названия идут в ответ команды
# дословно: человек должен видеть источник, не заглядывая в код.
RULER_SOURCES = {
    "fund": "фонд эталонных историй",
    "extrapolated": "достроено от верхнего эталона",
    "journal": "журнал ходов (фонд недоступен)",
}


def story_shelves() -> dict[float, tuple[float, float]]:
    """Полки историй с учётом версий эталонов (план 086, US-0620).

    Фонд — старая сторона полки, закрытые истории текущей версии — новая; вес
    новой растёт с их числом. Версий нет или считать не из чего — фонд как есть.
    """
    fund = fund_shelves()
    info = shelf_versions(fund)
    if not info:
        return fund
    return {sp: (row["minutes"], row["buffer"]) for sp, row in info["shelves"].items()}


def shelf_versions(fund: dict[float, tuple[float, float]] | None = None) -> dict | None:
    """Разбор полок по версиям: минуты, доля новой версии и источник."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import versions
        vs = versions.load()
        if len(vs) < 2:
            return None
        fund = fund if fund is not None else fund_shelves()
        # Отбор тот же, что у полок фонда: иначе одна история, простоявшая
        # открытой десять часов, задаёт полку новой версии.
        return versions.shelf_references(fund, read_state().get("stories", []), vs,
                                         reject=shelf_rejection)
    except Exception:
        return None


def fund_shelves() -> dict[float, tuple[float, float]]:
    """Полки из references/sp-scale.json — те, что сняты с эталонных ИСТОРИЙ.

    Старый журнал мерил размер ХОДА в чате: там 1 SP это 2.8 минуты, а история
    того же размера идёт около пяти. Разница почти вдвое, и брать полку из хода
    для истории нельзя. Поэтому измеренные полки историй имеют приоритет, а
    журнал остаётся запасным вариантом для уровней, которых в фонде ещё нет.
    """
    try:
        data = json.loads(SP_SCALE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[float, tuple[float, float]] = {}
    for shelf in data.get("shelves", []):
        minutes = shelf.get("minutes")
        if not minutes:
            continue
        low, high = float(minutes[0]), float(minutes[1])
        out[float(shelf["sp"])] = ((low + high) / 2, (high - low) / 2)
    return out


def find_sp_row(rows: list[dict], sp: float) -> dict:
    """Полка для размера. Внемасштабное значение приводится к шкале тем же
    snap_to_scale, что и при записи, — чтобы оценка и последующий замер легли на
    одну и ту же полку, а не на разные."""
    target = snap_to_scale(sp)
    shelves = story_shelves()
    if target in shelves:
        minutes, buffer_minutes = shelves[target]
        return {"sp": target, "minutes": minutes, "buffer_minutes": buffer_minutes,
                "n": 0, "source": "measured"}
    if shelves and target > max(shelves):
        # Выше фонда эталонов. Достраиваем от верхней измеренной полки, а НЕ
        # проваливаемся в старый журнал: он в другой единице (размер хода) и
        # даёт для 5 SP меньше, чем полка историй для 3 SP.
        top = max(shelves)
        minutes, buffer_minutes = shelves[top]
        factor = target / top
        return {"sp": target, "minutes": minutes * factor,
                "buffer_minutes": buffer_minutes * factor + minutes * factor * 0.3,
                "n": 0, "source": "extrapolated"}
    for row in rows:
        if abs(row["sp"] - target) < 1e-9:
            return row
    return min(rows, key=lambda r: abs(r["sp"] - target))


def shelf_seconds_for(rows: list[dict], points: list[float]) -> tuple[int, int]:
    """(секунды, запас) для набора задач: складываются ПОЛКИ, а не очки."""
    chosen = [find_sp_row(rows, sp) for sp in points]
    return (
        int(round(sum(r["minutes"] for r in chosen) * 60)),
        int(round(sum(r["buffer_minutes"] for r in chosen) * 60)),
    )


def cmd_sp_table() -> None:
    state = read_state()
    print(json.dumps(sp_table(calibration_rows(state)), ensure_ascii=False, indent=2))


def cmd_references(sp: float | None, limit: int) -> None:
    """Показать эталоны — с ними и сравнивается новая задача.

    Без аргумента печатает по несколько примеров на каждую полку: этого хватает,
    чтобы вспомнить масштаб. С `--sp` — все эталоны этого размера.
    """
    state = read_state()
    refs = state.get("references", [])
    if not refs:
        print(json.dumps({"error": "эталонов нет"}, ensure_ascii=False))
        return
    if sp is not None:
        chosen = [r for r in refs if float(r.get("sp", 0)) == snap_to_scale(sp)]
    else:
        chosen = []
        for value in SP_SCALE:
            same = [r for r in refs if float(r.get("sp", 0)) == value]
            chosen += same[: max(1, limit // max(len(SP_SCALE), 1))] if same else []
    print(json.dumps(
        [
            {
                "sp": r["sp"],
                "minutes": r.get("minutes"),
                "label": r.get("label"),
                "layers": r.get("layers"),
            }
            for r in chosen[:limit]
        ],
        ensure_ascii=False,
        indent=2,
    ))


def cmd_estimate_sp(points: list[float], label: str | None,
                    scope: str = "turn",
                    compared_to: list[str] | None = None) -> None:
    """Оценка по списку сторипоинтов: одно значение на задачу/шаг.

    Складываются ПОЛКИ, а не сторипоинты: десять задач по 2 SP и одна задача на
    20 SP — совершенно разные по стоимости вещи, потому что шкала растёт
    геометрически. Сложить сначала очки, а потом искать полку суммы — та же
    ошибка, из-за которой я и промахивалась на порядок, только с другой стороны.

    Отдельную скидку "второй шаг в той же области дешевле" не вводим: она уже
    сидит в замерах, сделанных на шагах внутри планов, и учтённая дважды швырнёт
    оценку в недооценку. Неизвестное закрывает запас.

    Эталоны сравнения идут в запись рядом с числом. У истории такая пометка
    есть с US-0260, у хода её не было вовсе — и размер хода назывался на глаз,
    потому что проверить его было нечем. На 22.09.2026 это дало медиану
    факт/полка 1.02 там, где сравнение настоящее, и 0.28 там, где его нет.
    """
    state = read_state()
    rows = sp_table(calibration_rows(state))
    chosen = [find_sp_row(rows, sp) for sp in points]
    minutes = round(sum(r["minutes"] for r in chosen), 1)
    buffer_minutes = round(sum(r["buffer_minutes"] for r in chosen), 1)
    snapped = [snap_to_scale(sp) for sp in points]
    interpolated = sorted({r["sp"] for r in chosen if r["source"] != "measured"})
    above_fund = sorted({r["sp"] for r in chosen if r["source"] == "extrapolated"})
    # Режим только записывается рядом с оценкой, числа он не двигает. Берётся из
    # самого свежего транскрипта, то есть из сессии, которая оценку и просит.
    effort, mode_source = current_effort()
    mode = {"effort": effort, "effort_source": mode_source}

    payload = {
        "sp_total": sum(points),
        "tasks": len(points),
        "compared_to": list(compared_to or []),
        "minutes": minutes,
        "buffer_minutes": buffer_minutes,
        "weakest_n": min(r["n"] for r in chosen),
        # какие из использованных полок не измерены, а достроены интерполяцией:
        # такую оценку надо озвучивать с оговоркой, а не как измеренную
        "interpolated_shelves": interpolated,
        # Откуда взялись минуты. Без этого поля линейку не отличить: полки
        # журнала печатает `stats`, полки фонда считает оценка, числа расходятся
        # вдвое, и по журнальным уже был сделан вывод, что оценка врёт.
        "source": RULER_SOURCES["extrapolated" if above_fund else
                                ("fund" if story_shelves() else "journal")],
        "scope": scope,
        "mode": mode,
    }
    # Из какой версии эталонов взяты минуты: без этого оценку, смешанную из двух
    # версий, не отличить от снятой целиком по одной.
    info = shelf_versions()
    if info:
        payload["version"] = {
            "current": info["current"],
            "coefficient": info["coefficient"],
            "coefficient_why": info["coefficient_why"],
            "shelves": {str(sp): info["shelves"][sp] for sp in sorted(set(snapped))
                        if sp in info["shelves"]},
        }
    if compared_to:
        payload["compared_facts"] = [fund_fact(ref) for ref in compared_to]
    if above_fund:
        # Выше пятёрки эталонов нет и не будет: история такого размера — пучок
        # осей, а не новая ось. Число достроено от верхнего эталона, и выдавать
        # его за измеренную полку нельзя.
        payload["above_fund"] = above_fund
    if scope == "plan":
        # План не приклеивается к ходу: ход — это озвучивание оценки (минуты), а
        # план идёт часами. Именно так в историю попали sp=45/46/48 — сумма очков
        # плана против длительности одного хода. Пара плана закрывается отдельно,
        # командой record-plan.
        state.setdefault("plans", []).append({
            "label": (label or "(без метки)")[:200],
            "points": points,
            "shelf_seconds": int(round(minutes * 60)),
            "buffer_seconds": int(round(buffer_minutes * 60)),
            "actual_seconds": None,
        })
        payload["plan_opened"] = True
    else:
        # Отложить размер, чтобы Stop-хук приклеил его к измеренной паре: сам он
        # знать sp не может, а без sp запись не пополнит шкалу.
        #
        # Кладём СПИСОК очков, а не сумму. Сумма очков — не размер: шкала
        # геометрическая, и 15 SP как "три задачи по 5" стоят совсем не столько,
        # сколько одна задача на 13-21. Раньше здесь лежала сумма, и она уезжала
        # в поле sp как размер одной задачи — из-за этого верх шкалы набивался
        # мусором. Полку пополняет только ход из ОДНОЙ задачи (см. sp_samples),
        # многозадачные идут в статистику точности по предсказанию полок.
        state["pending"] = {
            "points": points,
            "snapped": snapped,
            "shelf_seconds": int(round(minutes * 60)),
            "buffer_seconds": int(round(buffer_minutes * 60)),
        }
        state.pop("pending_sp", None)
    if label:
        state["pending_label"] = label[:200]
    write_state(state)
    print(json.dumps(payload, ensure_ascii=False))


def cmd_record(sp: float, actual_seconds: float, label: str | None,
               seed: bool = False) -> None:
    """Ручная запись пары (размер, факт) — для засева и для шагов, которые
    Stop-хук не измеряет.

    Вместе с фактом сохраняется shelf_seconds — что полка предсказывала НА МОМЕНТ
    записи, до того как этот замер в неё попал. Без этого поля запись пополняла
    шкалу, но проверить по ней точность было нечем: сравнивать приходилось с
    полкой, уже включающей сам замер, то есть с собой. Поле называется не
    estimated_seconds намеренно: estimated_seconds означает "озвучено вслух", и
    смешивать его с предсказанием таблицы нельзя — иначе старая бакетная матрица
    начнёт калиброваться сама на себя.

    seed=True — запись задним числом, когда факт был известен раньше оценки.
    Такие не годятся для замера точности и отделяются в stats.
    """
    state = read_state()
    before = sp_table(calibration_rows(state))
    shelf, buffer_seconds = shelf_seconds_for(before, [sp])
    record = {
        "measured": False,
        "sp": snap_to_scale(sp),
        "label": (label or "(вручную)")[:200],
        "actual_seconds": int(round(actual_seconds)),
        "shelf_seconds": shelf,
        "buffer_seconds": buffer_seconds,
    }
    if abs(float(sp) - record["sp"]) > 1e-9:
        # видно, что размер приведён к шкале, и какой был назван изначально
        record["sp_raw"] = float(sp)
    if seed:
        record["seed"] = True
    state.setdefault("history", []).append(record)
    write_state(state)
    print(json.dumps({"recorded": record}, ensure_ascii=False))


def cmd_record_plan(label: str | None, actual_seconds: float) -> None:
    """Закрыть пару плана: найти открытую запись в plans и вписать факт.

    Верх шкалы (8-34 SP) до сих пор достраивается интерполяцией по одному-двум
    замерам, а оценки планов живут именно там. Пара "оценка плана -> факт плана"
    — единственный способ проверить правило сложения полок целиком, а не по
    отдельным шагам.
    """
    state = read_state()
    plans = state.setdefault("plans", [])
    open_plans = [pl for pl in plans if pl.get("actual_seconds") is None]
    if not open_plans:
        print(
            json.dumps(
                {"error": "нет открытых планов: сначала estimate --sp ... --scope plan"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    if label:
        needle = label.lower()
        matched = [pl for pl in open_plans if needle in pl["label"].lower()]
        if not matched:
            print(
                json.dumps(
                    {"error": "план не найден по метке",
                     "open": [pl["label"] for pl in open_plans]},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        target = matched[-1]
    else:
        target = open_plans[-1]
    target["actual_seconds"] = int(round(actual_seconds))
    write_state(state)
    shelf = target["shelf_seconds"] or 1
    print(json.dumps({
        "closed": target["label"],
        "shelf_minutes": round(target["shelf_seconds"] / 60, 1),
        "actual_minutes": round(target["actual_seconds"] / 60, 1),
        "ratio": round(target["actual_seconds"] / shelf, 2),
    }, ensure_ascii=False))




def _ratio_summary(ratios: list[float]) -> dict:
    """Сводка по набору отношений факт/предсказание.

    Медиана и геометрическое среднее, а не арифметическое: отношения — величина
    мультипликативная, промах вдвое вверх (2.0) и вдвое вниз (0.5) должны
    компенсировать друг друга, а арифметическое среднее этих двух даёт 1.25 и
    делает вид, что есть перекос вверх.
    """
    if not ratios:
        return {"n": 0}
    ordered = sorted(ratios)
    log_sum = sum(math.log(r) for r in ordered if r > 0)
    return {
        "n": len(ordered),
        "median": round(_median(ordered), 2),
        "geomean": round(math.exp(log_sum / len(ordered)), 2),
        "within_1_5x": sum(1 for r in ordered if 1 / 1.5 <= r <= 1.5),
        "within_2x": sum(1 for r in ordered if 0.5 <= r <= 2),
        "worst_over": round(ordered[-1], 2),
        "worst_under": round(ordered[0], 2),
    }


def cmd_rulers() -> None:
    """Сходятся ли линейки: фонд историй против полок журнала.

    Проверки не было вовсе, и расхождение вдвое жило незамеченным, пока не
    обошлось получасом разбора. Теперь оно видно одной командой.

    `agree` отвечает про то, что реально влияет на оценку: берётся ли число из
    фонда. Расхождение самих чисел — не поломка, а разные единицы (размер хода
    против размера истории), и оно показывается справочно.
    """
    state = read_state()
    rows = sp_table(calibration_rows(state))
    fund = story_shelves()
    journal = {r["sp"]: r["minutes"] for r in rows}
    compare = []
    for sp in sorted(set(fund) | set(journal)):
        fund_minutes = round(fund[sp][0], 1) if sp in fund else None
        journal_minutes = journal.get(sp)
        compare.append({
            "sp": sp,
            "fund_minutes": fund_minutes,
            "journal_minutes": journal_minutes,
            "ratio": (round(journal_minutes / fund_minutes, 2)
                      if fund_minutes and journal_minutes else None),
        })
    # Отброшенные не пропадают: они лежат в журнале, и здесь названа причина.
    rejected = []
    for record in calibration_rows(state) + list(state.get("stories", [])):
        if record.get("sp") is None or not record.get("actual_seconds"):
            continue
        reason = shelf_rejection(record)
        if reason:
            rejected.append({
                "label": str(record.get("label") or record.get("us") or "")[:60],
                "sp": record.get("sp"),
                "minutes": round(float(record["actual_seconds"]) / 60, 1),
                "reason": reason,
            })

    print(json.dumps({
        "fund": {"path": str(SP_SCALE_PATH), "shelves": len(fund),
                 "unit": "размер истории"},
        "rejected": rejected,
        "journal": {"shelves": len(journal), "unit": "размер хода"},
        "used_by_estimate": "fund" if fund else "journal",
        "agree": bool(fund),
        "note": ("оценка историй считается по фонду; полки журнала — другая "
                 "единица и линейкой для историй не являются"),
        "compare": compare,
    }, ensure_ascii=False, indent=2))


def cmd_stats() -> None:
    """Состояние калибровки одной командой: полки, точность, здоровье данных.

    Отдельно "по засеву" и "вне засева". Засев по плану 010 вбивался задним
    числом, когда факты уже были известны, и полки по нему же и считались —
    точность на этих записях получается декоративно высокой (медиана ровно 1.00)
    и маскирует реальный перекос. Смешивать их в одну цифру нельзя: именно из-за
    такой смеси калибровка выглядела точной, будучи переоценкой в 1.7 раза.
    """
    state = recompute(read_state())
    write_state(state)
    history = state["history"]
    # Полки — по эталонам и истории вместе, иначе stats показывает не ту таблицу,
    # по которой на самом деле считаются оценки.
    rows = sp_table(calibration_rows(state))

    seed_ratios: list[float] = []
    live_ratios: list[float] = []
    multi_ratios: list[float] = []
    off_scale: list[str] = []
    no_prediction = 0
    snapped = 0

    for record in history:
        sp = record.get("sp")
        actual = record.get("actual_seconds")
        if sp is None or not actual:
            continue
        if float(sp) > SP_MAX:
            off_scale.append(str(record.get("label"))[:60])
            continue
        if record.get("sp_raw") is not None:
            snapped += 1
        shelf = record.get("shelf_seconds")
        if not shelf:
            no_prediction += 1
            continue
        ratio = actual / shelf
        points = record.get("sp_points")
        if points is not None and len(points) > 1:
            multi_ratios.append(ratio)
        elif record.get("seed"):
            seed_ratios.append(ratio)
        else:
            live_ratios.append(ratio)

    plans = state.get("plans", [])
    plan_rows = [
        {
            "label": pl["label"][:60],
            # список очков есть не у всех пар: у перенесённой при миграции его
            # не сохранилось, там объявленная сумма лежит отдельным полем
            "sp_total": sum(pl.get("points") or []) or pl.get("sp_total_declared"),
            "shelf_minutes": round(pl["shelf_seconds"] / 60, 1),
            "actual_minutes": round(pl["actual_seconds"] / 60, 1),
            "ratio": round(pl["actual_seconds"] / (pl["shelf_seconds"] or 1), 2),
        }
        for pl in plans
        if pl.get("actual_seconds")
    ]

    print(json.dumps({
        "shelves": [
            {k: r[k] for k in ("sp", "minutes", "buffer_minutes", "n", "source")}
            for r in rows
        ],
        # Эти полки сняты с ХОДОВ и восстановленных по коммитам задач, а оценка
        # историй считается по фонду (references/sp-scale.json) — числа
        # расходятся вдвое. Без пометки их принимают за рабочую линейку: ровно
        # так 15.09.2026 родился вывод, будто оценка врёт вдвое.
        "shelves_unit": "размер хода, не истории",
        "shelves_warning": ("не линейка историй: оценка идёт по фонду "
                            "references/sp-scale.json, сверка — calibrate.py rulers"),
        "shelves_lifted": [r["sp"] for r in rows if r.get("lifted")],
        "shelves_interpolated": [r["sp"] for r in rows if r["source"] != "measured"],
        "accuracy_live": _ratio_summary(live_ratios),
        "accuracy_seed": _ratio_summary(seed_ratios),
        "accuracy_multitask_turns": _ratio_summary(multi_ratios),
        "plans_closed": plan_rows,
        "plans_open": [pl["label"][:60] for pl in plans if pl.get("actual_seconds") is None],
        "health": {
            "history_records": len(history),
            "with_sp": sum(1 for r in history if r.get("sp") is not None),
            "snapped_to_scale": snapped,
            "without_shelf_prediction": no_prediction,
            "off_scale_ignored": off_scale,
        },
    }, ensure_ascii=False, indent=2))


# --- режим работы -----------------------------------------------------------
#
# Модель и уровень усилий записываются рядом с фактом, но оценку не двигают:
# отдельная поправка на режим снята 26.09.2026. Смена режима, которая держится,
# даёт систематическое расхождение, и лечится оно новой версией эталонов, а
# разовое повышение уровня на отдельной задаче — законная часть выборки.


def current_effort() -> tuple[str | None, str]:
    """Режим этой сессии: последняя запись самого свежего транскрипта.

    Спрашивать его ключом каждый раз нельзя — оценка объявляется десятки раз за
    сессию, и ключ забудут. Самый свежий транскрипт на этой машине и есть та
    сессия, которая оценку запрашивает.
    """
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None, "транскриптов нет"
    files = [p for d in projects.iterdir() if d.is_dir() for p in d.glob("*.jsonl")]
    if not files:
        return None, "транскриптов нет"
    newest = max(files, key=lambda p: p.stat().st_mtime)
    effort = None
    try:
        for line in newest.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "assistant":
                effort = entry.get("perTurnEffort") or entry.get("effort") or effort
    except OSError:
        return None, "транскрипт не прочитался"
    return effort, "транскрипт"


def modes_summary(records: list[dict]) -> dict:
    """Сводка по режимам работы: сколько замеров и минут набралось в каждом.

    Восстановленные задним числом считаются отдельной колонкой, а не
    растворяются в общем числе: доверие к ним ниже, и смешивать их с замерами,
    записанными по ходу, нельзя.

    Доля нераспознанных печатается всегда — это и есть ответ на вопрос, можно
    ли вообще доверять сравнению режимов. Пропуск законен: замеры со второй
    машины транскрипта здесь не имеют и режима не получат никогда.
    """
    total = len(records)
    # Журнал сливается между машинами построчным union. Разовая правка,
    # переписывающая файл целиком, для union выглядит как новые строки, и
    # старые остаются рядом — так 2322 записи однажды стали 3926. Дубль не
    # ломает ничего видимого, он тихо удваивает выборку, поэтому о нём
    # говорится в сводке, а не выясняется через месяц.
    seen: set = set()
    duplicates = 0
    for r in records:
        k = (r.get("ts"), r.get("sec"), r.get("host"), r.get("kind"))
        if k in seen:
            duplicates += 1
        seen.add(k)

    groups: dict[str, dict] = {}
    unknown_n = 0
    unknown_sec = 0.0
    for r in records:
        effort = r.get("effort")
        sec = float(r.get("sec") or 0)
        if not effort:
            unknown_n += 1
            unknown_sec += sec
            continue
        g = groups.setdefault(effort, {
            "effort": effort, "n": 0, "restored": 0, "seconds": 0.0,
            "models": {},
        })
        g["n"] += 1
        g["seconds"] += sec
        if r.get("restored"):
            g["restored"] += 1
        model = r.get("model")
        if model:
            g["models"][model] = g["models"].get(model, 0) + 1

    modes = []
    for g in sorted(groups.values(), key=lambda x: -x["n"]):
        g["minutes"] = round(g["seconds"] / 60, 1)
        g.pop("seconds")
        modes.append(g)
    return {
        "total": total,
        "duplicates": duplicates,
        "duplicates_note": ("журнал задвоен — прогони "
                            "dedup_20260916_steps.py" if duplicates else ""),
        "modes": modes,
        "unknown": {
            "n": unknown_n,
            "minutes": round(unknown_sec / 60, 1),
            "share_pct": round(unknown_n / total * 100, 1) if total else 0.0,
        },
    }


def cmd_units(days: int = 1) -> None:
    """Единицы работы за последние N дней и полнота покрытия.

    Печатает и род времени: работа отделена от разговора, потому что
    планирование и разбор стоят времени, но сделанного собой не показывают.
    """
    here = Path(__file__).resolve().parent
    for extra in (str(here), str(Path.home() / ".cursor" / "hooks")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import units as units_mod
    import steps_store

    projects = Path.home() / ".claude" / "projects"
    files = [p for d in projects.iterdir() if d.is_dir()
             for p in d.glob("*.jsonl")] if projects.is_dir() else []
    cutoff = time.time() - days * 86400
    files = [p for p in files if p.stat().st_mtime >= cutoff]

    all_units: list[dict] = []
    for path in files:
        all_units.extend(units_mod.units_from_transcript(path))
    all_units.sort(key=lambda u: u["started"])

    steps = [s for s in steps_store.read_all()
             if (s.get("ts") or "") >= datetime.fromtimestamp(
                 cutoff, tz=timezone.utc).isoformat()]
    by_genre: dict[str, dict] = {}
    for u in all_units:
        g = by_genre.setdefault(u["genre"], {"genre": u["genre"], "n": 0,
                                             "minutes": 0.0})
        g["n"] += 1
        g["minutes"] = round(g["minutes"] + u["seconds"] / 60, 1)

    print(json.dumps({
        "days": days,
        "units": len(all_units),
        "by_genre": list(by_genre.values()),
        "overlaps": units_mod.overlaps(all_units),
        "coverage": units_mod.coverage(steps, all_units),
        "sample": [{k: u[k] for k in ("opened_by", "genre", "seconds", "steps")}
                   for u in all_units[-5:]],
    }, ensure_ascii=False, indent=2))


def _count(rows: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        key = r.get(field) or "(не задано)"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def cmd_modes() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import steps_store
    state = read_state()
    stories = state.get("stories", [])
    payload = modes_summary(steps_store.read_all())
    # Восстановленное задним числом отделено от записанного по ходу: доверие к
    # нему ниже, и смешивать одно с другим в сводке нельзя.
    payload["stories"] = {
        "total": len(stories),
        "measured": sum(1 for s in stories
                        if s.get("effort") and not s.get("restored")),
        "restored": sum(1 for s in stories if s.get("restored")),
        "without_mode": sum(1 for s in stories if not s.get("effort")),
        "by_epic": _count(stories, "epic"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_versions(path: str | None = None) -> None:
    """Версии эталонов и сколько замеров в каждой (план 086)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import steps_store
    import versions
    vs = versions.load(Path(path) if path else versions.VERSIONS_PATH)
    stories = read_state().get("stories", [])
    print(json.dumps(versions.summary(steps_store.read_all(), stories, vs),
                     ensure_ascii=False, indent=2))


def cmd_version_new(start: str, reason: str, path: str | None = None) -> None:
    """Объявить новую версию эталонов: с начала дня `start` и с причиной."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import versions
    target = Path(path) if path else versions.VERSIONS_PATH
    try:
        new = versions.declare(target, start, reason)
    except ValueError as e:
        print(f"version-new: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"version_declared": new}, ensure_ascii=False))


def drift_report() -> dict:
    """Есть ли систематическое расхождение факта с оценкой (план 086, US-0621).

    Сравнивается с оценкой, уже учитывающей смесь версий: полки — из
    story_shelves, шаги — из versions.step_references.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import steps_store
    import versions
    vs = versions.load()
    steps = steps_store.read_all()
    kinds = versions.step_references(steps, vs)["kinds"]
    shelves = {sp: centre for sp, (centre, _) in story_shelves().items()}
    return versions.drift(read_state().get("stories", []), steps, kinds, shelves, vs)


def drift_prompt(report: dict) -> str:
    """Текст для сессии: задать владельцу вопрос карточкой. Пусто — вопроса нет."""
    if not report.get("propose"):
        return ""
    parts = []
    for name, key in (("историй", "stories"), ("шагов", "steps")):
        x = report.get(key)
        if x:
            parts.append(f"{x['n']} {name} подряд идут {x['side']} оценки: медиана "
                         f"факт/оценка {x['median_ratio']}, по одну сторону "
                         f"{round(x['same_side_share'] * 100)}%")
    # Граница версии ставится по местному дню, а серия хранит UTC: 23:30 UTC —
    # это уже следующий день по Москве.
    since = (datetime.fromisoformat(report["since"]).astimezone().date().isoformat()
             if report.get("since") else "")
    return (
        "[версии эталонов] Систематическое расхождение с " + since + ": "
        + "; ".join(parts) + ". Спроси владельца карточкой AskUserQuestion: "
        "«Завести новую версию эталонов?» — варианты «Завести версию» (причину "
        "владелец пишет в ответе) и «Не сейчас»; в тексте вопроса назови эти "
        f"цифры. «Завести» → `calibrate.py version-new --from {since} --reason "
        "\"<причина владельца>\"`; «Не сейчас» → `calibrate.py version-decline`. "
        "Сама версию не заводи."
    )


def cmd_drift() -> None:
    report = drift_report()
    report["prompt"] = drift_prompt(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_version_decline() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import versions
    try:
        at = versions.decline(versions.VERSIONS_PATH)
    except ValueError as e:
        print(f"version-decline: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"declined_at": at}, ensure_ascii=False))


USAGE = (
    "usage:\n"
    "  calibrate.py sp-table\n"
    "  calibrate.py stats                                # состояние калибровки\n"
    "  calibrate.py modes                                # режимы: сколько набралось\n"
    "  calibrate.py versions [--versions <файл>]         # версии эталонов\n"
    "  calibrate.py version-new --from ГГГГ-ММ-ДД --reason <причина>\n"
    "  calibrate.py drift                                # есть ли систематическое расхождение\n"
    "  calibrate.py version-decline                      # «не сейчас» на вопрос о версии\n"
    "  calibrate.py rulers                               # сходятся ли линейки\n"
    "  calibrate.py estimate --sp <points...> [label]     # основной путь\n"
    "  calibrate.py estimate --sp <points...> --scope plan --label <метка>\n"
    "  calibrate.py record --sp <point> --actual <sec> [--seed] [label]\n"
    "  calibrate.py record-plan --actual <sec> [--label <подстрока>]\n"
    "  calibrate.py recompute\n"
    "  calibrate.py estimate <task|step|workflow> <sec>   # прежний механизм, устарел\n"
)


def pop_flag(args: list[str], name: str) -> bool:
    """Снять флаг без значения, если он есть."""
    if name in args:
        args.remove(name)
        return True
    return False


def pop_option(args: list[str], name: str) -> str | None:
    """Снять опцию со значением. Возвращает значение или None.

    Раньше значения выковыривались через повторные args.index() уже по
    изменённому списку — на двух-трёх опциях это давало смещение индексов и
    метка склеивалась с числами. Здесь список правится за один проход.
    """
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        return None
    value = args[idx + 1]
    del args[idx : idx + 2]
    return value


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    command, rest = args[0], args[1:]

    if command == "sp-table":
        cmd_sp_table()
    elif command == "stats":
        cmd_stats()
    elif command == "modes":
        cmd_modes()
    elif command == "versions":
        cmd_versions(pop_option(rest, "--versions"))
    elif command == "drift":
        cmd_drift()
    elif command == "version-decline":
        cmd_version_decline()
    elif command == "version-new":
        path = pop_option(rest, "--versions")
        start = pop_option(rest, "--from")
        reason = pop_option(rest, "--reason")
        if not start or not reason:
            print("version-new: нужны --from ГГГГ-ММ-ДД и --reason", file=sys.stderr)
            sys.exit(1)
        cmd_version_new(start, reason, path)
    elif command == "units":
        cmd_units(int(pop_option(rest, "--days") or 1))
    elif command == "estimate" and rest[:1] == ["--sp"]:
        rest = rest[1:]
        scope = pop_option(rest, "--scope") or "turn"
        if scope not in {"turn", "plan"}:
            print("--scope: turn или plan", file=sys.stderr)
            sys.exit(1)
        label_opt = pop_option(rest, "--label")
        compared_opt = pop_option(rest, "--compared-to")
        points: list[float] = []
        tail: list[str] = []
        for token in rest:
            if not tail:
                try:
                    points.append(float(token))
                    continue
                except ValueError:
                    pass
            tail.append(token)
        if not points:
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        label = label_opt or " ".join(tail) or None
        # Отказ, а не число: размер, названный без эталона, — это не
        # предсказание, а метка, и полку он бы портил. То же правило, что у
        # истории (shelf_rejection), только называется оно здесь вслух и до
        # работы, а не при разборе шкалы через неделю.
        refs = [r.strip() for r in (compared_opt or "").split(",") if r.strip()]
        if not refs:
            print("оценка без эталона не даётся: назовите, с чем сравнивали "
                  "размер — `--compared-to US-NNNN` (фонд: "
                  "references/stories/, оси: references/stories/AXES.md). "
                  "Размер, названный на глаз, промахивается вчетверо.",
                  file=sys.stderr)
            sys.exit(2)
        unknown = [r for r in refs if not fund_axis(r)]
        if unknown:
            print("в фонде нет эталонов: " + ", ".join(unknown)
                  + " — сверьтесь с references/stories/", file=sys.stderr)
            sys.exit(2)
        cmd_estimate_sp(points, label, scope, refs)
    elif command == "rulers":
        cmd_rulers()
    elif command == "story-start":
        plan = pop_option(rest, "--plan")
        us = pop_option(rest, "--us")
        sp_value = pop_option(rest, "--sp")
        title = pop_option(rest, "--title")
        if not (plan and us):
            print("story-start --plan NNN --us US-NNNN [--sp N] [--title ...]"
                  " — размер и название читаются из плана, если не названы",
                  file=sys.stderr)
            sys.exit(1)
        cmd_story_start(plan, us,
                        float(sp_value) if sp_value else None, title)
    elif command == "story-blocked":
        cmd_story_blocked()

    elif command == "story-block":
        us = pop_option(rest, "--us")
        kind = pop_option(rest, "--kind")
        why = pop_option(rest, "--why")
        if not us or not kind or why is None:
            print("story-block --us US-NNNN --kind "
                  + "|".join(BLOCK_KINDS) + " --why «что мешает»",
                  file=sys.stderr)
            sys.exit(1)
        cmd_story_block(us, kind, why)

    elif command == "story-close":
        us = pop_option(rest, "--us")
        seconds = pop_option(rest, "--seconds")
        minutes = pop_option(rest, "--minutes")
        if not us:
            print("story-close --us US-NNNN [--minutes N | --seconds N]",
                  file=sys.stderr)
            sys.exit(1)
        actual = None
        if seconds is not None:
            actual = float(seconds)
        elif minutes is not None:
            actual = float(minutes) * 60
        cmd_story_close(us, actual)
    elif command == "stories":
        cmd_story_list(closed_only="--closed" in rest,
                       open_only="--open" in rest)
    elif command == "record":
        seed = pop_flag(rest, "--seed")
        sp_value = pop_option(rest, "--sp")
        actual_value = pop_option(rest, "--actual")
        if sp_value is None or actual_value is None:
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        label_opt = pop_option(rest, "--label")
        label = label_opt or " ".join(rest) or None
        cmd_record(float(sp_value), float(actual_value), label, seed)
    elif command == "record-plan":
        actual_value = pop_option(rest, "--actual")
        if actual_value is None:
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        label_opt = pop_option(rest, "--label")
        cmd_record_plan(label_opt or " ".join(rest) or None, float(actual_value))
    elif command == "references":
        sp_value = pop_option(rest, "--sp")
        limit_value = pop_option(rest, "--limit")
        cmd_references(float(sp_value) if sp_value else None, int(limit_value or 24))
    elif command == "recompute":
        cmd_recompute()
    elif command == "estimate" and len(rest) == 2:
        cmd_estimate(rest[0], float(rest[1]))
    else:
        print(USAGE, file=sys.stderr)
        sys.exit(1)



def _now_iso() -> str:
    """Момент в однозначном виде: местное время в записи бесполезно, когда
    машин две и они в разных поясах."""
    return datetime.now(timezone.utc).isoformat()


STORY_HEADING = re.compile(
    r"^##\s+US-(\d{4})\.\s+(.+?)\s*(?:—\s*([\d.]+|\?)\s*SP\s*)?$", re.M)


def _plan_text(plan: str, plan_path=None) -> str:
    if plan_path is None:
        matches = sorted(m for d in plan_dirs() for m in d.glob(f"{plan}-*.md"))
        if not matches:
            return ""
        plan_path = matches[0]
    try:
        return io.open(plan_path, encoding="utf-8").read()
    except OSError:
        return ""


def story_body(plan: str, us: str, plan_path=None) -> str:
    """Текст одной истории из плана — от её заголовка до следующего.

    Нужен, чтобы прочитать обоснование размера. Читать весь план нельзя:
    эталон, названный в соседней истории, обосновывал бы и эту.
    """
    text = _plan_text(plan, plan_path)
    if not text:
        return ""
    number = str(us).split("-")[-1]
    heads = list(STORY_HEADING.finditer(text))
    for i, m in enumerate(heads):
        if m.group(1) == number:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end]
    return ""


def story_from_plan(plan: str, us: str,
                    plan_path=None) -> tuple[float | None, str]:
    """Размер и название истории — из самого плана.

    Дублировать их руками нельзя: через неделю не вспомнить ни формулировку,
    ни размер, а две записи одной величины расходятся при первой же правке
    плана. План здесь — источник, запись факта — производное.
    """
    if plan_path is None:
        matches = sorted(m for d in plan_dirs() for m in d.glob(f"{plan}-*.md"))
        if not matches:
            return None, ""
        plan_path = matches[0]
    try:
        text = io.open(plan_path, encoding="utf-8").read()
    except OSError:
        return None, ""

    number = str(us).split("-")[-1]
    for found, title, sp in STORY_HEADING.findall(text):
        if found == number:
            try:
                return float(sp), title.strip()
            except (TypeError, ValueError):
                return None, title.strip()
    return None, ""


def open_stories() -> list[dict]:
    """Истории без факта. Незакрытая запись — потерянный замер, и молчать о
    ней нельзя: через неделю уже не восстановить, сколько ушло."""
    return [s for s in (read_state().get("stories") or [])
            if s.get("actual_seconds") is None]


# Эталон, с которым сравнивали размер: `references/stories/US-NNNN.md` в тексте
# истории. Голый номер не годится — он означает историю того же плана.
# Трёхчастное основание размера проверяется в двух местах — здесь и в валидаторе
# плана. Выражения берутся ОТТУДА, а не пишутся заново: записанные дважды, они
# разошлись молча. `calibrate` запрещал перенос строки между словом «мест» и
# числом, `lint_plan` разрешал, и US-0440 плана 064 попала по разные стороны —
# история размечена полностью, а факт в полки не пустили.
#
# Направление зависимости такое: формат плана принадлежит валидатору, журнал
# калибровки его только читает.
_HOOKS = Path.home() / ".cursor" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.append(str(_HOOKS))
try:
    from lint_plan import AXIS_RE, FUND_REF_RE, PLACES_RE  # noqa: E402
except ImportError as _exc:  # валидатор недоступен — считаем сами
    # Тихий откат и был причиной того, что расхождение жило незамеченным.
    print(f"[calibrate] правило размера не взято из валидатора: {_exc}",
          file=sys.stderr)
    FUND_REF_RE = re.compile(r"references/stories/US-(\d{4})")
    AXIS_RE = re.compile(r"[Оо]сь\s+([A-K])(?![A-Za-z])")
    PLACES_RE = re.compile(
        r"[Мм]ест\w*[^.]{0,24}?"
        r"(одно|двое|трое|четверо|пятеро|один|два|три|четыре|пять|"
        r"шесть|семь|восемь|\d+|больше|меньше|столько\s+же)")

FUND_STORIES_DIR = Path.home() / ".cursor" / "references" / "stories"


def fund_axis(ref: str) -> str | None:
    """Ось эталона из его шапки. None — эталона нет или оси в нём не записано."""
    path = FUND_STORIES_DIR / f"{ref}.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines()[:20]:
        if line.startswith("axis:"):
            return line.split(":", 1)[1].strip() or None
    return None


def fund_fact(ref: str) -> dict:
    """Факт эталонной истории с номером версии, в которой он снят, и факт
    похожих историй текущей версии: та же полка, эталон той же оси."""
    path = FUND_STORIES_DIR / f"{ref}.md"
    head: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines()[1:20]:
            if line.startswith("---"):
                break
            key, _, value = line.partition(":")
            head[key.strip()] = value.strip()
    out: dict = {"us": ref}
    if not head:
        out["error"] = "эталона нет в фонде"
        return out
    sp = float(head.get("sp") or 0)
    axis = head.get("axis")
    out.update({"sp": sp, "axis": axis,
                "fact_minutes": round(float(head.get("fact_seconds") or 0) / 60, 1),
                "fact_version": int(head.get("fact_version") or 1)})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import versions
        vs = versions.load()
        current, _ = versions.active(vs)
        similar = [st["actual_seconds"] / 60 for st in read_state().get("stories", [])
                   if st.get("actual_seconds") and st.get("sized_by") == "comparison"
                   and float(st.get("sp") or 0) == sp
                   and versions.record_version(st, vs) == current
                   and any(fund_axis(r) == axis for r in st.get("compared_to") or [])]
        out["similar_current_version"] = {
            "version": current, "n": len(similar),
            "median_minutes": round(versions._median(similar), 1) if similar else None}
    except Exception:
        pass
    return out


def sizing_basis(plan: str, us: str, plan_path=None) -> tuple[str, list[str]]:
    """Чем обоснован размер истории: ("comparison"|"guess", [эталоны]).

    Пометка идёт в журнал вместе с фактом, и по ней же работает отбор в полки
    (shelf_rejection). Без неё запись, размер которой назван на глаз,
    неотличима от посчитанной сравнением, и обе двигают полку — а раздутая
    тянет минуты на очко вниз и раздувает следующую оценку.
    """
    body = story_body(plan, us, plan_path) or ""
    refs = sorted({"US-" + n for n in FUND_REF_RE.findall(body)})
    if not refs:
        return "guess", []

    # Эталон должен существовать в фонде: ссылка на несуществующий файл —
    # это опечатка или выдуманный номер, и проверить по ней нечего.
    known = [r for r in refs if fund_axis(r)]
    if not known:
        return "guess", refs

    # Ось названа и совпадает с осью эталона. Без этого сравнение формально:
    # номер эталона в тексте стоит, а меряли им другую работу.
    named = {m.upper() for m in AXIS_RE.findall(body)}
    if not named or not named & {fund_axis(r) for r in known}:
        return "guess", refs

    # Места названы числом. Эталон даёт уровень, места — сдвиг от него; без
    # них размер берётся из эталона целиком, каким бы он ни был.
    if not PLACES_RE.search(body):
        return "guess", refs

    return "comparison", refs


def cmd_story_start(plan: str, us: str, sp: float | None = None,
                    title: str | None = None, plan_path=None) -> None:
    """Открыть запись факта по истории: размер, названный ДО работы.

    Смысл в порядке. Размер, записанный до начала, — это предсказание, и его
    можно сравнить с фактом. Размер, вписанный после, — это пересказ факта, и
    сравнивать его не с чем.

    Размер и название по умолчанию читаются из плана. Боль не копируется: она
    живёт в плане и адресуется парой «план + номер».
    """
    state = read_state()
    stories = state.setdefault("stories", [])
    # Заблокированная запись открытой не считается: работать по ней было
    # нечем, и снятие блокировки — это новое предсказание, названное до работы,
    # а не продолжение прежнего. Прежняя запись остаётся с причиной: история
    # блокировки не стирается, иначе по журналу не понять, что тормозило.
    if any(s["us"] == us and s.get("actual_seconds") is None
           and not s.get("blocked") for s in stories):
        raise SystemExit(f"история {us} уже открыта — сначала закрой её")

    plan_sp, plan_title = story_from_plan(plan, us, plan_path)
    if sp is None:
        sp = plan_sp
    # Размер бывает неизвестен законно: пока нет эталонных историй, сравнивать не
    # с чем, и в плане стоит `? SP`. Раньше здесь был отказ, и он приводил к
    # обратному от задуманного — размер выдумывался на месте, только чтобы запись
    # открылась, и в базу эталонов ехало вымышленное предсказание. Факт без
    # предсказания полезен (из него и вырастут эталоны), предсказание из воздуха —
    # нет. Полка при неизвестном размере не считается: сравнивать нечего.
    if not title:
        title = plan_title

    sized_by, compared_to = sizing_basis(plan, us, plan_path)
    if sp is None:
        shelf, buffer_seconds = None, None
    else:
        shelf, buffer_seconds = shelf_seconds_for(
            sp_table(calibration_rows(state)), [sp])
    record = {
        "plan": str(plan),
        "us": us,
        "title": (title or "")[:200],
        "sp": float(sp) if sp is not None else None,
        # Как получен размер и с чем сравнивали — часть записи, а не устная
        # договорённость: иначе эталоном эта история стать не сможет.
        "sized_by": sized_by,
        "compared_to": compared_to,
        "started": _now_iso(),
        "finished": None,
        "actual_seconds": None,
        "source": None,
        "shelf_seconds": shelf,
        "buffer_seconds": buffer_seconds,
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
    }
    # Режим пишется при открытии, а не при закрытии:
    # уровень усилий задаётся на сессию, и единицей назначения может быть только
    # работа целиком. Вписанный после работы, он стал бы пересказом факта — той
    # же болезнью, от которой защищён размер.
    effort, effort_source = current_effort()
    record["effort"] = effort
    record["effort_source"] = effort_source
    stories.append(record)
    write_state(state)
    print(json.dumps({"story_opened": record}, ensure_ascii=False))


# Почему история может стоять, если это не лень исполнителя. Список закрытый:
# свободная формулировка со временем превращается в отговорку, а по закрытому
# перечню видно, упёрлась работа в среду, в человека или в чужую работу.
BLOCK_KINDS = ("environment", "owner", "collision")


def cmd_story_block(us: str, kind: str, why: str) -> None:
    """Отметить историю заблокированной: работа стоит не по решению агента.

    Предохранитель плана умел два состояния — закрыта или нет, — и «не закрыта,
    и закрыть её нечем из доступного» читал как «ленится». 24.09.2026 это дало
    полтора десятка холостых оборотов: агент отвечал, что работа исчерпана, хук
    снова держал ход.

    **Причина обязательна.** Без неё «заблокировано» неотличимо от «не
    захотелось», и через месяц по журналу не понять, упиралась работа в среду
    или в исполнителя.

    **Факт не записывается.** Заблокированная история ничего не измерила:
    прошедшее время — это ожидание, а не работа, и в полки ему нельзя. Когда
    блокировка снимется, история открывается заново — с новым предсказанием,
    названным до работы.

    **Снимает блокировку владелец,** заводя историю снова. Сам агент не должен
    получить способ закончить ход раньше времени.
    """
    if kind not in BLOCK_KINDS:
        raise SystemExit(
            f"вида блокировки «{kind}» не бывает; есть только "
            + ", ".join(BLOCK_KINDS))
    if not (why or "").strip():
        raise SystemExit(
            "блокировка без причины не принимается — назовите, что именно "
            "не даёт довести историю до демонстрации")

    state = read_state()
    open_ones = [s for s in (state.get("stories") or [])
                 if s["us"] == us and s.get("actual_seconds") is None
                 and not s.get("blocked")]
    if not open_ones:
        raise SystemExit(
            f"открытой истории {us} нет: она либо не начиналась, либо уже "
            f"закрыта или заблокирована.")

    story = open_ones[-1]
    story["finished"] = _now_iso()
    story["source"] = "blocked"
    story["blocked"] = {"kind": kind, "why": why.strip(), "at": story["finished"]}
    write_state(state)

    print(json.dumps({"story_blocked": us, "kind": kind, "why": why.strip(),
                      "note": "факт не записан: ожидание работой не считается"},
                     ensure_ascii=False))


def cmd_story_close(us: str, actual_seconds: float | None = None) -> None:
    """Вписать факт в открытую запись истории.

    По умолчанию факт — это время от открытия до закрытия. Вычитать из него
    паузы не нужно: простой между историями означает, что план выполнялся с
    остановками, а это дефект работы, а не измерения, и чинится он в
    `execute-plan`. Машинерия вычитания лечила бы симптом.

    Работа действительно шла не подряд — человек прервал, машина
    перезагрузилась, история переехала на завтра — факт называется явно, и в
    записи видно, что он назван вручную.

    Факт вне правдоподобного диапазона записывается как есть, но закрытие
    говорит об этом вслух: запись полку не двинет, и узнать это надо в момент
    закрытия, а не через неделю при разборе шкалы. Молчание тут дороже всего —
    оставленная на ночь сессия выглядит в журнале обычной историей.
    """
    state = read_state()
    stories = state.get("stories") or []
    open_ones = [s for s in stories
                 if s["us"] == us and s.get("actual_seconds") is None]
    if not open_ones:
        raise SystemExit(
            f"открытой истории {us} нет: она либо не начиналась, либо уже "
            f"закрыта. Перезакрывать нельзя — факт затрётся.")

    story = open_ones[-1]
    finished = _now_iso()
    source = "manual"
    if actual_seconds is None:
        source = "elapsed"
        started = datetime.fromisoformat(story["started"])
        actual_seconds = max(
            0.0, (datetime.fromisoformat(finished) - started).total_seconds())
    story["finished"] = finished
    story["actual_seconds"] = int(round(float(actual_seconds)))
    story["source"] = source
    write_state(state)

    shelf = story.get("shelf_seconds") or 0
    out = {"story_closed": us, "sp": story["sp"],
           "actual_minutes": round(story["actual_seconds"] / 60, 1)}
    if shelf:
        out["shelf_minutes"] = round(shelf / 60, 1)
        out["ratio"] = round(story["actual_seconds"] / shelf, 2)
    # Причина отбраковки вычисляется, а не хранится: порог со временем
    # двигается, и вписанная в запись пометка разошлась бы с правилом.
    reason = shelf_rejection(story)
    if reason:
        out["not_counted"] = reason
    print(json.dumps(out, ensure_ascii=False))


def cmd_story_blocked() -> None:
    """Заблокированные истории с причинами.

    Блокировка обязана быть видной: незаметная превращается в тихо брошенную
    работу, а это ровно то, от чего предохранитель и написан.
    """
    rows = [s for s in (read_state().get("stories") or []) if s.get("blocked")]
    for s in rows:
        b = s["blocked"]
        print(f"{s['plan']} {s['us']}  {b.get('kind', '?')}  {b.get('why', '')}")
    print(f"заблокировано: {len(rows)}")


def cmd_story_list(closed_only: bool = False,
                   open_only: bool = False) -> None:
    """Закрытые — кандидаты в эталоны, открытые — забытые замеры."""
    stories = read_state().get("stories") or []
    if closed_only:
        stories = [s for s in stories if s.get("actual_seconds") is not None]
    if open_only:
        # Заблокированная история не открыта: работать по ней нечем, и в
        # списке забытых замеров ей не место. Видной она остаётся отдельно.
        stories = [s for s in stories
                   if s.get("actual_seconds") is None and not s.get("blocked")]
    for s in stories:
        if s.get("blocked"):
            fact = f"ЗАБЛОКИРОВАНА ({s['blocked'].get('kind', '?')})"
        elif s.get("actual_seconds") is None:
            fact = "открыта"
        else:
            fact = f"{round(s['actual_seconds'] / 60, 1)} мин"
        # Размер бывает неизвестен намеренно: пока нет эталонных историй, сравнивать
        # не с чем, и в плане стоит `? SP`. Печатаем тем же знаком, а не «None».
        size = s["sp"] if s.get("sp") is not None else "?"
        print(f"{s['plan']} {s['us']}  {size} SP  {fact}  {s['title']}")
    print(f"всего: {len(stories)}")


if __name__ == "__main__":
    main()

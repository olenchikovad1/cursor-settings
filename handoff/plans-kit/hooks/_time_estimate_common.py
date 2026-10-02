"""Общие куски для трёх хуков progress-time-estimates/time-estimate-calibration:
check_time_estimate_start.py (UserPromptSubmit), check_time_estimate_step.py
(PostToolUse), check_time_estimate_log.py (Stop).

Вынесено, чтобы разбор транскрипта и regex-проверки не расходились между
тремя точками входа — раньше вся логика жила только в Stop-хуке, который
срабатывает один раз в конце хода, поэтому нарушения формата попадали в
фидбек только пост-фактум, за весь ход разом.
"""

import json
import re
import sys
from pathlib import Path

MAX_WORDS = 12
MIN_STEPS_FOR_WHOLE_TASK_ESTIMATE = 2
TIME_ANALYSIS_DIR = Path.home() / ".cursor" / "time-analysis"

# Журнал калибровки разложен по файлам: matrix.json +
# records/*.jsonl + .pending.json (см. time-analysis/journal_store.py). Читать
# и писать его напрямую больше нельзя — только через это хранилище, иначе хук
# и calibrate.py разъедутся по формату.
sys.path.insert(0, str(TIME_ANALYSIS_DIR))
try:
    import journal_store as _journal
except ImportError:  # хранилища нет — хуки обязаны продолжать работать
    _journal = None

# Оставлено для совместимости: путь ещё встречается в сообщениях и старых
# записях. Само состояние теперь не в этом файле.
JOURNAL_PATH = TIME_ANALYSIS_DIR / "task-estimates.json"


def journal_read_state() -> dict:
    if _journal is None:
        return {}
    try:
        return _journal.read_state()
    except Exception:
        return {}


def journal_write_state(state: dict) -> bool:
    if _journal is None:
        return False
    try:
        _journal.write_state(state)
        return True
    except Exception:
        return False


def journal_mtime() -> float:
    """Момент последней записи журнала — по самому свежему из файлов."""
    times = []
    for p in (TIME_ANALYSIS_DIR / "matrix.json",
              TIME_ANALYSIS_DIR / "records" / "history.jsonl",
              JOURNAL_PATH):
        try:
            times.append(p.stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else 0.0
# Момент начала хода (ставит UserPromptSubmit) — единственный честный способ
# получить длительность: раньше actual_seconds писались "по ощущениям" задним
# числом, и калибровка настраивалась на выдуманные числа.
TURN_START_PATH = TIME_ANALYSIS_DIR / ".turn_start"
# Замечания по прошедшему ходу, которые нужно показать в НАЧАЛЕ следующего.
# Stop-хук их только записывает, UserPromptSubmit — читает и стирает.
PENDING_FEEDBACK_PATH = TIME_ANALYSIS_DIR / ".pending_feedback.json"

ESTIMATE_PATTERN = re.compile(
    r"\d+\s*(секунд|сек\.?|минут|мин\.?)\b|займ[её]т\s+\d+|оцен(ка|ию|ила|ено)\s+в\s+\d+",
    re.IGNORECASE,
)

WHOLE_TASK_CONTEXT = re.compile(r"задач|целиком|весь\s+ход|в\s+целом", re.IGNORECASE)

# Контекст для оценки времени ФОНОВОГО Workflow (пайплайна субагентов) — она
# должна звучать отдельно от обычной "оценки задачи целиком" (та — про твою
# же работу в чате, не про то, сколько будут работать фоновые субагенты).
WORKFLOW_CONTEXT = re.compile(r"пайплайн|воркфлоу|workflow|фоново", re.IGNORECASE)

# Запрещённые вводные слова перед шагом ("Дальше: X", "Теперь X", "Сейчас X" —
# любое из них лишнее, шаг и так виден по факту нового сообщения). Раньше
# ловилось только "дальше:" с двоеточием — пользователь начал писать "Дальше —"
# (тире вместо двоеточия) и это проходило незамеченным. Матчим слово целиком
# и любой/никакой разделитель после него, а не конкретную пунктуацию.
INTRO_WORD_PATTERN = re.compile(
    r"^\s*(дальше|далее|теперь|сейчас|итак)\b[\s:,\-—]*",
    re.IGNORECASE,
)


def _is_real_user_turn(entry: dict) -> bool:
    """True для настоящего сообщения пользователя, False для tool_result.

    В транскрипте результат инструмента возвращается как запись с
    type == "user" (роль tool_result в API) — визуально неотличима от
    настоящего сообщения человека без проверки контента.
    """
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return True
    return not all(b.get("type") == "tool_result" for b in content)


def turn_messages(transcript_path: str) -> tuple[list[str], list[str]]:
    """(шаги с вызовом инструмента, весь текст ассистента) с последнего user-хода.

    Текст-нарратив и tool_use часто попадают в РАЗНЫЕ последовательные assistant-
    записи транскрипта (не в один content-массив), даже когда модель пишет их
    подряд в одном "ходе" — раньше шаг брал текст только из своей же записи и
    получал пустую строку почти всегда, из-за чего проверка падала независимо
    от того, что реально было написано перед вызовом. Теперь текст копится
    (pending_text) через все assistant-записи без tool_use и прикрепляется к
    следующей записи с tool_use, затем сбрасывается — так шаг видит именно то,
    что было сказано перед ним, а не только текст в его собственной записи.

    Шагом считается только ОБЪЯВЛЕННОЕ действие — запись с tool_use, перед
    которой был текст. Вызов инструмента без текста перед ним — продолжение
    уже объявленного шага (дочитать файл, прогнать ту же команду по другому
    пути, добрать grep), а не новый шаг: требовать отдельную оценку времени
    на каждый технический вызов внутри одного действия бессмысленно, и
    раньше именно это давало лавину ложных срабатываний (в длинном ходе
    259 "нарушений" из 340 при том, что все реальные шаги были объявлены с
    оценкой). Интент правила — "каждое объявленное действие с оценкой ≤12
    слов" — сохраняется полностью.
    """
    path = Path(transcript_path)
    if not path.exists():
        return [], []

    steps: list[str] = []
    all_text: list[str] = []
    pending_text: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if _is_real_user_turn(entry):
            steps = []
            all_text = []
            pending_text = []
            continue
        if entry.get("type") != "assistant":
            continue

        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        if text:
            all_text.append(text)
            pending_text.append(text)
        if any(b.get("type") == "tool_use" for b in content):
            announced = _announcement_line(pending_text)
            if announced:
                steps.append(announced)
            pending_text = []

    return steps, all_text


def _announcement_line(pending_text: list[str]) -> str:
    """Последняя непустая строка перед вызовом инструмента — она и есть объявление шага.

    Раньше в объявление склеивался ВЕСЬ текст перед вызовом. Если модель сначала
    отвечала пользователю на вопрос обычной прозой (несколько абзацев), а затем
    объявляла следующий шаг с оценкой, лимит в 12 слов считался по всему этому
    тексту — шаг помечался нарушением, хотя объявление было ровно по формату.
    Лимит применяется к самому объявлению, а не к ответу на вопрос над ним.
    """
    lines = [ln.strip() for chunk in pending_text for ln in chunk.splitlines()]
    non_empty = [ln for ln in lines if ln]
    return non_empty[-1] if non_empty else ""


WARN_STATE_PATH = Path.home() / ".cursor" / "hooks" / ".time_estimate_warned.json"


def turn_index(transcript_path: str) -> int:
    """Порядковый номер текущего хода = сколько настоящих user-сообщений уже было.

    Ход — это всё после последнего сообщения человека, поэтому переписанный
    ассистентом ответ остаётся ВНУТРИ того же хода. Номер хода даёт стабильный
    ключ, по которому пошаговый хук помнит, о чём уже предупреждал, и не
    повторяет одно и то же замечание на каждый следующий вызов инструмента.
    """
    path = Path(transcript_path)
    if not path.exists():
        return 0

    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _is_real_user_turn(entry):
            count += 1
    return count


def already_warned(transcript_path: str, kind: str) -> bool:
    """True, если в этом ходе о `kind` уже предупреждали."""
    signature = f"{transcript_path}::{turn_index(transcript_path)}"
    try:
        state = json.loads(WARN_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return state.get("turn") == signature and kind in state.get("kinds", [])


def remember_warning(transcript_path: str, kind: str) -> None:
    signature = f"{transcript_path}::{turn_index(transcript_path)}"
    try:
        state = json.loads(WARN_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("turn") != signature:
        state = {"turn": signature, "kinds": []}
    if kind not in state["kinds"]:
        state["kinds"].append(kind)
    try:
        WARN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WARN_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def mark_turn_start() -> None:
    """Засечь начало хода (вызывает UserPromptSubmit)."""
    import time

    try:
        TIME_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        TURN_START_PATH.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def elapsed_since_turn_start() -> float | None:
    """Сколько реально длился ход, в секундах. None, если засечки нет."""
    import time

    try:
        started = float(TURN_START_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    delta = time.time() - started
    return delta if delta >= 0 else None


def write_pending_feedback(reasons: list[str]) -> None:
    """Отложить замечания до начала следующего хода.

    Именно здесь корень проблемы, из-за которой пользователь несколько раз
    получал один и тот же ответ: Stop-хук раньше отвечал `decision: block`, что
    означает "продолжай ход". Но его замечания — про формат УЖЕ отправленных
    сообщений, исправить их на месте нельзя, и единственное, что оставалось
    модели, — переписать финальный ответ заново. Содержание дублировалось, а
    замечание оставалось в силе. Обратная связь про прошедший ход полезна ровно
    в одной точке — до того, как модель начала писать следующий, — поэтому
    теперь она откладывается сюда, а Stop не блокирует вообще ничего.
    """
    try:
        TIME_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_FEEDBACK_PATH.write_text(
            json.dumps(reasons, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def pop_pending_feedback() -> list[str]:
    """Прочитать и сразу стереть отложенные замечания."""
    try:
        reasons = json.loads(PENDING_FEEDBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    try:
        PENDING_FEEDBACK_PATH.unlink()
    except OSError:
        pass
    return reasons if isinstance(reasons, list) else []


_UNIT_SECONDS = (
    (re.compile(r"^(час|ч)\.?$", re.IGNORECASE), 3600),
    (re.compile(r"^(минут\w*|мин)\.?$", re.IGNORECASE), 60),
    (re.compile(r"^(секунд\w*|сек)\.?$", re.IGNORECASE), 1),
)


def _unit_multiplier(unit: str) -> int:
    for pattern, mult in _UNIT_SECONDS:
        if pattern.match(unit.strip()):
            return mult
    return 60


def whole_task_estimate_seconds(all_text: list[str]) -> int | None:
    """Озвученная оценка задачи целиком, в секундах. None — если её не было.

    Возвращает именно число, а не факт наличия: Stop-хук на его основе сам
    дописывает в журнал пару (озвучено, реально измерено) — раньше эта пара
    заполнялась вручную и по памяти, то есть калибровка настраивалась на
    приблизительные числа, восстановленные задним числом.

    Берётся ПЕРВОЕ совпадение: оценка задачи целиком звучит один раз, до
    первого шага, а дальше в тексте идут оценки отдельных шагов.
    """
    full = " ".join(all_text)
    for m in re.finditer(
        r"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*\d+(?:[.,]\d+)?\s*)?"
        r"(час\.?|ч\.?|минут\w*|мин\.?|секунд\w*|сек\.?)",
        full,
        re.IGNORECASE,
    ):
        window = full[max(0, m.start() - 40):m.end() + 10]
        if WHOLE_TASK_CONTEXT.search(window):
            value = float(m.group(1).replace(",", "."))
            return int(round(value * _unit_multiplier(m.group(2))))
    return None


def has_whole_task_estimate(all_text: list[str]) -> bool:
    """Явная оценка задачи целиком: число+ед.времени в контексте 'задача'/'целиком'/'весь ход'."""
    return whole_task_estimate_seconds(all_text) is not None


# Размер задачи в очках, названный словами: «~5 SP», «13 SP ≈ ~18 минут».
SP_IN_TEXT = re.compile(r"(\d+(?:[.,]\d+)?)\s*SP\b", re.IGNORECASE)
# Вершина шкалы Фибоначчи (см. calibrate.SP_SCALE). Больше — это сумма очков
# ПЛАНА, а не размер одной задачи, и полке она не принадлежит.
SP_TEXT_MAX = 34
# Оценка плана звучит в том же обороте «целиком», что и оценка задачи, но
# описывает десятки задач. Отличаем по слову «план» рядом.
PLAN_CONTEXT = re.compile(r"план\w*", re.IGNORECASE)


def whole_task_sp(all_text: list[str]) -> float | None:
    """Размер задачи целиком в очках, названный в тексте хода. None — если не назван.

    Запасной путь для Stop-хука. Основной — `state["pending"]`, который кладёт
    `calibrate.py estimate --sp`. Но размер часто произносится словами без запуска
    команды, и такая пара «оценка/факт» уходила в журнал без размера: сравнивать
    длительность не с чем, полку она не пополняет. Так накопился 61 замер из 99,
    не пригодившийся калибровке ни разу.

    Берётся ПЕРВОЕ совпадение — оценка задачи целиком звучит один раз, до первого
    шага. Суммы очков плана и упоминания рядом со словом «план» отбрасываются:
    ход, где названа оценка плана на 45 SP, а потрачено три минуты, попал бы в
    историю как «задача на 45 SP заняла 3 минуты» и испортил верх шкалы.
    """
    full = " ".join(all_text)
    for m in SP_IN_TEXT.finditer(full):
        value = float(m.group(1).replace(",", "."))
        if value <= 0 or value > SP_TEXT_MAX:
            continue
        window = full[max(0, m.start() - 60):m.end() + 10]
        if PLAN_CONTEXT.search(window):
            continue
        if WHOLE_TASK_CONTEXT.search(window):
            return value
    return None


def has_workflow_duration_estimate(all_text: list[str]) -> bool:
    """Отдельная оценка ожидаемого времени работы ФОНОВОГО Workflow (не твоей же работы)."""
    full = " ".join(all_text)
    for m in re.finditer(r"\d+\s*(секунд|сек\.?|минут|мин\.?|час\.?|часов)", full, re.IGNORECASE):
        window = full[max(0, m.start() - 40):m.end() + 15]
        if WORKFLOW_CONTEXT.search(window):
            return True
    return False


def turn_used_workflow_tool(transcript_path: str) -> bool:
    """True, если в текущем ходе (с последнего настоящего user-сообщения) был вызов Workflow."""
    path = Path(transcript_path)
    if not path.exists():
        return False

    used = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if _is_real_user_turn(entry):
            used = False
            continue
        if entry.get("type") != "assistant":
            continue

        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if any(b.get("type") == "tool_use" and b.get("name") == "Workflow" for b in content):
            used = True

    return used


def _word_count(text: str) -> int:
    """Слова без пунктуационных токенов.

    `str.split()` считал отдельно стоящее тире полноценным словом — а сам
    документированный формат объявления ("X — ~N мин") тире и содержит, причём
    обычно два: одно как разделитель, одно перед оценкой. Два слова из двенадцати
    уходили на пунктуацию, и объявления ровно по формату помечались нарушением.
    """
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


# --- вывод объявленной оценки -------------------------------------------------
#
# Проверять число как таковое нельзя: замер 15.09.2026 показал, что промахи —
# трёх-шестикратные, но в абсолюте это «1 мин» вместо 18 секунд. Любое такое
# число достижимо какой-нибудь суммой эталонов, поэтому полоса правдоподобия
# ловила 1 промах из 23. Проверяемым делается ВЫВОД: объявление называет, из
# каких действий шаг состоит и сколько в каждом вызовов, а проверка пересчитывает.

STEP_REFS_DIR = Path.home() / ".cursor" / "references" / "steps"

# Короткие имена видов: человек пишет «чтение», а не «чтение файлов». Слева —
# то, что реально набирается в строке объявления; справа — `kind` эталона.
BASIS_ALIASES = {
    "чтение": "чтение файлов",
    "поиск": "поиск по коду",
    "правки": "код: правки (1 файл)",
    "правки2": "код: правки (2+ файла)",
    "новый": "код: новый файл",
    "код": "код: новое и правки",
    "тесты": "тесты: целевые",
    "сьют": "тесты: весь сьют",
    "фронттесты": "тесты: фронт",
    "сборка": "сборка фронта",
    "линт": "линт и типы",
    "браузер": "ручная проверка в браузере",
    "http": "ручная проверка по HTTP",
    "миграции": "бд: миграции",
    "бд": "бд: запросы",
    "стенд": "стенд: поднять",
    "деплой": "деплой",
    "гит": "git: состояние",
    "коммит": "git: коммит",
    "пуш": "git: пуш",
    "команда": "команда прочая",
    "веб": "веб-разведка",
}

# `(чтение×3, тесты x2)` — состав шага в скобках в конце строки.
BASIS_BLOCK = re.compile(r"\(([^()]*[×x]\s*\d+[^()]*)\)\s*\.?\s*\*{0,2}\s*$")
BASIS_ITEM = re.compile(r"([А-Яа-яЁёA-Za-z][\w-]*)\s*[×x]\s*(\d+)")

# Промах, который стоит замечания. Меньше — шум: разброс внутри вида сам по себе
# около двух раз (σ(log) ≈ 0.6), и ловить полуторакратное значит бранить за
# погоду. Ловим то, ради чего всё делалось, — промах в разы.
BASIS_TOLERANCE = 2.2


def parse_basis(step_text: str) -> list[tuple[str | None, int]]:
    """Состав шага из объявления: [(вид эталона или None, вызовов)].

    None вместо вида — действие, которому эталона нет. Отличать его от «вывод
    не назван» обязательно: первое оценку не запрещает, второе означает, что
    проверять нечего.
    """
    m = BASIS_BLOCK.search(step_text.strip())
    if not m:
        return []
    out = []
    for name, calls in BASIS_ITEM.findall(m.group(1)):
        out.append((BASIS_ALIASES.get(name.lower()), int(calls)))
    return out


STEPS_JOURNAL = Path.home() / ".cursor" / "time-analysis" / "records" / "steps.jsonl"
_BLENDED: dict | None = None


def blended_step_refs() -> dict:
    """Эталоны шагов из двух последних версий.

    Считается один раз на процесс: хук живёт один вызов, а журнал на 10 тысяч
    строк читать на каждый вид заново незачем. Пусто — версий или журнала нет,
    и тогда работают файлы эталонов как есть.
    """
    global _BLENDED
    if _BLENDED is not None:
        return _BLENDED
    _BLENDED = {}
    try:
        import sys as _sys
        ta = str(Path.home() / ".cursor" / "time-analysis")
        if ta not in _sys.path:
            _sys.path.insert(0, ta)
        import versions
        _BLENDED = versions.step_references(
            versions.read_journal(STEPS_JOURNAL), versions.load())
    except Exception:
        _BLENDED = {}
    return _BLENDED


def _step_refs() -> dict[str, tuple[float, float]]:
    """{kind: (секунд на вызов, наклон)} из шапок references/steps/STEP-*.md.

    Секунды на вызов масштабируются под смешанный эталон версий: шапка снята
    по корпусу транскриптов без различия версий, и без масштаба проверка
    объявления спорила бы с числами, которые сессия видит в строке эталонов.
    """
    blended = (blended_step_refs().get("kinds") or {})
    refs = {}
    try:
        paths = sorted(STEP_REFS_DIR.glob("STEP-*.md"))
    except OSError:
        return refs
    for path in paths:
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        kind = re.search(r"^kind: (.+)$", head, re.M)
        per_call = re.search(r"^per_call_sec: ([\d.]+)$", head, re.M)
        slope = re.search(r"^slope: ([\d.]+)$", head, re.M)
        median = re.search(r"^median_sec: ([\d.]+)$", head, re.M)
        if kind and per_call and slope:
            name = kind.group(1).strip()
            scale = 1.0
            b = blended.get(name)
            if b and median and float(median.group(1)) > 0:
                scale = b["minutes"] * 60 / float(median.group(1))
            refs[name] = (float(per_call.group(1)) * scale,
                          float(slope.group(1)))
    return refs


def expected_seconds(basis: list[tuple[str | None, int]]) -> float | None:
    """Сколько шаг должен занять по эталонам. None — посчитать нельзя.

    Время вида = `per_call_sec × вызовов^slope`; шаг из нескольких действий —
    сумма. Сумма законна и должна проходить: большое число получается именно
    так, а не выдумывается.
    """
    if not basis:
        return None
    refs = _step_refs()
    if not refs:
        return None
    total = 0.0
    for kind, calls in basis:
        if kind is None or kind not in refs:
            return None
        per_call, slope = refs[kind]
        total += per_call * (max(calls, 1) ** slope)
    return total


def declared_seconds(step_text: str) -> float | None:
    """Объявленное время в секундах."""
    m = re.search(r"~?\s*([\d.,]+)\s*(сек|секунд|мин|минут)", step_text, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value if m.group(2).lower().startswith("сек") else value * 60


def step_verdict(step_text: str) -> str | None:
    """Замечание к объявлению шага. None — всё в порядке.

    Порядок проверок: сначала форма (длина, наличие числа), потом вывод. Своя
    поломка — отсутствие эталонов, нечитаемая шапка — означает «пропустить»:
    неоценённый шаг лучше остановленной работы.
    """
    # Состав считается выводом, а не пояснением, и в лимит слов не входит:
    # иначе формат ломает сам себя — назвал, из чего время, и вышел за предел.
    без_состава = BASIS_BLOCK.sub("", step_text.strip())
    if _word_count(без_состава) > MAX_WORDS:
        return (f"объявление длиннее {MAX_WORDS} слов — действие называется "
                f"сразу, без пояснений")
    if not ESTIMATE_PATTERN.search(step_text):
        return "в объявлении нет времени — назови его числом с единицей"

    basis = parse_basis(step_text)
    if not basis:
        return ("не сказано, из чего время: допиши состав в скобках, например "
                "«(чтение×3)» — иначе число непроверяемо")

    expected = expected_seconds(basis)
    declared = declared_seconds(step_text)
    if expected is None or declared is None or expected <= 0:
        return None

    ratio = declared / expected
    if ratio > BASIS_TOLERANCE or ratio < 1 / BASIS_TOLERANCE:
        return (f"названный состав даёт по эталонам {_human(expected)}, "
                f"а объявлено {_human(declared)} — расхождение в "
                f"{max(ratio, 1 / ratio):.1f} раза")
    return None


# Во сколько раз объём шага должен разойтись с задуманным, чтобы об этом стоило
# говорить. Шаг редко совпадает вызов в вызов: добрать grep, дочитать файл —
# нормальное продолжение объявленного действия. Двойной перебор — уже не
# уточнение, а другой шаг.
SCOPE_TOLERANCE = 2.0


def scope_verdict(step_text: str, actual_calls: int) -> str | None:
    """Разошёлся ли объём шага с задуманным. None — нет или сверять нечего.

    Это НЕ про время. Время сверяет step_verdict по эталонам; здесь сравнивается
    задуманное число действий со сделанным, и лечится расхождение дроблением
    шага, а не другим числом в объявлении.
    """
    basis = parse_basis(step_text)
    if not basis or not actual_calls:
        return None
    planned = sum(calls for _, calls in basis)
    if planned <= 0:
        return None
    ratio = actual_calls / planned
    if ratio < SCOPE_TOLERANCE:
        return None
    return (f"объявлено действий: {planned}, сделано вызовов: {actual_calls} — "
            f"промахнулся объём шага, а не оценка; такой шаг стоит объявлять "
            f"частями")


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"~{int(round(seconds))} сек"
    return f"~{seconds / 60:.1f} мин".replace(".0 ", " ")


def step_is_bad(step_text: str) -> bool:
    return step_verdict(step_text) is not None


def journal_is_fresh(fresh_window_seconds: int) -> bool:
    import time

    mtime = journal_mtime()
    return mtime > 0 and time.time() - mtime <= fresh_window_seconds


def open_task_steps_empty() -> bool:
    """True, если последняя (ещё не закрытая) задача журнала не получила ни одного шага."""
    entries = journal_read_state().get("history") or []
    if not entries:
        return False
    last = entries[-1]
    if "actual_seconds" in last:
        return False
    return len(last.get("steps") or []) == 0

"""
UserPromptSubmit-хук (глобальный). Единственная точка, где "начало хода" —
реальный момент, а не догадка. Делает три вещи:

1. Засекает время начала хода — Stop-хук по нему считает фактическую
   длительность и сам пишет её в журнал. До этого actual_seconds заполнялись
   вручную и по памяти, то есть калибровка подстраивалась под приблизительные
   числа, восстановленные задним числом.
2. Показывает отложенные замечания по ПРОШЕДШЕМУ ходу (их складывает Stop-хук).
   Это правильное место для обратной связи задним числом: ход ещё не начался,
   поведение можно изменить, и ничего переписывать не надо.
3. Печатает короткую памятку про формат оценок.

Не блокирует: UserPromptSubmit не может знать заранее, тривиальна ли задача.
"""

import json
import re
from pathlib import Path
from statistics import median

from _time_estimate_common import (blended_step_refs, mark_turn_start,
                                    pop_pending_feedback)

REMINDER = (
    "[progress-time-estimates] Если задача нетривиальна (>1 шага): разбить на "
    "задачи, поставить каждой сторипоинты сравнением с эталонами и озвучить ДО "
    "первого шага минуты И запас отдельно "
    "(`calibrate.py estimate --sp <очки> --compared-to US-NNNN`; без эталона "
    "оценка не даётся вовсе, а размер на глаз промахивается вчетверо — "
    "у каждой задачи назовите ось, эталон той же оси и число мест). Затем короткая "
    "строка с оценкой перед каждым шагом, без вводных слов "
    "'Дальше'/'Теперь'/'Сейчас'. Тесты — по риску: что правка могла сломать, то и "
    "проверить; после правки — ничего; весь сьют — один раз в конце плана; тест "
    "дольше 2 минут — только при критическом регрессе, по одному. Журнал "
    "заполняется автоматически."
)


# Эталоны подставляются в памятку числами, а не ссылкой на файл.
# Причина: памятка велела «на глаз не оценивать», но самих медиан не давала —
# за ними надо было идти в records/steps.jsonl, и на практике вместо этого
# называлась прикидка. Число, которое уже перед глазами, придумывать незачем.
#
# Считаем по журналу замеров, а не по файлам references/steps: те правятся
# руками и успели отстать — на 15.09.2026 пять видов из 22 расходились с
# журналом больше чем в полтора раза, «линт и типы» втрое. Журнал пополняется
# сам, поэтому устареть не может. Файлы эталонов остаются запасным путём.
JOURNAL_PATH = Path.home() / "\.cursor" / "time-analysis" / "records" / "steps.jsonl"
REFS_DIR = Path.home() / "\.cursor" / "references" / "steps"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)

# Ниже этого числа замеров медиана — шум, а не эталон: два случая «веб-разведки»
# давали 0.3 мин против 0.7 по журналу.
MIN_SAMPLES = 5


def _tag(ref: dict) -> str:
    """Откуда число: без пометки надёжное не отличить от ненадёжного."""
    if ref["source"] == "новое":
        return "нов."
    if ref["source"] == "смешано":
        return f"нов. {round(ref['share_new'] * 100)}%"
    if ref["source"] == "старое с поправкой":
        return "стар.×к"
    return "стар."


def _from_journal() -> list[tuple[str, float, str]]:
    """Эталоны из двух последних версий (план 086) с пометкой источника."""
    out = blended_step_refs()
    return [
        (kind, ref["minutes"], _tag(ref))
        for kind, ref in (out.get("kinds") or {}).items()
        if ref["n_new"] + ref["n_old"] >= MIN_SAMPLES
    ]


def _from_reference_files() -> list[tuple[str, float, str]]:
    pairs: list[tuple[str, float, str]] = []
    try:
        paths = sorted(REFS_DIR.glob("STEP-*.md"))
    except OSError:
        return []
    for path in paths:
        try:
            head = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not head:
            continue
        kind, seconds = "", 0.0
        for line in head.group(1).splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "kind":
                kind = value
            elif key == "median_sec":
                try:
                    seconds = float(value)
                except ValueError:
                    pass
        if kind and seconds:
            pairs.append((kind, seconds / 60, "файл"))
    return pairs


def reference_line() -> str:
    """Медианы эталонных шагов одной строкой. Пусто, если считать не из чего."""
    pairs = _from_journal() or _from_reference_files()
    if not pairs:
        return ""
    pairs.sort(key=lambda pair: pair[1])
    listed = ", ".join(f"{kind} {minutes:.1f} ({tag})" for kind, minutes, tag in pairs)
    out = blended_step_refs()
    coef = out.get("coefficient")
    head = (f"версия {out['current']}; в скобках источник: нов. — только новая "
            "версия, нов. N% — доля новой в смеси, "
            + (f"стар.×к — старое с коэффициентом версии {coef}"
               if coef else "стар. — старое как есть, коэффициента версии нет")
            if out.get("current") else "медианы файлов эталонов")
    return (
        f"[progress-time-estimates] Эталоны шага, минуты ({head}): "
        + listed
        + ". Оценка шага — сумма эталонов тех действий, из которых он состоит; "
        "между двумя вариантами брать 80-й процентиль, а не удваивать."
    )

def main() -> None:
    mark_turn_start()

    feedback = pop_pending_feedback()
    if feedback:
        print(
            "[progress-time-estimates] По прошлому ходу: "
            + "; ".join(feedback)
            + ". Учти это здесь, переписывать прошлый ответ не нужно."
        )

    print(REMINDER)

    # Памятка не должна падать из-за журнала: UserPromptSubmit обязан
    # вернуть 0, иначе ход не начнётся вовсе.
    try:
        refs = reference_line()
    except Exception:
        refs = ""
    if refs:
        print(refs)

    try:
        drift = version_drift_prompt()
    except Exception:
        drift = ""
    if drift:
        print(drift)


def version_drift_prompt() -> str:
    """Вопрос о новой версии эталонов при систематическом расхождении (план
    086). Хук только сообщает, что серия набрана; спрашивает сессия карточкой,
    а версию заводит ответ владельца."""
    import sys
    ta = str(Path.home() / "\.cursor" / "time-analysis")
    if ta not in sys.path:
        sys.path.insert(0, ta)
    import calibrate
    return calibrate.drift_prompt(calibrate.drift_report())


if __name__ == "__main__":
    main()

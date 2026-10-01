#!/usr/bin/env python3
"""Проверка формата плана: последовательность UserStory.

    py lint_plan.py <файл>...      проверить конкретные файлы
    py lint_plan.py --all          проверить все планы
    py lint_plan.py --hook         режим хука: путь берётся из stdin (PostToolUse)

Коды возврата: 0 — чисто или только замечания; 2 — есть нарушения структуры.

Зачем это вообще. План раньше был списком шагов, которые вне контекста беседы
превращались в чушь: «2–4. Контент разделов» не говорит ни о чём, если ты не
участвовал в её создании. Теперь план — последовательность самодостаточных
историй, и структура у них жёсткая, иначе самодостаточность не удержать.

Что строго (блокирует), а что нет — решение пользователя: структуру держим
жёстко, смысловые эвристики пока только предупреждают. Эвристика ошибается на
законном тексте, и блокировать на ровном месте хуже, чем пропустить.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

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
    return [PLANS_DIR, *extra_plan_dirs()]

# В Cursor нумерация планов начата заново 01.10.2026: с 001 это уже истории,
# а не списки шагов. Порог 022 — история каталога Claude, здесь он глушил бы
# хук на всех живых планах.
FIRST_STORY_PLAN = 1

# С этого плана размер обязан быть назван сравнением с эталонной историей.
# Граница нужна, потому что правило смотрит вперёд: в планах 022-035 размеры
# ставились на глаз, и задним числом их не переставляют — переставленный по
# факту размер это пересказ факта, сравнивать его не с чем. Ровно так планы 030
# и 034 дали факт/полка 0.19 и 0.27, а фонд за пять планов не пополнился ни
# одной историей.
FIRST_SIZED_PLAN = 36

# Каталог эталонных историй: по нему проверяется, что названный эталон
# существует. Нет его — сравнение слабое, и это говорится, а не бракуется:
# фонд неполон, осей в нём одиннадцать, и запрещать оценку по отсутствующей оси
# значит запрещать оценку вовсе.
FUND_DIR = Path.home() / ".cursor" / "references" / "stories"

# `## US-0042. Название — 5 SP`
STORY_RE = re.compile(
    r"^##\s+US-(\d{4})\.\s+(.+?)\s*(?:—\s*([\d.]+|\?)\s*SP\s*)?$", re.M)
SECTIONS = ("Боль", "Порядок и зависимости", "Критерии приёмки",
            "Порядок демонстрации")
# «Размер» обязателен не везде (см. FIRST_SIZED_PLAN), но границей секции
# служит всегда: иначе обоснование размера прилипает к боли и рубит проверку
# длины боли на ровном месте.
SECTION_BOUNDARIES = SECTIONS + ("Размер",)
REF_RE = re.compile(r"US-(\d{4})")
# Ссылка на эталонную историю фонда — не зависимость от другого плана, а
# обоснование размера: фонд лежит рядом и доступен всегда. Отличается она
# путём, и только в таком виде разрешена — голый номер по-прежнему значит
# «история этого плана».
REF_STORY_RE = re.compile(r"references/stories/US-(\d{4})")
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
# Раздел верхнего уровня после историй: журнал и всё прочее, что телом
# истории не является. `### Особенности реализации` внутри истории сюда
# не попадает — у него три решётки.
TOP_SECTION_RE = re.compile(r"^## (?!US-)", re.M)

# Эвристики (не блокируют)
CODEISH = re.compile(r"```|^\s{4}\S", re.M)
TESTISH = re.compile(r"\b(pytest|unittest|assert|юнит-тест|покрыт[оы] тест)", re.I)
ACTOR = re.compile(r"\bкак\s+\S", re.I)
WEAK_ACTOR = re.compile(r"как\s+(пользовател|систем|клиент приложени)", re.I)

MIN_PAIN_WORDS = 15
# Порог «секция не отписка» — свой у каждой. У зависимостей законный ответ
# бывает в одно слово («Независима»), и общий порог рубил бы его как пустоту.
MIN_WORDS = {
    "Боль": 5,
    "Порядок и зависимости": 1,
    "Критерии приёмки": 5,
    "Порядок демонстрации": 5,
}


class Report:
    def __init__(self, path: Path):
        self.path = path
        self.errors: list[str] = []
        self.warns: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def ok(self) -> bool:
        return not self.errors


def split_stories(text: str) -> list[tuple[int, str, str, str | None, str]]:
    """-> [(строка, номер, название, sp, тело)]

    Тело последней истории обрывается на ближайшем разделе верхнего
    уровня — журнале или любом другом `## `. Без этого журнал становился
    её телом, и законные упоминания историй в записях («**Дальше:**
    US-0064») бракова́лись как ссылки на чужой план. Всплыло это на
    разрезе плана 026: журнал остался у одной части, а половина номеров
    уехала в другую.
    """
    out = []
    ms = list(STORY_RE.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end]
        tail = TOP_SECTION_RE.search(body)
        if tail:
            body = body[: tail.start()]
        line = text[: m.start()].count(chr(10)) + 1
        out.append((line, m.group(1), m.group(2).strip(), m.group(3), body))
    return out


def section_body(body: str, name: str) -> str | None:
    """Текст секции `**Имя.**` до следующей секции/заголовка."""
    m = re.search(r"\*\*" + re.escape(name) + r"\.?\*\*", body)
    if not m:
        return None
    rest = body[m.end():]
    nxt = re.search(r"\n\s*(\*\*(?:"
                    + "|".join(re.escape(s) for s in SECTION_BOUNDARIES)
                    + r")\.?\*\*|#{2,4}\s|\n---\s*\n)", rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


# --- направление работы в имени плана (US-0281) ------------------------------
#
# По списку файлов должно быть видно, чем каждый план занимается, не открывая
# его. Направление задаётся смыслом, а не репозиторием: подключение портала к
# платформе — работа по платформе, хотя правки лежат в портале.
#
# Имя устроено как `NNN-<направление>-<слаг>.md`. Номер остаётся ведущей частью:
# им план называют в разговоре, им же он записан в журнале фактов, и по нему же
# адресуются владение, пауза и маркер сессии.
PLAN_NAME_RE = re.compile(r"^(\d{3})-([a-z][a-z0-9]*)-(.+)\.md$")

# С этого номера план ОБЯЗАН нести направление: правило заведено планом 039, и
# требовать его от написанного раньше значит блокировать правку задним числом.
FIRST_EPIC_PLAN = 39

# А с этого — направление только просится. Планы 021-038 написаны на историях и
# направление получат при разборе задним числом (US-0289); до тех пор замечание,
# а не ошибка. Валидатор стоит на PostToolUse и блокирует запись: цена ошибки
# здесь не «неверное замечание», а невозможность дописать журнал старого плана.
# Планы 001-020 написаны списком шагов и переименованию не подлежат вовсе.
EPIC_WANTED_FROM = 21

# Живым считается план, работа по которому идёт или ещё пойдёт. Именно у живых
# номер обязан быть уникальным: пауза и владение адресуются голым номером, и два
# живых плана с одним номером означают, что пауза на одном глушит другой.
LIVE_STATUSES = {"draft", "in-progress", "paused"}


# Состояние плана хранится в ОДНОМ месте — в поле `status` его шапки. Журнал
# и заметки состояние описывают, но не задают: два источника истины расходятся
# всегда, и расходятся молча. Проверки ниже ловят расхождение машиной, а не
# глазами — именно глазами его и не заметили 24.09, когда стартовое сообщение
# назвало незакрытыми два плана, у одного из которых журнал говорил «пройден
# целиком», а у второго рядом лежала заметка о паузе.


def paused_marker(number: int, plans_dir: Path | None = None) -> bool:
    """Лежит ли рядом с планом маркер паузы."""
    root = (plans_dir or PLANS_DIR) / "notes"
    return (root / f"{number:03d}-paused").exists()


def check_status(plan: dict, paused: set[int] | None = None,
                 working: set[int] | None = None) -> list[str]:
    """Расхождения между статусом плана и тем, что о нём говорят соседи."""
    out: list[str] = []
    number, status = plan["number"], plan.get("status")
    if paused is not None and number in paused and status != "paused":
        stale = status not in LIVE_STATUSES
        out.append(
            f"{plan['name']}: рядом лежит маркер паузы, а `status: {status}` — "
            + ("план закрыт, значит маркер забыли убрать"
               if stale else
               "заметка состояние описывает, но не задаёт; поправить надо статус"))
    # `in-progress` означает, что план прямо сейчас кто-то ведёт. Ни одной
    # открытой истории — значит не ведёт, и статус просто забыли переставить.
    if working is not None and status == "in-progress" and number not in working:
        out.append(
            f"{plan['name']}: `status: in-progress`, но ни одной открытой истории "
            f"в журнале нет — план либо закрыт, либо брошен")
    return out


def check_story_clash(plans: list[dict]) -> list[str]:
    """Номера историй, занятые дважды среди ЖИВЫХ планов.

    У закрытого плана номер уже ни на что не адресуется, а у живого ссылка
    `US-NNNN` обязана вести в одно место: иначе непонятно, какую историю
    имели в виду — и в журнале фактов тоже.
    """
    out: list[str] = []
    seen: dict[str, list[str]] = {}
    for plan in plans:
        if plan.get("status") not in LIVE_STATUSES:
            continue
        for us in plan.get("stories") or []:
            seen.setdefault(us, []).append(plan["name"])
    for us, names in sorted(seen.items()):
        if len(names) > 1:
            where = ", ".join(sorted(set(names))) if len(set(names)) > 1 else names[0]
            out.append(f"номер {us} занят дважды среди живых планов: {where} — "
                       f"ссылка на него двусмысленна")
    return out


def check_plan_name(path: Path, epic: str | None) -> str | None:
    """Ошибка в имени файла плана или None, если имя в порядке."""
    name = path.name
    m = re.match(r"^(\d{3})-", name)
    if not m:
        return (f"{name}: номер должен быть ведущей частью имени — "
                f"`NNN-<направление>-<слаг>.md`")
    if int(m.group(1)) < FIRST_EPIC_PLAN:
        return None

    full = PLAN_NAME_RE.match(name)
    if not full:
        return (f"{name}: в имени нет направления — ожидается "
                f"`NNN-<направление>-<слаг>.md`")
    if epic and full.group(2) != epic:
        return (f"{name}: направление в имени ({full.group(2)}) не совпадает "
                f"с полем `epic: {epic}` в шапке")
    return None


def check_number_clash(plans: list[dict]) -> list[str]:
    """Повторы номеров. Пусто — повторов нет либо они законны.

    Повтор законен только между ЗАКРЫТЫМИ планами разных направлений: номер
    там фиксирует момент в хронологии, а не файл. Внутри одного направления
    повтор — ошибка всегда, и правится он до начала работы.
    """
    out: list[str] = []
    by_number: dict[int, list[dict]] = {}
    for p in plans:
        # Планы 001-020 направления не имеют и не получат: у всех `epic` пуст,
        # поэтому любой их повтор читается как «в одном направлении» по
        # построению. Ложное срабатывание при каждом прогоне научит его
        # пролистывать, а вместе с ним и настоящие.
        if p["number"] < EPIC_WANTED_FROM:
            continue
        by_number.setdefault(p["number"], []).append(p)
    for number, group in sorted(by_number.items()):
        if len(group) < 2:
            continue
        names = ", ".join(sorted(p["name"] for p in group))
        epics = [p.get("epic") for p in group]
        if len(set(epics)) < len(epics):
            out.append(f"номер {number:03d} повторён в одном направлении: "
                       f"{names} — это ошибка, правится до начала работы")
            continue
        live = [p for p in group if p.get("status") in LIVE_STATUSES]
        if len(live) > 1:
            out.append(f"номер {number:03d} у двух живых планов: {names} — "
                       f"владение и пауза адресуются голым номером, и пауза "
                       f"на одном заглушит другой")
    return out


def fund_has(us: str) -> bool:
    return (FUND_DIR / f"US-{us}.md").exists()


# Ось, названная в секции «Размер», и число мест. Проверяются вместе с
# эталоном: одной ссылки мало. На 22.09.2026 истории, где сошлись все три,
# дали медиану факт/полка 1.02, а истории с одной только ссылкой — 0.28.
# То же выражение под именем, которым его зовёт журнал калибровки.
# Второй компиляции здесь быть не должно: ровно из двух копий одного
# правила и вышло расхождение, которое чинится этим коммитом.
FUND_REF_RE = REF_STORY_RE
AXIS_RE = re.compile("[Оо]сь[ ]+([A-K])(?![A-Za-z])")
PLACES_RE = re.compile(
    "[Мм]ест[а-яё]*[^.]{0,24}?(одно|двое|трое|четверо|пятеро|один|два|три|четыре|пять|шесть|семь|восемь|[0-9]+|больше|меньше|столько[ ]+же)")


def fund_axis(us: str) -> str | None:
    """Ось эталона из его шапки."""
    path = FUND_DIR / f"US-{us}.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines()[:20]:
        if line.startswith("axis:"):
            return line.split(":", 1)[1].strip() or None
    return None


def check_sizing(r: Report, tag: str, sp: str | None,
                 fund_refs: set[str], number: int | None,
                 size_text: str = "") -> None:
    """Размер должен быть назван сравнением, а не на глаз.

    Проверяется не сам размер — его правильность машине недоступна, — а то, что
    названо, с чем сравнивали. Без этого размер непроверяем и непередаваем:
    из «5 SP» не следует ни почему пять, ни что делать, когда следующая история
    похожа.

    `? SP` освобождён намеренно: это законный ответ, означающий, что сравнение
    не сработало и размер будет посчитан при декомпозиции.
    """
    if number is None or number < FIRST_SIZED_PLAN:
        return
    if sp is None or sp == "?":
        return
    if not fund_refs:
        r.err(f"{tag}: размер {sp} SP назван без сравнения — в «**Размер.**» "
              f"должен стоять эталон вида `references/stories/US-NNNN.md`, "
              f"иначе число непроверяемо")
        return
    missing = sorted(us for us in fund_refs if not fund_has(us))
    if missing:
        r.warn(f"{tag}: эталона {', '.join('US-' + m for m in missing)} в фонде "
               f"нет — сравнение слабое, и это стоит сказать вслух")

    # Ось и места — остальные две трети сравнения. Ссылка без них означает,
    # что эталон назван, а размер взят на глаз: именно так шкала и разошлась
    # с фактом при заполненном `compared_to`.
    known = {us for us in fund_refs if fund_has(us)}
    if not known or not size_text:
        return
    named = {m.upper() for m in AXIS_RE.findall(size_text)}
    axes = {fund_axis(us) for us in known}
    # Замечание, а не ошибка: планы, написанные до 22.09.2026, этой проверки
    # не знали, и валить их блокирующей ошибкой значит заставить переписывать
    # готовое. Данные при этом защищены строго — `sizing_basis` такой размер
    # в полку не пустит.
    if not named:
        r.warn(f"{tag}: в «**Размер.**» не названа ось — эталон задаёт уровень, "
              f"а ось говорит, какая это работа (список: "
              f"references/stories/AXES.md)")
    elif not named & axes:
        r.warn(f"{tag}: ось {'/'.join(sorted(named))} не совпадает с осью "
              f"эталона ({'/'.join(sorted(a for a in axes if a))}) — "
              f"сравнение с эталоном чужой оси меряет другую работу")
    if not PLACES_RE.search(size_text):
        r.warn(f"{tag}: в «**Размер.**» не посчитаны места — эталон задаёт "
              f"уровень, места дают сдвиг от него («мест два», «мест выходит "
              f"четыре», «мест больше»)")


def lint_text(text: str, path: Path) -> Report:
    r = Report(path)

    fm = FM_RE.match(text)
    if not fm:
        r.err("нет frontmatter")
    else:
        head = fm.group(1)
        if not re.search(r"^project:\s*\S", head, re.M):
            r.err("во frontmatter нет `project:`")
        if not re.search(r"^status:\s*\S", head, re.M):
            r.err("во frontmatter нет `status:`")
        # Направление — то же самое, что `epic`. Слово «эпик» не используется в
        # тексте историй намеренно, чтобы не смешивать с чужими трактовками,
        # но поле называется так.
        epic_m = re.search(r"^epic:\s*(\S+)", head, re.M)
        epic = epic_m.group(1) if epic_m else None
        number = plan_number(path)
        if number is not None and not epic:
            say = (r.err if number >= FIRST_EPIC_PLAN
                   else (r.warn if number >= EPIC_WANTED_FROM else None))
            if say:
                say("во frontmatter нет `epic:` — по списку планов направление "
                    "должно читаться, не открывая файл")
        name_problem = check_plan_name(path, epic)
        if name_problem:
            r.err(name_problem)

    stories = split_stories(text)
    if not stories:
        r.err("нет ни одной истории `## US-NNNN. Название — N SP`")
        return r

    seen: dict[str, int] = {}
    numbers = {num for _, num, _, _, _ in stories}

    for line, num, title, sp, body in stories:
        tag = f"US-{num}"
        if num in seen:
            r.err(f"строка {line}: {tag} — номер уже занят "
                  f"(строка {seen[num]})")
        seen[num] = line

        if not title:
            r.err(f"строка {line}: {tag} без названия")
        if sp is None:
            r.err(f"строка {line}: {tag} без оценки SP в заголовке "
                  f"(если оценить пока нечем — поставить `? SP`)")
        elif sp == "?":
            # Явное «пока не умеем оценить». Это НЕ промах оценки: пока
            # эталонных историй мало, честнее сказать «не знаю» и посчитать
            # размер при декомпозиции, чем выдумать число и потом объяснять
            # расхождение, которого не было.
            r.warn(f"{tag}: размер не проставлен (`? SP`) — оценить при "
                   f"декомпозиции, до начала работы")
        else:
            try:
                val = float(sp)
            except ValueError:
                r.err(f"строка {line}: {tag} — SP не число: {sp!r}")
                val = 0
            if val > 21:
                r.err(f"строка {line}: {tag} — {sp} SP, дороже 21 делим всегда")
            elif val in (13.0, 21.0):
                r.warn(f"{tag}: {sp} SP — подумать, дробится ли до 8–13 без "
                       f"искажения истории")

        for name in SECTIONS:
            sec = section_body(body, name)
            if sec is None:
                r.err(f"строка {line}: {tag} — нет секции «{name}»")
            elif len(sec.split()) < MIN_WORDS[name]:
                r.err(f"строка {line}: {tag} — секция «{name}» пустая "
                      f"или в одну строку")

        pain = section_body(body, "Боль") or ""
        if pain:
            if not ACTOR.search(pain):
                r.warn(f"{tag}: в боли не видно актора — «я как <кто>»")
            elif WEAK_ACTOR.search(pain):
                r.warn(f"{tag}: актор общий («пользователь», «система») — "
                       f"нужен конкретный")
            if len(pain.split()) < MIN_PAIN_WORDS:
                r.warn(f"{tag}: боль короче {MIN_PAIN_WORDS} слов — вряд ли "
                       f"понятна без контекста беседы")

        crit = section_body(body, "Критерии приёмки") or ""
        if TESTISH.search(crit):
            r.warn(f"{tag}: в критериях приёмки речь о тестах — это аналитика, "
                   f"а не тесты")

        contract = "\n".join(
            section_body(body, n) or "" for n in SECTIONS)
        if CODEISH.search(contract):
            r.warn(f"{tag}: в тексте истории есть код — подробности реализации "
                   f"выносятся в «Особенности реализации»")

        fund = set(REF_STORY_RE.findall(body))
        check_sizing(r, tag, sp, fund, plan_number(path),
                     section_body(body, "Размер") or "")
        for ref in REF_RE.findall(body):
            if ref != num and ref not in numbers and ref not in fund:
                r.err(f"строка {line}: {tag} ссылается на US-{ref}, которой "
                      f"нет в этом плане — истории связываются только внутри "
                      f"плана, планы между собой через `depends_on`")

    return r


def lint_file(path: Path) -> Report:
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError as e:
        r = Report(path)
        r.err(f"не читается: {e}")
        return r
    return lint_text(text, path)


def plan_number(path: Path) -> int | None:
    m = re.match(r"^(\d{3})-", path.name)
    return int(m.group(1)) if m else None


def is_plan(path: Path) -> bool:
    """План нового формата — только его и проверяем."""
    try:
        p = path.resolve()
    except OSError:
        return False
    if p.parent not in {d.resolve() for d in plan_dirs()} or p.suffix != ".md":
        return False
    num = plan_number(p)
    return num is not None and num >= FIRST_STORY_PLAN


def collect_plans() -> list[dict]:
    """Все планы с номером, направлением и статусом — для проверки повторов."""
    out = []
    for path in sorted(m for d in plan_dirs() for m in d.glob("[0-9][0-9][0-9]-*.md")):
        number = plan_number(path)
        if number is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = FM_RE.match(text)
        head = m.group(1) if m else ""
        epic = re.search(r"^epic:\s*(\S+)", head, re.M)
        status = re.search(r"^status:\s*(\S+)", head, re.M)
        out.append({
            "number": number,
            "name": path.name,
            "epic": epic.group(1) if epic else None,
            "status": status.group(1) if status else None,
            "stories": re.findall(r"^## (US-\d{4})\.", text, re.M),
        })
    return out


def render(reports: list[Report]) -> tuple[str, bool]:
    lines, bad = [], False
    for r in reports:
        if not r.errors and not r.warns:
            continue
        lines.append(f"{r.path.name}:")
        for e in r.errors:
            bad = True
            lines.append(f"  ОШИБКА  {e}")
        for w in r.warns:
            lines.append(f"  замечание  {w}")
    return "\n".join(lines), bad


def open_plan_numbers() -> set[int]:
    """Номера планов, у которых есть открытая история.

    Спрашиваем журнал калибровки его же инструментом, а не гадаем по файлам:
    открытая история живёт там, и второго места у неё нет.
    """
    tool = Path.home() / ".cursor" / "time-analysis" / "calibrate.py"
    if not tool.is_file():
        return set()
    try:
        done = subprocess.run([sys.executable, "-X", "utf8", str(tool),
                               "stories", "--open"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    out: set[int] = set()
    for line in done.stdout.splitlines():
        m = re.match(r"\s*(\d{3})\s+US-\d{4}", line)
        if m:
            out.add(int(m.group(1)))
    return out


def hook_targets(payload: dict) -> list[Path]:
    """Файлы плана из входа хука Cursor.

    Правка инструментом приходит в `path` (у Claude было `file_path`).
    Команда оболочки пути в аргументах не отдаёт — её ищем в тексте команды,
    иначе проверка видела бы только один способ записи.
    """
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        return []
    found: list[Path] = []
    for key in ("file_path", "path", "notebook_path"):
        raw = inp.get(key)
        if isinstance(raw, str) and raw.strip():
            found.append(Path(raw))
    command = inp.get("command")
    if isinstance(command, str):
        for match in re.finditer(r"plans[\\/](\d{3}-[^\s\"']+\.md)", command):
            found.append(PLANS_DIR / match.group(1))
    seen: list[Path] = []
    for path in found:
        if path not in seen:
            seen.append(path)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hook", action="store_true")
    a = ap.parse_args()

    if a.hook:
        try:
            payload = json.load(sys.stdin)
        except (ValueError, OSError):
            return 0
        reports = [
            lint_file(path)
            for path in hook_targets(payload)
            if is_plan(path) and not path.name.startswith("_")
        ]
        if not reports:
            return 0
        out, bad = render(reports)
        if out:
            # postToolUse уже не отменяет запись. Текст уходит агенту, код 2
            # остаётся сигналом структурной ошибки — как у хука Claude.
            print(json.dumps(
                {"additional_context": "Валидатор плана:\n" + out},
                ensure_ascii=False,
            ))
            print(out, file=sys.stderr)
        return 2 if bad else 0

    targets = ([p for p in sorted(m for d in plan_dirs() for m in d.glob("[0-9][0-9][0-9]-*.md"))
                if (plan_number(p) or 0) >= FIRST_STORY_PLAN]
               if a.all else [Path(f) for f in a.files])
    if not targets:
        print("нечего проверять", file=sys.stderr)
        return 0
    reports = [lint_file(p) for p in targets]
    out, bad = render(reports)
    plans = collect_plans() if a.all else []
    clashes = check_number_clash(plans) if a.all else []
    clashes += check_story_clash(plans) if a.all else []

    divergences: list[str] = []
    if a.all:
        working = open_plan_numbers()
        paused = {q["number"] for q in plans if paused_marker(q["number"])}
        for q in plans:
            divergences += check_status(q, paused=paused, working=working)
    if clashes:
        bad = True
        out = (out + "\n" if out else "") + "\n".join(
            f"  ОШИБКА  {c}" for c in clashes)
    if divergences:
        bad = True
        out = (out + chr(10) if out else "") + chr(10).join(
            f"  РАСХОЖДЕНИЕ  {d}" for d in divergences)
    print(out or f"чисто: проверено {len(reports)}")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

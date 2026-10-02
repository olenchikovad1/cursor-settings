#!/usr/bin/env python3
"""Собрать производную часть набора `handoff/plans-kit/` из живого ~/.cursor.

    py -X utf8 handoff/build_plans_kit.py

Набор — папка, которую владелец отдаёт архивом другим людям, чтобы у них
работали планы, истории, шаги и оценки времени. Он раскладывается поверх их
`~/.cursor`, поэтому внутри повторяет его устройство: `skills/`, `hooks/`,
`time-analysis/`, `references/`, `plans/`.

Что здесь собирается, а что нет. Тексты скиллов, README, оси и эталонные
истории написаны руками прямо в `plans-kit/` и обезличены: номера планов и
историй владельца, имена проектов и машин вне его компьютера смысла не имеют.
Отсюда же, из скрипта, идёт то, что выводится из живых данных и устареет, если
переписать один раз:

* копии кода хуков и калибровки с поправленными порогами (в наборе все
  правила действуют с плана 001);
* эталоны шагов `references/steps/STEP-*.md` с медианами по журналу владельца —
  последняя версия на Opus 5.5 и текущая на Cursor, смешанные по тому же
  правилу, что и в живой памятке;
* снимок полок историй `references/sp-scale.json` — те же минуты, что сейчас
  печатает `calibrate.py estimate`;
* стартовый журнал шагов `time-analysis/records/steps.jsonl` без имён машин,
  сессий и фраз — чтобы у получателя версии и дрейф считались с первого дня.

Запускается заново, когда у владельца сменились числа; тексты при этом не
трогаются.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home() / ".cursor"
KIT = Path(__file__).resolve().parent / "plans-kit"

sys.path.insert(0, str(HOME / "time-analysis"))
sys.path.insert(0, str(HOME / "hooks"))
import calibrate  # noqa: E402
import steps_store  # noqa: E402
import versions  # noqa: E402

# Имена, по которым набор можно связать с владельцем. Строки с ними из
# текстов эталонов шагов убираются целиком: в них всегда речь о конкретном
# проекте, и без него фраза теряет смысл.
PRIVATE_WORDS = re.compile(
    r"assemblage|platform|resume|hrportal|docroi|\bplm\b|installation|"
    r"\breference\b|Laretto|tori|RBB-|DESKTOP-|olenchikov|claude", re.I)

# Начало версии «Cursor» в наборе — та же дата, что у владельца: по ней
# замеры стартового журнала делятся на старую и новую сторону.
CURSOR_SINCE = "2026-10-01T00:00:00+03:00"
# Стартовый журнал берётся с перехода владельца на Opus 5.5 и до перехода на
# Cursor: более ранние замеры описывают другую модель, а замеры Cursor у
# владельца начались только 02.10.2026 (до того хук не читал запрос из-за BOM)
# и меряют ход целиком, а не объявленный шаг. Версию «Cursor» получатель
# наполняет сам.
SEED_SINCE = "2026-09-24T00:00:00+03:00"
SEED_UNTIL = "2026-10-01T00:00:00+03:00"

# extract_steps.py — разбор транскриптов Claude Code; в Cursor сам по себе не
# нужен, но от него зависят units.py и тесты калибровки, без него они падают.
HOOK_FILES = ("time_estimate.py", "_time_estimate_common.py",
              "check_time_estimate_start.py", "lint_plan.py", "extract_steps.py")
TIME_FILES = ("calibrate.py", "journal_store.py", "steps_store.py",
              "versions.py", "units.py", "test_calibrate.py",
              "test_story_block.py", "test_units.py", "test_versions.py")

# Комментарии кода ссылаются на планы и истории владельца и на имя его версии
# эталонов; вне его машины это ни о чём, в наборе убирается.
CODE_PATCHES = (
    (re.compile(r"\s*\(план \d{3}(?:, US-\d{4})?\)"), ""),
    (re.compile(r"\s*\(US-\d{4}(?:,? плана? \d{3})?\)"), ""),
    (re.compile(r'^(\s*""")US-\d{4}(?: плана \d{3})?: ', re.M), r"\1"),
    (re.compile(r"с миграции \d+ "), ""),
    (re.compile(r"переход на курсор"), "работа в Cursor"),
    # у владельца предыдущая версия начинается датой, в наборе — с начала журнала
    (re.compile(r'self\.assertEqual\(vs\[-2\]\["start"\], "2026-09-24T00:00:00\+03:00"\)'),
     'self.assertIsNone(vs[-2]["start"])'),
    # тесты калибровки опираются на эталон фонда: в наборе это US-9007 (ось G,
    # 3 SP, 14.8 минуты — тот же уровень и ось, что у эталона владельца)
    (re.compile(r"US-0080"), "US-9007"),
    (re.compile(r"project: claude-home"), "project: demo"),
    (re.compile(r"~/\.claude/time-analysis"), "~/.cursor/time-analysis"),
)

# В наборе планы начинаются с 001 и все правила валидатора действуют сразу.
LINT_PATCHES = (
    (re.compile(r"^FIRST_SIZED_PLAN = \d+", re.M), "FIRST_SIZED_PLAN = 1"),
    (re.compile(r"^FIRST_EPIC_PLAN = \d+", re.M), "FIRST_EPIC_PLAN = 1"),
    (re.compile(r"^EPIC_WANTED_FROM = \d+", re.M), "EPIC_WANTED_FROM = 1"),
)


def copy_code() -> None:
    (KIT / "hooks" / "tests").mkdir(parents=True, exist_ok=True)
    (KIT / "time-analysis").mkdir(parents=True, exist_ok=True)
    for name in HOOK_FILES:
        text = (HOME / "hooks" / name).read_text(encoding="utf-8")
        if name == "lint_plan.py":
            for pattern, repl in LINT_PATCHES:
                text, n = pattern.subn(repl, text)
                assert n == 1, f"lint_plan.py: не нашлось {repl}"
        for pattern, repl in CODE_PATCHES:
            text = pattern.sub(repl, text)
        (KIT / "hooks" / name).write_text(text, encoding="utf-8", newline="\n")
    for name in TIME_FILES:
        text = (HOME / "time-analysis" / name).read_text(encoding="utf-8")
        for pattern, repl in CODE_PATCHES:
            text = pattern.sub(repl, text)
        (KIT / "time-analysis" / name).write_text(text, encoding="utf-8", newline="\n")


def _seed_steps() -> list[dict]:
    """Замеры с SEED_SINCE без всего, что указывает на машину и беседу."""
    since = datetime.fromisoformat(SEED_SINCE)
    until = datetime.fromisoformat(SEED_UNTIL)
    out = []
    for r in steps_store.read_all():
        ts = r.get("ts")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if not (since <= when < until) or not r.get("kind") or not r.get("sec"):
            continue
        rec = {"sec": r["sec"], "kind": r["kind"], "ref": r.get("ref"), "ts": ts}
        for key in ("model", "effort"):
            if r.get(key):
                rec[key] = r[key]
        scale = r.get("scale")
        if isinstance(scale, dict):
            # имена инструментов и файлов не нужны; числа — да
            rec["scale"] = {k: scale[k] for k in ("edited", "created", "cmds")
                            if scale.get(k)}
        out.append(rec)
    return out


def write_seed(steps: list[dict]) -> None:
    records = KIT / "time-analysis" / "records"
    records.mkdir(parents=True, exist_ok=True)
    with (records / "steps.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in steps:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for name in ("history", "plans", "workflows", "stories"):
        (records / f"{name}.jsonl").write_text("", encoding="utf-8")
    (KIT / "time-analysis" / ".gitignore").write_text(
        ".pending.json\nturns/\n__pycache__/\n", encoding="utf-8", newline="\n")


def kit_versions() -> list[dict]:
    return [
        {"n": 1, "start": None,
         "reason": "стартовый набор: замеры автора набора на Opus 5.5, сентябрь 2026"},
        {"n": 2, "start": CURSOR_SINCE, "reason": "работа в Cursor",
         "declared": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    ]


def write_versions(vs: list[dict]) -> None:
    (KIT / "references").mkdir(parents=True, exist_ok=True)
    (KIT / "references" / "versions.json").write_text(json.dumps({
        "_comment": [
            "Версии эталонов. Версия — граница по дате: замер относится к версии",
            "по своему времени, старое не стирается. В расчёт идут две последние.",
            "Новую объявляет владелец: calibrate.py version-new --from ГГГГ-ММ-ДД",
            "--reason \"...\". Версия 1 здесь — стартовый набор, пришедший вместе",
            "с архивом; версия 2 — то, что меряется уже на вашей машине.",
        ],
        "versions": vs,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _blended(steps: list[dict], vs: list[dict]) -> dict:
    return versions.step_references(steps, vs)["kinds"]


def _by_version(steps: list[dict], vs: list[dict]) -> dict[str, dict[int, list[float]]]:
    out: dict[str, dict[int, list[float]]] = {}
    for r in steps:
        n = versions.record_version(r, vs)
        if n is None:
            continue
        out.setdefault(r["kind"], {}).setdefault(n, []).append(float(r["sec"]))
    return out


def write_step_refs(steps: list[dict], vs: list[dict]) -> list[str]:
    """STEP-*.md с медианами по смеси версий и без строк про проекты владельца."""
    blended = _blended(steps, vs)
    per_version = _by_version(steps, vs)
    target = KIT / "references" / "steps"
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for src in sorted((HOME / "references" / "steps").glob("STEP-*.md")):
        text = src.read_text(encoding="utf-8")
        head, _, body = text.partition("\n---\n")
        fm: dict[str, str] = {}
        for line in head.splitlines()[1:]:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
        kind = fm.get("kind", "")
        b = blended.get(kind)
        old_median = float(fm.get("median_sec") or 0)
        if b and old_median:
            new_median = round(b["minutes"] * 60)
            scale = new_median / old_median
            fm["median_sec"] = str(new_median)
            fm["mean_sec"] = str(round(float(fm.get("mean_sec") or 0) * scale))
            fm["per_call_sec"] = str(round(float(fm.get("per_call_sec") or 0) * scale, 1))
            fm["samples"] = str(b["n_new"] + b["n_old"])
            fm["source"] = ("журнал шагов автора набора, версии Opus 5.5 и Cursor, "
                            f"снимок {datetime.now().date().isoformat()}")
        fm["provisional"] = "true"
        # Заголовок с числами переписывается по новой шапке, а абзацы про
        # конкретные проекты убираются: вне машины владельца они ни о чём.
        body_lines = [ln for ln in body.splitlines() if not PRIVATE_WORDS.search(ln)]
        body = "\n".join(body_lines)
        per_call = float(fm["per_call_sec"])
        slope = float(fm.get("slope") or 0.917)
        calls = int(fm.get("median_calls") or 1)
        body = re.sub(r"\*\*На один вызов инструмента — [\d.]+ с\.\*\*",
                      f"**На один вызов инструмента — {fm['per_call_sec']} с.**", body)
        body = re.sub(r"^\| время \|.*$",
                      "| время | " + " | ".join(f"{round(per_call * n ** slope)} с"
                                               for n in (1, 2, 3, 5, 8)) + " |",
                      body, flags=re.M)
        body = re.sub(r"Типичный такой шаг — \d+ вызов\w*, это \*\*\d+ с\*\*\.",
                      f"Типичный такой шаг — {calls} "
                      f"{'вызов' if calls == 1 else 'вызова' if calls < 5 else 'вызовов'}, "
                      f"это **{round(per_call * calls ** slope)} с**.", body)
        body = re.sub(r"Наблюдаемая медиана шага — \d+ с, среднее \(для суммы\) — \d+ с\. Замеров: \d+\.",
                      f"Наблюдаемая медиана шага — {fm['median_sec']} с, среднее (для суммы) — "
                      f"{fm['mean_sec']} с. Замеров: {fm['samples']}.", body)
        new_head = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items())
        (target / src.name).write_text(new_head + "\n---\n" + body.rstrip() + "\n",
                                       encoding="utf-8", newline="\n")
        pv = per_version.get(kind, {})
        v1 = sorted(pv.get(1, []))
        med = lambda xs: f"{xs[len(xs) // 2] / 60:.1f}" if xs else "—"  # noqa: E731
        rows.append(f"| {fm.get('id')} | {kind} | {med(v1)} | {len(v1)} | "
                    f"{float(fm['per_call_sec'])} |")
    (target / "README.md").write_text(
        "# Эталоны шагов: откуда числа\n\n"
        "Каждый файл описывает вид деятельности: цену одного вызова инструмента,\n"
        "наклон, медиану и что влияет на время. Числа в шапках сняты с журнала\n"
        "автора набора за его последнюю версию до Cursor (Opus 5.5, сентябрь\n"
        "2026); тот же журнал лежит в `time-analysis/records/steps.jsonl` как\n"
        "стартовый и считается у вас версией 1.\n\n"
        "| эталон | вид | медиана, мин | замеров | на вызов, с |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        "Памятка в начале хода считает медианы по журналу и смешивает стартовые\n"
        "замеры с вашими (версия 2, «работа в Cursor») с весом n/(n+5) по числу\n"
        "ваших замеров. К файлам она обращается, только пока журнала нет. Со\n"
        "временем числа разойдутся с шапками — это нормально: журнал пополняется\n"
        "сам, файлы правятся руками при пересборке.\n\n"
        "Единица замера в Cursor — ход целиком, от реплики до ответа, а не\n"
        "каждое объявленное действие: транскрипт Cursor не несёт времени вызовов,\n"
        "и хук меряет то, что может. Поэтому первые ваши замеры будут длиннее\n"
        "стартовых; смесь это учтёт постепенно, а при устойчивом расхождении хук\n"
        "предложит завести новую версию.\n",
        encoding="utf-8", newline="\n")
    return rows


def write_sp_scale() -> dict:
    """Полки историй — те же минуты, что сейчас выдаёт оценка владельца."""
    shelves = calibrate.story_shelves()
    out_shelves = []
    notes = {0.5: "история по смыслу целая, но заметно мельче обычной",
             1: "одно место, куда надо вникнуть; число файлов не важно",
             2: "два-три места разного рода",
             3: "много разных мест либо новая общая вещь с потребителями",
             5: "история строит слой, на который лягут следующие"}
    for sp in (0.5, 1, 2, 3, 5):
        centre, half = shelves.get(float(sp), (None, None))
        out_shelves.append({
            "sp": sp,
            "minutes": [round(centre - half, 1), round(centre + half, 1)] if centre else None,
            "note": notes[sp]})
    for sp in (8, 13):
        out_shelves.append({"sp": sp, "minutes": None,
                            "note": "эталона нет и не будет: пучок осей, считается их суммой"})
    out_shelves.append({"sp": 21, "minutes": None,
                        "note": "внятного времени нет — дробить до 8-13"})
    one = shelves.get(1.0, (None,))[0]
    data = {
        "_comment": [
            "Во что сейчас обходится история того или иного размера. Это СНИМОК,",
            "а не закон: числа двигаются по мере накопления ваших фактов, смысл SP",
            "при этом не меняется. Размер истории ставится СРАВНЕНИЕМ с эталонными",
            "историями (references/stories/), а минуты читаются отсюда уже после",
            "того, как размер выбран.",
            "",
            "Снимок снят с оценки автора набора: полки его фонда, смешанные с",
            "закрытыми историями его текущей версии. На вашей машине оценка",
            "смешивает эти полки с вашими закрытыми историями (вес n/(n+5)).",
        ],
        "version": 1,
        "updated": datetime.now().date().isoformat(),
        "source": "снимок смешанных полок автора набора на дату updated",
        "provisional": True,
        "anchor_minutes_per_sp": round(one, 1) if one else None,
        "shelves": out_shelves,
        "split_rules": {"always_above": 21, "consider_at": [13, 21],
                        "target": [3, 5, 8],
                        "never_if": "дробление искажает саму историю"},
    }
    (KIT / "references" / "sp-scale.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    return {sp: shelves.get(float(sp)) for sp in (0.5, 1, 2, 3, 5)}


def main() -> int:
    KIT.mkdir(parents=True, exist_ok=True)
    copy_code()
    steps = _seed_steps()
    write_seed(steps)
    vs = kit_versions()
    write_versions(vs)
    rows = write_step_refs(steps, vs)
    shelves = write_sp_scale()
    print(f"шагов в стартовом журнале: {len(steps)}")
    print(f"эталонов шагов: {len(rows)}")
    print("полки историй (минуты):",
          {sp: (round(c, 1) if c else None) for sp, (c, _) in shelves.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())

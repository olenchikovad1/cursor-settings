"""Версии эталонов."""
import json
import sys
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import versions  # noqa: E402

MSK = timezone(timedelta(hours=3))


def _file(entries: list[dict]) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "versions.json"
    p.write_text(json.dumps({"versions": entries}, ensure_ascii=False),
                 encoding="utf-8")
    return p


TWO = [
    {"n": 1, "start": None, "reason": "всё до перехода"},
    {"n": 2, "start": "2026-09-24T00:00:00+03:00",
     "reason": "Переход на Opus 5.5 и актуализация подходов"},
]


class TestVersionOf(unittest.TestCase):
    def test_замер_до_границы_в_предыдущей_версии(self):
        vs = versions.load(_file(TWO))
        self.assertEqual(versions.version_of("2026-09-23T20:59:59+00:00", vs), 1)

    def test_час_ночи_по_москве_уже_в_новой_версии(self):
        """Граница — начало дня по местному времени, замеры — в UTC. 01:00 МСК
        24.09 — это 22:00 UTC 23.09, и замер обязан попасть в новую версию."""
        vs = versions.load(_file(TWO))
        self.assertEqual(versions.version_of("2026-09-23T22:00:00+00:00", vs), 2)

    def test_запись_без_времени_не_угадывается(self):
        vs = versions.load(_file(TWO))
        self.assertIsNone(versions.version_of(None, vs))


class TestActive(unittest.TestCase):
    def test_в_расчёт_идут_две_последние(self):
        three = TWO + [{"n": 3, "start": "2026-10-10T00:00:00+03:00",
                        "reason": "третья"}]
        vs = versions.load(_file(three))
        self.assertEqual(versions.active(vs), (3, 2))

    def test_одна_версия_предыдущей_нет(self):
        vs = versions.load(_file(TWO[:1]))
        self.assertEqual(versions.active(vs), (1, None))


class TestDeclare(unittest.TestCase):
    def test_новая_версия_с_даты_и_причиной(self):
        p = _file(TWO)
        v = versions.declare(p, "2026-10-01", "новый приём работы", tz=MSK)
        self.assertEqual(v["n"], 3)
        self.assertEqual(v["start"], "2026-10-01T00:00:00+03:00")
        self.assertEqual([x["n"] for x in versions.load(p)], [1, 2, 3])

    def test_без_причины_отказ(self):
        with self.assertRaises(ValueError):
            versions.declare(_file(TWO), "2026-10-01", "  ", tz=MSK)

    def test_граница_раньше_текущей_отказ(self):
        """Версии идут по времени; граница в прошлом текущей версии сделала бы
        принадлежность замера зависящей от порядка строк в файле."""
        with self.assertRaises(ValueError):
            versions.declare(_file(TWO), "2026-09-20", "назад", tz=MSK)


class TestSplit(unittest.TestCase):
    def test_сумма_по_версиям_равна_журналу_и_старое_выпадает(self):
        three = TWO + [{"n": 3, "start": "2026-10-10T00:00:00+03:00",
                        "reason": "третья"}]
        vs = versions.load(_file(three))
        steps = [{"ts": "2026-09-01T10:00:00+00:00"},
                 {"ts": "2026-09-25T10:00:00+00:00"},
                 {"ts": "2026-10-11T10:00:00+00:00"},
                 {"ts": None}]
        stories = [{"started": "2026-09-25T10:00:00+00:00", "actual_seconds": 300},
                   {"started": "2026-09-25T11:00:00+00:00"}]
        s = versions.summary(steps, stories, vs)
        rows = {r["n"]: r for r in s["versions"]}
        self.assertEqual(rows[1]["steps"], 1)
        self.assertFalse(rows[1]["in_calculation"])
        self.assertTrue(rows[2]["in_calculation"])
        self.assertEqual(rows[2]["stories"], 1)
        self.assertEqual(s["steps_without_time"], 1)
        self.assertEqual(sum(r["steps"] for r in s["versions"])
                         + s["steps_without_time"], len(steps))


class TestRealFile(unittest.TestCase):
    def test_граница_перехода_на_курсор_заведена(self):
        vs = versions.load(versions.VERSIONS_PATH)
        cur = vs[-1]
        self.assertEqual(cur["start"], "2026-10-01T00:00:00+03:00")
        self.assertEqual(cur["reason"], "работа в Cursor")
        self.assertIsNone(vs[-2]["start"])


def _steps(kind: str, sec: float, n: int, day: str) -> list[dict]:
    return [{"kind": kind, "sec": sec, "ts": f"{day}T10:{i // 60:02d}:{i % 60:02d}+00:00",
             "host": "A"} for i in range(n)]


OLD, NEW = "2026-09-10", "2026-09-25"


class TestStepBlend(unittest.TestCase):
    def setUp(self):
        self.vs = versions.load(_file(TWO))

    def _base(self) -> list[dict]:
        """Пять видов, где новая версия на 20% быстрее старой."""
        rows = []
        for k in ("a", "b", "c", "d", "e"):
            rows += _steps(k, 60, 20, OLD) + _steps(k, 48, 20, NEW)
        return rows

    def test_вес_новой_растёт_с_числом_замеров(self):
        rows = self._base() + _steps("x", 60, 20, OLD) + _steps("x", 30, 2, NEW)
        refs = versions.step_references(rows, self.vs)["kinds"]
        self.assertAlmostEqual(refs["a"]["share_new"], 20 / 25, places=2)
        self.assertAlmostEqual(refs["x"]["share_new"], 2 / 7, places=2)
        self.assertEqual(refs["x"]["source"], "смешано")

    def test_вид_без_новых_замеров_берёт_старое_с_поправкой(self):
        rows = self._base() + _steps("y", 100, 20, OLD)
        out = versions.step_references(rows, self.vs)
        self.assertAlmostEqual(out["coefficient"], 0.8, places=2)
        y = out["kinds"]["y"]
        self.assertEqual(y["source"], "старое с поправкой")
        self.assertAlmostEqual(y["minutes"], 100 * 0.8 / 60, places=2)

    def test_мало_видов_в_обеих_версиях_отказ_от_коэффициента(self):
        rows = (_steps("a", 60, 20, OLD) + _steps("a", 48, 20, NEW)
                + _steps("y", 100, 20, OLD))
        out = versions.step_references(rows, self.vs)
        self.assertIsNone(out["coefficient"])
        self.assertIn("мало", out["coefficient_why"])
        y = out["kinds"]["y"]
        self.assertEqual(y["source"], "старое как есть")
        self.assertAlmostEqual(y["minutes"], 100 / 60, places=2)

    def test_выбивающийся_вид_не_двигает_коэффициент(self):
        """Весь сьют ×3.28 из-за смены проектов не должен размазаться на всех."""
        rows = self._base() + _steps("suite", 60, 20, OLD) + _steps("suite", 200, 20, NEW)
        out = versions.step_references(rows, self.vs)
        self.assertAlmostEqual(out["coefficient"], 0.8, places=2)
        self.assertIn("suite", out["excluded_kinds"])

    def test_ход_целиком_в_эталон_шага_не_идёт(self):
        """Замер Cursor — ход, а не шаг. Подмешанный в вид, он двигает медиану."""
        rows = self._base() + [
            {"kind": "a", "sec": 3600, "ts": f"{NEW}T12:00:00+00:00",
             "host": "A", "unit": "turn"},
        ]
        out = versions.step_references(rows, self.vs)
        self.assertEqual(out["kinds"]["a"]["n_new"], 20)

    def test_дубли_журнала_не_удваивают_выборку(self):
        rows = self._base()
        out = versions.step_references(rows + rows, self.vs)
        self.assertEqual(out["kinds"]["a"]["n_new"], 20)

    def test_версии_раньше_предыдущей_не_читаются(self):
        three = [{"n": 1, "start": None, "reason": "т"},
                 {"n": 2, "start": "2026-09-15T00:00:00+03:00", "reason": "т"},
                 {"n": 3, "start": "2026-09-20T00:00:00+03:00", "reason": "т"}]
        vs = versions.load(_file(three))
        rows = _steps("a", 999, 20, "2026-09-10") + _steps("a", 60, 20, "2026-09-17")
        out = versions.step_references(rows, vs)
        self.assertEqual(out["kinds"]["a"]["n_old"], 20)
        self.assertAlmostEqual(out["kinds"]["a"]["minutes"], 1.0, places=2)


def _stories(sp: float, minutes: float, n: int, day: str = NEW,
             sized_by: str = "comparison") -> list[dict]:
    return [{"sp": sp, "actual_seconds": minutes * 60, "sized_by": sized_by,
             "started": f"{day}T10:00:{i:02d}+00:00"} for i in range(n)]


FUND = {0.5: (3.25, 0.25), 1.0: (5.85, 1.05), 2.0: (9.1, 1.6),
        3.0: (15.95, 3.05), 5.0: (27.95, 5.05)}


class TestShelfBlend(unittest.TestCase):
    def setUp(self):
        self.vs = versions.load(_file(TWO))

    def test_полка_смешивается_по_числу_историй_новой_версии(self):
        rows = _stories(2.0, 6.0, 20)
        out = versions.shelf_references(FUND, rows, self.vs)["shelves"][2.0]
        self.assertEqual(out["source"], "смешано")
        self.assertAlmostEqual(out["share_new"], 0.8, places=2)
        self.assertAlmostEqual(out["minutes"], 0.8 * 6.0 + 0.2 * 9.1, places=2)

    def test_размер_без_сравнения_в_полку_не_идёт(self):
        """Иначе шкала догоняет собственную ошибку — раздел sp_drift."""
        rows = _stories(2.0, 1.0, 20, sized_by="guess")
        out = versions.shelf_references(FUND, rows, self.vs)["shelves"][2.0]
        self.assertEqual(out["n_new"], 0)

    def test_отбракованная_история_в_полку_не_идёт(self):
        """Простой, оставленная на ночь сессия — не работа. 02.10.2026 одна
        история в 583 минуты подняла полку 3 SP до 24 минут и утянула за
        собой пятёрку; отбор тот же, что у shelf_rejection в calibrate."""
        rows = _stories(3.0, 12.0, 1) + _stories(3.0, 583.0, 1)
        plain = versions.shelf_references(FUND, rows, self.vs)["shelves"][3.0]
        filtered = versions.shelf_references(
            FUND, rows, self.vs,
            reject=lambda s: s["actual_seconds"] > 4 * 3600)["shelves"][3.0]
        self.assertEqual(plain["n_new"], 2)
        self.assertEqual(filtered["n_new"], 1)
        self.assertGreater(plain["minutes"], 60)
        self.assertLess(filtered["minutes"], 20)

    def test_старые_истории_в_полку_не_идут_их_уже_держит_фонд(self):
        rows = _stories(2.0, 1.0, 20, day=OLD)
        out = versions.shelf_references(FUND, rows, self.vs)["shelves"][2.0]
        self.assertEqual(out["n_new"], 0)

    def test_пустая_полка_берёт_фонд_с_коэффициентом(self):
        rows = (_stories(1.0, 5.85 * 0.7, 5) + _stories(2.0, 9.1 * 0.7, 5)
                + _stories(3.0, 15.95 * 0.7, 5))
        out = versions.shelf_references(FUND, rows, self.vs)
        self.assertAlmostEqual(out["coefficient"], 0.7, places=2)
        five = out["shelves"][5.0]
        self.assertEqual(five["source"], "старое с поправкой")
        self.assertAlmostEqual(five["minutes"], 27.95 * 0.7, places=2)

    def test_мало_полок_отказ_от_коэффициента(self):
        rows = _stories(2.0, 6.0, 5)
        out = versions.shelf_references(FUND, rows, self.vs)
        self.assertIsNone(out["coefficient"])
        self.assertEqual(out["shelves"][5.0]["source"], "старое как есть")
        self.assertAlmostEqual(out["shelves"][5.0]["minutes"], 27.95, places=2)

    def test_полки_не_убывают_с_ростом_размера(self):
        """Одна длинная история на полке 0.5 не должна сделать её дороже 1 SP."""
        rows = _stories(0.5, 11.0, 1) + _stories(1.0, 3.0, 5)
        out = versions.shelf_references(FUND, rows, self.vs)["shelves"]
        values = [out[sp]["minutes"] for sp in sorted(out)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(out[0.5]["levelled"])


DECLARED = [{"n": 1, "start": None, "reason": "т"},
            {"n": 2, "start": "2026-09-24T00:00:00+03:00", "reason": "т",
             "declared": "2026-09-26T00:00:00+00:00"}]


def _closed(sp: float, minutes: float, n: int, day: str = "2026-09-27") -> list[dict]:
    return [{"sp": sp, "actual_seconds": minutes * 60, "sized_by": "comparison",
             "started": f"{day}T{10 + i // 60:02d}:{i % 60:02d}:00+00:00"} for i in range(n)]


class TestDrift(unittest.TestCase):
    def setUp(self):
        self.vs = versions.load(_file(DECLARED))
        self.shelves = {2.0: 10.0}

    def test_серия_историй_за_половину_оценки_даёт_вопрос(self):
        d = versions.drift(_closed(2.0, 5.0, 20), [], {}, self.shelves, self.vs)
        self.assertTrue(d["propose"])
        self.assertEqual(d["stories"]["side"], "быстрее")
        self.assertAlmostEqual(d["stories"]["median_ratio"], 0.5, places=2)
        self.assertEqual(d["stories"]["n"], versions.STORY_SERIES)
        self.assertEqual(d["since"][:10], "2026-09-27")

    def test_три_истории_одного_дня_вопроса_не_дают(self):
        d = versions.drift(_closed(2.0, 5.0, 3), [], {}, self.shelves, self.vs)
        self.assertFalse(d["propose"])

    def test_разброс_в_обе_стороны_не_серия(self):
        rows = _closed(2.0, 5.0, 10) + _closed(2.0, 20.0, 10, day="2026-09-28")
        d = versions.drift(rows, [], {}, self.shelves, self.vs)
        self.assertFalse(d["propose"])

    def test_записи_до_объявления_версии_не_считаются(self):
        """Иначе сразу после объявления детектор повторит вопрос про тот же сдвиг."""
        d = versions.drift(_closed(2.0, 5.0, 20, day="2026-09-25"), [], {},
                           self.shelves, self.vs)
        self.assertFalse(d["propose"])

    def test_после_не_сейчас_нужна_новая_полная_серия(self):
        p = _file(DECLARED)
        versions.decline(p, at="2026-09-27T12:00:00+00:00")
        vs = versions.load(p)
        rows = _closed(2.0, 5.0, 20)            # 10:00–10:19, до отказа
        self.assertFalse(versions.drift(rows, [], {}, self.shelves, vs)["propose"])
        later = _closed(2.0, 5.0, 20, day="2026-09-28")
        self.assertTrue(versions.drift(rows + later, [], {}, self.shelves, vs)["propose"])

    def test_серия_шагов_тоже_даёт_вопрос(self):
        steps = [{"kind": "a", "sec": 120, "host": "A",
                  "ts": f"2026-09-27T{10 + i // 60:02d}:{i % 60:02d}:00+00:00"}
                 for i in range(300)]
        refs = {"a": {"minutes": 1.0}}
        d = versions.drift([], steps, refs, self.shelves, self.vs)
        self.assertTrue(d["propose"])
        self.assertEqual(d["steps"]["side"], "медленнее")


if __name__ == "__main__":
    unittest.main()

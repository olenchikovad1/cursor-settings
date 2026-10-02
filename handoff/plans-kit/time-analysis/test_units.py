"""Тесты единицы работы.

Зачем они есть. 74% измеренного времени не попадало ни в одну открытую
историю: часть — потому что истории не открывались, часть — потому что работа
начиналась просьбой в разговоре и историей не становилась никогда. Любая
сводка по историям описывала восьмую часть работы и выдавала её за картину
целиком.

Единица заводится сама: граница не требует от владельца ни объявления начала,
ни объявления конца.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
HOOKS = Path.home() / ".cursor" / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import units  # noqa: E402


def asst(ts, blocks, **extra):
    e = {"type": "assistant", "timestamp": ts, "message": {"content": blocks}}
    e.update(extra)
    return e


def step(text, name="Bash", inp=None):
    return [{"type": "text", "text": text},
            {"type": "tool_use", "name": name, "input": inp or {"command": "ls"}}]


def human(ts, text="сделай вот это"):
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


def tool_result(ts):
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


class TestГраницаЕдиницы(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        p = Path(self.tmp.name) / "t.jsonl"
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_реплика_владельца_открывает_единицу(self):
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Правлю — ~1 мин",
                                              "Edit", {"file_path": "a.py"})),
            asst("2026-01-01T00:01:05Z", step("Гоняю — ~1 мин")),
            human("2026-01-01T00:02:00Z", "а теперь другое"),
            asst("2026-01-01T00:02:05Z", step("Смотрю — ~1 мин")),
            asst("2026-01-01T00:03:05Z", step("Ещё — ~1 мин")),
        ])
        us = units.units_from_transcript(p)
        self.assertEqual(len(us), 2)
        self.assertEqual(us[0]["opened_by"], "сделай вот это")
        self.assertEqual(us[1]["opened_by"], "а теперь другое")

    def test_результат_инструмента_единицу_не_рвёт(self):
        """Результаты приходят как type=user; приняв их за реплику, каждый
        вызов породил бы свою единицу."""
        # Шагов после результата инструмента должно быть ДВА: последний шаг
        # хода не измеряется вовсе, и с одним проверка проходила бы даже у
        # сломанного разбора — вторая группа выходила бы пустой сама собой.
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Правлю — ~1 мин")),
            tool_result("2026-01-01T00:00:30Z"),
            asst("2026-01-01T00:01:05Z", step("Ещё — ~1 мин")),
            asst("2026-01-01T00:02:05Z", step("И ещё — ~1 мин")),
            asst("2026-01-01T00:03:05Z", step("Последний — ~1 мин")),
        ])
        us = units.units_from_transcript(p)
        self.assertEqual(len(us), 1, [u["opened_by"] for u in us])
        self.assertEqual(us[0]["steps"], 3)

    def test_владелец_не_объявляет_ни_начала_ни_конца(self):
        """В транскрипте нет ни одной команды открытия — единицы всё равно есть."""
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Правлю — ~1 мин")),
            asst("2026-01-01T00:01:05Z", step("Ещё — ~1 мин")),
        ])
        us = units.units_from_transcript(p)
        self.assertEqual(len(us), 1)
        self.assertGreater(us[0]["seconds"], 0)


class TestРодВремени(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        p = Path(self.tmp.name) / "t.jsonl"
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_правки_делают_единицу_работой(self):
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Правлю — ~1 мин",
                                              "Edit", {"file_path": "a.py"})),
            asst("2026-01-01T00:01:05Z", step("Ещё — ~1 мин")),
        ])
        self.assertEqual(units.units_from_transcript(p)[0]["genre"], "работа")

    def test_только_чтение_это_разговор(self):
        """Обсуждение, планирование и разбор — отдельный род времени: иначе сам
        разговор о качестве попадёт в счёт качества."""
        p = self._write([
            human("2026-01-01T00:00:00Z", "объясни, как это устроено"),
            asst("2026-01-01T00:00:05Z", step("Читаю — ~1 мин",
                                              "Read", {"file_path": "a.py"})),
            asst("2026-01-01T00:01:05Z", step("Ещё читаю — ~1 мин",
                                              "Read", {"file_path": "b.py"})),
        ])
        self.assertEqual(units.units_from_transcript(p)[0]["genre"], "разговор")

    def test_прогон_тестов_без_правок_это_разбор(self):
        """Разбор стоит времени, но работой не считается: ничего не изменено."""
        p = self._write([
            human("2026-01-01T00:00:00Z", "почему падает?"),
            asst("2026-01-01T00:00:05Z", step("Гоняю — ~1 мин",
                                              inp={"command": "pytest -q"})),
            asst("2026-01-01T00:01:05Z", step("Смотрю — ~1 мин",
                                              inp={"command": "cat a.py"})),
        ])
        self.assertEqual(units.units_from_transcript(p)[0]["genre"], "разговор")

    def test_коммит_без_правок_это_работа(self):
        """Состояние изменено снаружи файлов, а не только правкой в них."""
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Коммичу — ~1 мин",
                                              inp={"command": "git commit -m x"})),
            asst("2026-01-01T00:01:05Z", step("Смотрю — ~1 мин",
                                              inp={"command": "cat a.py"})),
        ])
        self.assertEqual(units.units_from_transcript(p)[0]["genre"], "работа")


class TestПокрытие(unittest.TestCase):
    def test_каждая_минута_ровно_в_одной_единице(self):
        us = [
            {"session": "a", "started": "2026-01-01T00:00:00+00:00",
             "finished": "2026-01-01T00:02:00+00:00", "seconds": 120},
            {"session": "a", "started": "2026-01-01T00:02:00+00:00",
             "finished": "2026-01-01T00:04:00+00:00", "seconds": 120},
        ]
        self.assertEqual(units.overlaps(us), [])

    def test_пересечение_внутри_сессии_это_ошибка(self):
        us = [
            {"session": "a", "started": "2026-01-01T00:00:00+00:00",
             "finished": "2026-01-01T00:03:00+00:00", "seconds": 180},
            {"session": "a", "started": "2026-01-01T00:02:00+00:00",
             "finished": "2026-01-01T00:04:00+00:00", "seconds": 120},
        ]
        self.assertEqual(len(units.overlaps(us)), 1)

    def test_параллельные_сессии_пересекаться_вправе(self):
        """Две сессии работают одновременно — это разные потоки работы, а не
        двойной счёт одной и той же минуты."""
        us = [
            {"session": "a", "started": "2026-01-01T00:00:00+00:00",
             "finished": "2026-01-01T00:03:00+00:00", "seconds": 180},
            {"session": "b", "started": "2026-01-01T00:02:00+00:00",
             "finished": "2026-01-01T00:04:00+00:00", "seconds": 120},
        ]
        self.assertEqual(units.overlaps(us), [])

    def test_доля_нераспределённого_названа_числом(self):
        steps = [{"ts": "2026-01-01T00:00:30+00:00", "sec": 30},
                 {"ts": "2026-01-01T09:00:00+00:00", "sec": 90}]
        us = [{"started": "2026-01-01T00:00:00+00:00",
               "finished": "2026-01-01T00:02:00+00:00", "seconds": 120}]
        cov = units.coverage(steps, us)
        self.assertEqual(cov["unassigned_n"], 1)
        self.assertEqual(cov["unassigned_pct"], 50.0)

    def test_пустая_выборка_не_делит_на_ноль(self):
        cov = units.coverage([], [])
        self.assertEqual(cov["unassigned_pct"], 0.0)


class TestПаузы(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        p = Path(self.tmp.name) / "t.jsonl"
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_пауза_владельца_в_работу_не_входит(self):
        """Ловились интервалы до 39 минут — владелец отходил от терминала.
        Длительность единицы считается по её шагам, а не по краям."""
        p = self._write([
            human("2026-01-01T00:00:00Z"),
            asst("2026-01-01T00:00:05Z", step("Правлю — ~1 мин")),
            asst("2026-01-01T00:01:05Z", step("Ещё — ~1 мин")),
            # владелец ушёл на сорок минут и вернулся
            human("2026-01-01T00:41:00Z", "продолжай"),
            asst("2026-01-01T00:41:05Z", step("Дальше — ~1 мин")),
            asst("2026-01-01T00:42:05Z", step("И ещё — ~1 мин")),
        ])
        us = units.units_from_transcript(p)
        self.assertEqual(len(us), 2)
        # 40 минут простоя не попали ни в одну единицу
        self.assertLess(sum(u["seconds"] for u in us), 300)


if __name__ == "__main__":
    unittest.main()

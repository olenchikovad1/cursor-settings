"""Тесты калибровки времени. Запуск: python -m unittest discover ~/.cursor/time-analysis

Зачем они есть. Здесь арифметика, ошибку в которой не видно глазом: полка
считается по усечённой медиане, пустая достраивается интерполяцией по логарифму,
а размер задачи приводится к геометрической шкале. Ровно в такой арифметике и
жил баг, из-за которого сумма очков плана уезжала в поле размера одной задачи и
портила верх шкалы — молча, без единой ошибки в логах, месяц.

Каждый тест работает на своём временном журнале: JOURNAL_PATH подменяется, чтобы
прогон тестов не дописывал мусор в настоящую историю замеров.
"""

import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

TIME_ANALYSIS = Path.home() / ".cursor" / "time-analysis"
HOOKS = Path.home() / ".cursor" / "hooks"
for extra in (str(TIME_ANALYSIS), str(HOOKS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import calibrate  # noqa: E402
import journal_store  # noqa: E402
import steps_store  # noqa: E402


class JournalCase(unittest.TestCase):
    """База: подменяет журнал на временный каталог.

    До миграции 014 журнал был одним файлом, и хватало подмены JOURNAL_PATH.
    Теперь состояние разложено (matrix.json + records/*.jsonl + .pending.json),
    поэтому перенаправляется каталог целиком через journal_store.set_dir.
    Тела самих тестов при этом не менялись: write()/read() принимают и отдают
    тот же плоский словарь, что и раньше.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._real_dir = journal_store.DIR
        self._real_path = calibrate.JOURNAL_PATH
        journal_store.set_dir(self.dir)
        self.path = journal_store.LEGACY_PATH
        calibrate.JOURNAL_PATH = self.path
        # Полки эталонных ИСТОРИЙ лежат вне журнала (references/sp-scale.json) и
        # имеют приоритет над журнальными. Эти тесты проверяют арифметику
        # журнала, поэтому настоящий файл на время прогона отводится: иначе они
        # проверяли бы числа из фонда, а не то, что построил сам тест.
        self._real_scale = calibrate.SP_SCALE_PATH
        calibrate.SP_SCALE_PATH = self.dir / "sp-scale-absent.json"

    def tearDown(self):
        journal_store.set_dir(self._real_dir)
        calibrate.JOURNAL_PATH = self._real_path
        calibrate.SP_SCALE_PATH = self._real_scale
        self.tmp.cleanup()

    def write(self, state):
        journal_store.write_state(state)

    def read(self):
        return journal_store.read_state()

    @staticmethod
    def samples(sp, seconds, count):
        return [
            {"sp": sp, "actual_seconds": seconds, "label": "проба %d" % i}
            for i in range(count)
        ]


class TestSnapToScale(unittest.TestCase):
    def test_scale_values_unchanged(self):
        for sp in calibrate.SP_SCALE:
            self.assertEqual(calibrate.snap_to_scale(sp), sp)

    def test_snaps_by_logarithm_not_distance(self):
        # 17 равноудалено от 13 и 21 по разнице, но геометрическая середина
        # отрезка — sqrt(13*21) ≈ 16.5, поэтому 17 относится уже к 21
        self.assertEqual(calibrate.snap_to_scale(17), 21)
        self.assertEqual(calibrate.snap_to_scale(16), 13)
        self.assertEqual(calibrate.snap_to_scale(3.5), 3)
        self.assertEqual(calibrate.snap_to_scale(6), 5)
        self.assertEqual(calibrate.snap_to_scale(11), 13)

    def test_clamps_both_ends(self):
        self.assertEqual(calibrate.snap_to_scale(0.1), 0.5)
        self.assertEqual(calibrate.snap_to_scale(100), calibrate.SP_MAX)


class TestSpSamples(JournalCase):
    def test_off_scale_records_ignored(self):
        # ровно те записи, что попадали в историю из-за бага pending_sp
        history = [{"sp": 45, "actual_seconds": 184, "label": "оценка плана"}]
        self.assertEqual(calibrate.sp_samples(history), {})

    def test_multitask_turn_does_not_feed_shelf(self):
        history = [{
            "sp": 5, "sp_points": [3, 5, 8], "actual_seconds": 600, "label": "три задачи",
        }]
        self.assertEqual(calibrate.sp_samples(history), {})

    def test_single_point_turn_feeds_shelf(self):
        history = [{"sp": 5, "sp_points": [5], "actual_seconds": 600, "label": "одна"}]
        self.assertEqual(calibrate.sp_samples(history), {5: [600.0]})

    def test_off_scale_value_snapped_into_shelf(self):
        history = [{"sp": 11, "actual_seconds": 660, "label": "вне шкалы"}]
        self.assertEqual(calibrate.sp_samples(history), {13: [660.0]})


class TestShelfArithmetic(JournalCase):
    def test_shelves_are_summed_not_points(self):
        # три задачи по 2 SP — это три полки по 2, а не одна полка на 6 SP:
        # шкала геометрическая, и подмена одного другим и есть та ошибка,
        # из-за которой оценки уезжали на порядок
        self.write({"history": self.samples(2, 120, 3) + self.samples(8, 900, 3)})
        rows = calibrate.sp_table(self.read()["history"])
        three_small, _ = calibrate.shelf_seconds_for(rows, [2, 2, 2])
        one_big, _ = calibrate.shelf_seconds_for(rows, [8])
        self.assertEqual(three_small, 360)
        self.assertEqual(one_big, 900)
        self.assertNotEqual(three_small, one_big)

    def test_interpolated_shelf_is_between_neighbours(self):
        self.write({"history": self.samples(3, 300, 3) + self.samples(13, 1200, 3)})
        rows = calibrate.sp_table(self.read()["history"])
        by_sp = {r["sp"]: r for r in rows}
        self.assertEqual(by_sp[3]["source"], "measured")
        self.assertEqual(by_sp[5]["source"], "interpolated")
        self.assertLess(by_sp[3]["minutes"], by_sp[5]["minutes"])
        self.assertLess(by_sp[5]["minutes"], by_sp[13]["minutes"])

    def test_median_not_mean(self):
        # один затянувшийся прогон не должен тащить полку за собой
        history = [
            {"sp": 3, "actual_seconds": 300, "label": "a"},
            {"sp": 3, "actual_seconds": 300, "label": "b"},
            {"sp": 3, "actual_seconds": 6000, "label": "выброс"},
        ]
        rows = calibrate.sp_table(history)
        self.assertEqual({r["sp"]: r["minutes"] for r in rows}[3], 5.0)


class TestEstimateSp(JournalCase):
    def setUp(self):
        super().setUp()
        self.write({"history": self.samples(5, 600, 3)})

    def test_turn_scope_stores_points_list_not_sum(self):
        calibrate.cmd_estimate_sp([5, 5, 5], "три задачи", "turn")
        pending = self.read()["pending"]
        # сумма 15 в поле размера — это и был баг: она уезжала в sp как размер
        # одной задачи и попадала на полку 13
        self.assertEqual(pending["points"], [5, 5, 5])
        self.assertNotIn("sp", self.read())
        self.assertEqual(self.read().get("pending_sp"), None)

    def test_plan_scope_opens_plan_and_leaves_no_pending(self):
        calibrate.cmd_estimate_sp([8, 13, 21], "план 012", "plan")
        state = self.read()
        self.assertNotIn("pending", state)
        self.assertEqual(len(state["plans"]), 1)
        self.assertIsNone(state["plans"][0]["actual_seconds"])
        self.assertEqual(state["plans"][0]["points"], [8, 13, 21])

    def test_reports_which_shelves_are_interpolated(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calibrate.cmd_estimate_sp([5, 34], None, "turn")
        payload = json.loads(out.getvalue())
        # полка 5 измерена (три пробы), полка 34 достроена — оценку по ней
        # нельзя озвучивать как измеренную
        self.assertEqual(payload["interpolated_shelves"], [34])
        self.assertEqual(payload["weakest_n"], 0)


class TestRecord(JournalCase):
    def test_prediction_saved_from_table_before_this_record(self):
        self.write({"history": self.samples(3, 300, 3)})
        calibrate.cmd_record(3, 900, "медленная тройка")
        record = self.read()["history"][-1]
        # предсказание — 5 минут (полка до записи), а не 5..15 после подмешивания
        self.assertEqual(record["shelf_seconds"], 300)
        self.assertEqual(record["actual_seconds"], 900)

    def test_off_scale_value_snapped_with_raw_kept(self):
        self.write({"history": []})
        calibrate.cmd_record(11, 600, "вне шкалы")
        record = self.read()["history"][-1]
        self.assertEqual(record["sp"], 13)
        self.assertEqual(record["sp_raw"], 11.0)

    def test_seed_flag(self):
        self.write({"history": []})
        calibrate.cmd_record(3, 300, "задним числом", seed=True)
        self.assertTrue(self.read()["history"][-1]["seed"])
        calibrate.cmd_record(3, 300, "по факту")
        self.assertNotIn("seed", self.read()["history"][-1])


class TestRecordPlan(JournalCase):
    def test_closes_latest_open_plan(self):
        self.write({"history": self.samples(5, 600, 3)})
        calibrate.cmd_estimate_sp([5, 5], "план A", "plan")
        calibrate.cmd_estimate_sp([5, 5, 5], "план Б", "plan")
        calibrate.cmd_record_plan(None, 3000)
        plans = self.read()["plans"]
        self.assertIsNone(plans[0]["actual_seconds"])
        self.assertEqual(plans[1]["actual_seconds"], 3000)

    def test_closes_by_label_substring(self):
        self.write({"history": self.samples(5, 600, 3)})
        calibrate.cmd_estimate_sp([5], "план A", "plan")
        calibrate.cmd_estimate_sp([5], "план Б", "plan")
        calibrate.cmd_record_plan("план a", 1234)
        plans = self.read()["plans"]
        self.assertEqual(plans[0]["actual_seconds"], 1234)
        self.assertIsNone(plans[1]["actual_seconds"])

    def test_fails_when_nothing_open(self):
        self.write({"history": [], "plans": []})
        with self.assertRaises(SystemExit):
            calibrate.cmd_record_plan(None, 100)


class TestStats(JournalCase):
    def test_seed_and_live_counted_separately(self):
        import contextlib
        import io

        self.write({"history": [
            {"sp": 3, "actual_seconds": 300, "shelf_seconds": 300, "seed": True, "label": "з"},
            {"sp": 3, "actual_seconds": 600, "shelf_seconds": 300, "label": "ж"},
            {"sp": 5, "sp_points": [3, 3], "actual_seconds": 600,
             "shelf_seconds": 600, "label": "многозадачный"},
            {"sp": 45, "actual_seconds": 100, "shelf_seconds": 100, "label": "внемасштабный"},
        ]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calibrate.cmd_stats()
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["accuracy_seed"]["n"], 1)
        self.assertEqual(payload["accuracy_live"]["n"], 1)
        self.assertEqual(payload["accuracy_live"]["median"], 2.0)
        self.assertEqual(payload["accuracy_multitask_turns"]["n"], 1)
        self.assertEqual(len(payload["health"]["off_scale_ignored"]), 1)

    def test_ratio_summary_uses_geometric_mean(self):
        # промах вдвое вверх и вдвое вниз должны компенсироваться, а не давать 1.25
        summary = calibrate._ratio_summary([0.5, 2.0])
        self.assertEqual(summary["geomean"], 1.0)


class TestArgParsing(unittest.TestCase):
    def test_pop_option_handles_several_options(self):
        args = ["--sp", "5", "--actual", "600", "--label", "метка"]
        self.assertEqual(calibrate.pop_option(args, "--actual"), "600")
        self.assertEqual(calibrate.pop_option(args, "--sp"), "5")
        self.assertEqual(calibrate.pop_option(args, "--label"), "метка")
        self.assertEqual(args, [])

    def test_pop_option_absent(self):
        args = ["--sp", "5"]
        self.assertIsNone(calibrate.pop_option(args, "--scope"))
        self.assertEqual(args, ["--sp", "5"])

    def test_pop_flag(self):
        args = ["--seed", "текст"]
        self.assertTrue(calibrate.pop_flag(args, "--seed"))
        self.assertFalse(calibrate.pop_flag(args, "--seed"))
        self.assertEqual(args, ["текст"])



class TestStepsStore(unittest.TestCase):
    """Новая запись: факты о шагах вместо пар «озвучено / вышло».

    Старый TestStopHook проверял механизм, которого больше нет: Stop-хук
    считал погрешность между оценкой и фактом. От этого отказались —
    погрешность оказалась бесполезной, оцениваем сравнением с эталонами.
    """

    def setUp(self):
        import steps_store
        self.ss = steps_store
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._real_records = self.ss.RECORDS
        self._real_path = self.ss.STEPS_PATH
        self.ss.RECORDS = d / "records"
        self.ss.STEPS_PATH = self.ss.RECORDS / "steps.jsonl"

    def tearDown(self):
        self.ss.RECORDS = self._real_records
        self.ss.STEPS_PATH = self._real_path
        self.tmp.cleanup()

    def test_known_kind_records_only_reference_and_fact(self):
        refs = {"тесты": {"id": "STEP-007", "median_sec": 109}}
        step = {"sec": 120.0, "kind": "тесты", "started": "t", "host": "h",
                "files_edited": 0, "files_created": 0, "commands": 1,
                "phrase": "Прогоняю тесты — ~2 мин"}
        rec = self.ss.to_record(step, refs)
        self.assertEqual(rec["ref"], "STEP-007")
        self.assertEqual(rec["sec"], 120.0)
        # подробностей быть не должно: эталон уже их описывает
        self.assertNotIn("phrase", rec)

    def test_unknown_kind_keeps_details_that_affect_time(self):
        step = {"sec": 143.0, "kind": "прочее", "started": "t", "host": "h",
                "files_edited": 12, "files_created": 2, "commands": 3,
                "tools": {"Edit": 12}, "phrase": "Правлю модель — ~3 мин"}
        rec = self.ss.to_record(step, {})
        self.assertIsNone(rec["ref"])
        self.assertEqual(rec["scale"]["edited"], 12)
        self.assertEqual(rec["scale"]["created"], 2)
        self.assertIn("phrase", rec)

    def test_no_estimate_actual_pair_is_stored(self):
        rec = self.ss.to_record(
            {"sec": 10.0, "kind": "git", "started": "t", "host": "h",
             "files_edited": 0, "files_created": 0, "commands": 1},
            {"git": {"id": "STEP-001"}})
        for gone in ("estimated_seconds", "shelf_seconds", "buffer_seconds",
                     "sp", "measured"):
            self.assertNotIn(gone, rec)

    def test_append_and_fold_by_reference(self):
        self.ss.append([
            {"sec": 100, "kind": "тесты", "ref": "STEP-007"},
            {"sec": 120, "kind": "тесты", "ref": "STEP-007"},
            {"sec": 20, "kind": "git", "ref": "STEP-001"},
        ])
        folded = self.ss.by_ref()
        self.assertEqual(sorted(folded["STEP-007"]), [100, 120])
        self.assertEqual(folded["STEP-001"], [20])


class TestStepExtraction(unittest.TestCase):
    """Разбор транскрипта: две ловушки, на которых наивный парсер врёт."""

    def _write(self, entries):
        d = Path(self.tmp.name) / "t.jsonl"
        with io.open(d, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return d

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import extract_steps
        self.ex = extract_steps

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _asst(ts, blocks):
        return {"type": "assistant", "timestamp": ts,
                "message": {"content": blocks}}

    def test_tool_result_is_not_a_user_turn(self):
        """Результаты инструментов приходят как type=user.

        Если считать их репликой человека, каждый шаг рвётся на первом же
        вызове и длительности выходят по секунде.
        """
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Гоняю тесты — ~2 мин"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "pytest -q"}}]),
            {"type": "user", "timestamp": "2026-01-01T00:00:30Z",
             "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            self._asst("2026-01-01T00:02:00Z",
                       [{"type": "text", "text": "Коммичу — ~30 сек"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "git commit -m x"}}]),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["sec"], 120.0)
        # `pytest -q` без сужения — весь сьют; целевой прогон опознаётся по
        # маркеру, пути к файлу или -k
        self.assertEqual(steps[0]["kind"], "тесты: весь сьют")

    def test_targeted_tests_are_separate_from_full_suite(self):
        """Целевой прогон и весь сьют — разные виды: у сьюта хвост вдвое длиннее."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Гоняю целевые — ~2 мин"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "pytest -m \"not db\" -q"}}]),
            self._asst("2026-01-01T00:01:00Z",
                       [{"type": "text", "text": "Правлю файл — ~1 мин"},
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": "a.py"}}]),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps[0]["kind"], "тесты: целевые")

    def test_edits_win_over_incidental_grep(self):
        """42% шагов с правками содержат попутный grep — правки должны победить."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Правлю модуль — ~2 мин"},
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": "a.py"}},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "grep -n foo a.py"}}]),
            self._asst("2026-01-01T00:02:00Z",
                       [{"type": "text", "text": "Коммичу — ~30 сек"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "git commit -am x"}}]),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps[0]["kind"], "код: правки (1 файл)")

    def test_cd_prefix_is_stripped(self):
        """`cd ... &&` — навигация; без отсечения всё падало бы в «прочее»."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Гоняю миграции — ~1 мин"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "cd backend && alembic upgrade head"}}]),
            self._asst("2026-01-01T00:01:00Z",
                       [{"type": "text", "text": "Смотрю файл — ~30 сек"},
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "a.py"}}]),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps[0]["kind"], "бд: миграции")

    def test_real_user_message_cuts_the_measurement(self):
        """Пауза, пока пользователь отходил, не приписывается шагу."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Смотрю файл — ~30 сек"},
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "a.py"}}]),
            {"type": "user", "timestamp": "2026-01-01T00:40:00Z",
             "message": {"content": "продолжай"}},
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps, [])

    def test_tool_call_without_text_continues_the_step(self):
        p = self._write([
            self._asst("2026-01-01T00:00:00Z",
                       [{"type": "text", "text": "Читаю резолвер — ~1 мин"},
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "a.py"}}]),
            self._asst("2026-01-01T00:00:10Z",
                       [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": "b.py"}}]),
            self._asst("2026-01-01T00:01:00Z",
                       [{"type": "text", "text": "Правлю файл — ~1 мин"},
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": "a.py"}}]),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["sec"], 60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StoryFactsCase(JournalCase):
    """Факты по историям: размер, названный до работы, и время, ушедшее на неё.

    Зачем. Эталонная история — это история с зафиксированным фактом. Без записи
    пары «размер, сколько ушло» выбрать эталон потом не из чего: план говорит,
    сколько предполагалось, и молчит о том, сколько получилось.

    Боль истории здесь намеренно не дублируется: она живёт в плане и
    адресуется парой «план + номер». Копия разошлась бы с оригиналом при первой
    правке плана.
    """

    def test_открытая_история_записывается_с_размером(self):
        calibrate.cmd_story_start("024", "US-0043", 3.0, "Пикинг по штрихкоду")
        stories = journal_store.read_state().get("stories") or []
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["plan"], "024")
        self.assertEqual(stories[0]["us"], "US-0043")
        self.assertEqual(stories[0]["sp"], 3.0)
        self.assertIsNone(stories[0]["actual_seconds"])
        self.assertIn("shelf_seconds", stories[0])

    def test_закрытие_вписывает_факт_в_открытую_запись(self):
        calibrate.cmd_story_start("024", "US-0043", 3.0, "Пикинг")
        calibrate.cmd_story_close("US-0043", actual_seconds=1800)
        stories = journal_store.read_state()["stories"]
        self.assertEqual(len(stories), 1, "закрытие не должно плодить записи")
        self.assertEqual(stories[0]["actual_seconds"], 1800)

    def test_время_считается_само_если_факт_не_назван(self):
        calibrate.cmd_story_start("024", "US-0044", 2.0, "Вторая")
        calibrate.cmd_story_close("US-0044")
        story = journal_store.read_state()["stories"][0]
        self.assertIsNotNone(story["actual_seconds"])
        self.assertGreaterEqual(story["actual_seconds"], 0)

    def test_закрытие_несуществующей_истории_не_молчит(self):
        with self.assertRaises(SystemExit):
            calibrate.cmd_story_close("US-9999")

    def test_повторное_закрытие_не_затирает_факт(self):
        calibrate.cmd_story_start("024", "US-0045", 5.0, "Третья")
        calibrate.cmd_story_close("US-0045", actual_seconds=600)
        with self.assertRaises(SystemExit):
            calibrate.cmd_story_close("US-0045", actual_seconds=99)
        self.assertEqual(
            journal_store.read_state()["stories"][0]["actual_seconds"], 600)

    def test_половина_очка_не_округляется_до_целого(self):
        """0.5 SP — законный размер: он есть в шкале, и мелкая история должна
        оставаться мелкой, а не подтягиваться к 1."""
        calibrate.cmd_story_start("024", "US-0046", 0.5, "Совсем мелкая")
        self.assertEqual(journal_store.read_state()["stories"][0]["sp"], 0.5)

    def test_кандидаты_в_эталоны_это_закрытые_истории(self):
        calibrate.cmd_story_start("024", "US-0047", 3.0, "Закрытая")
        calibrate.cmd_story_close("US-0047", actual_seconds=1200)
        calibrate.cmd_story_start("024", "US-0048", 3.0, "Открытая")
        done = [s for s in journal_store.read_state()["stories"]
                if s["actual_seconds"] is not None]
        self.assertEqual([s["us"] for s in done], ["US-0047"])


PLAN_HEAD = "---\nproject: x\nstatus: draft\n---\n\n"


class StoryFactSourceCase(JournalCase):
    """Что должно пережить неделю: размер, названный до работы, и время.

    Стенные часы здесь годятся: простой между историями — это дефект работы, а
    не свойство измерения, и чинится он тем, что план выполняется без остановок
    (`execute-plan`). Вычитать паузы из факта значило бы чинить симптом.

    Когда работа действительно шла не подряд — человек прервал, машина
    перезагрузилась, — факт называется вручную, и это видно по источнику.
    """

    def test_размер_и_название_берутся_из_плана(self):
        """Через неделю я не вспомню ни размер, ни формулировку. В плане они
        записаны, и дублировать их руками значит рано или поздно разойтись."""
        plan = self.dir / "024-тест.md"
        plan.write_text(
            PLAN_HEAD
            + "## US-0054. Пикинг по штрихкоду — 5 SP\n\n"
            + "**Боль.** Я как кладовщик...\n", encoding="utf-8")
        calibrate.cmd_story_start("024", "US-0054", plan_path=plan)
        story = journal_store.read_state()["stories"][0]
        self.assertEqual(story["sp"], 5.0)
        self.assertEqual(story["title"], "Пикинг по штрихкоду")

    def test_история_без_размера_открывается_и_остаётся_без_полки(self):
        """`? SP` — законный размер, и запись факта из-за него не срывается.

        Раньше здесь был отказ, и он давал обратное задуманному: размер
        выдумывался на месте, только чтобы запись открылась, и в фонд ехало
        вымышленное предсказание. Факт без предсказания полезен — из таких и
        выросли эталоны плана 026; предсказание из воздуха бесполезно. Полка при
        неизвестном размере не считается: сравнивать нечего.
        """
        plan = self.dir / "024-тест.md"
        plan.write_text(
            PLAN_HEAD
            + "## US-0057. Без размера — ? SP\n\n**Боль.** ...\n",
            encoding="utf-8")
        calibrate.cmd_story_start("024", "US-0057", plan_path=plan)
        story = journal_store.read_state()["stories"][0]
        self.assertIsNone(story["sp"])
        self.assertIsNone(story["shelf_seconds"])
        self.assertEqual(story["title"], "Без размера")

    def test_названный_вручную_факт_помечен_источником(self):
        calibrate.cmd_story_start("024", "US-0053", 2.0, "Руками")
        calibrate.cmd_story_close("US-0053", actual_seconds=900)
        story = journal_store.read_state()["stories"][0]
        self.assertEqual(story["actual_seconds"], 900)
        self.assertEqual(story["source"], "manual")

    def test_обычное_закрытие_считает_по_часам(self):
        calibrate.cmd_story_start("024", "US-0058", 3.0, "Обычная")
        calibrate.cmd_story_close("US-0058")
        story = journal_store.read_state()["stories"][0]
        self.assertEqual(story["source"], "elapsed")
        self.assertIsNotNone(story["actual_seconds"])

    def test_незакрытые_истории_видно(self):
        """Незакрытая запись — потерянный замер, и молчать о ней нельзя."""
        calibrate.cmd_story_start("024", "US-0055", 3.0, "Забытая")
        calibrate.cmd_story_start("024", "US-0056", 3.0, "Тоже забытая")
        calibrate.cmd_story_close("US-0056", actual_seconds=60)
        self.assertEqual([s["us"] for s in calibrate.open_stories()],
                         ["US-0055"])

class TestOneRuler(JournalCase):
    """размер меряется одной линейкой, и видно какой.

    Линеек в системе было три, расходящихся вдвое: фонд замеренных историй
    (1 SP = 5.9 мин), восстановленная по зазорам между коммитами (2.8) и
    сторипоинты ХОДА, а не истории (4.2). Оценка всегда считалась по фонду, но
    `stats` печатал полки журнала без всякой пометки — и по ним же был сделан
    вывод, что оценка врёт вдвое. Ошибка стоила получаса разбора.
    """

    def setUp(self):
        super().setUp()
        # База отводит фонд в несуществующий файл, чтобы проверять арифметику
        # журнала. Здесь проверяется ровно обратное — что фонд главный, —
        # поэтому кладём свой, с известными числами.
        scale = self.dir / "sp-scale.json"
        scale.write_text(json.dumps({
            "shelves": [
                {"sp": 1, "minutes": [4.8, 6.9]},
                {"sp": 3, "minutes": [12.9, 19]},
                {"sp": 5, "minutes": [22.9, 33]},
                {"sp": 8, "minutes": None},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        calibrate.SP_SCALE_PATH = scale
        # Журнальные полки заведомо ниже фондовых: так видно, какая победила.
        self.write({"history": self.samples(5, 300, 12)})

    def run_json(self, fn, *args):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return json.loads(out.getvalue())

    def test_ответ_оценки_называет_источник(self):
        """Иначе нельзя проверить, по какой из линеек посчитано."""
        payload = self.run_json(calibrate.cmd_estimate_sp, [5], "одна", "turn")
        self.assertEqual(payload["minutes"], 27.9)
        self.assertEqual(payload["source"], "фонд эталонных историй")

    def test_выше_фонда_помечено_как_достроенное(self):
        """8 SP и выше — пучок осей; эталона там нет и не будет, и полка,
        достроенная от пятёрки, не должна выдавать себя за измеренную."""
        payload = self.run_json(calibrate.cmd_estimate_sp, [8], "крупная", "turn")
        self.assertEqual(payload["source"], "достроено от верхнего эталона")
        self.assertEqual([float(x) for x in payload["above_fund"]], [8.0])

    def test_полки_журнала_помечены_как_чужая_единица(self):
        """`stats` показывает полки, снятые с ходов и восстановленных задач.
        Для историй они не линейка, и путать их нельзя."""
        payload = self.run_json(calibrate.cmd_stats)
        self.assertEqual(payload["shelves_unit"], "размер хода, не истории")
        self.assertIn("не линейка историй", payload["shelves_warning"])

    def test_подтянутая_полка_названа(self):
        """enforce_monotone поднимает провалившуюся полку до предыдущей. Это
        честно, но молча — значит полка выглядит измеренной, не будучи ею."""
        rows = calibrate.enforce_monotone([
            {"sp": 3, "minutes": 10.0},
            {"sp": 5, "minutes": 4.0},
        ])
        self.assertTrue(rows[1].get("lifted"))

    def test_расхождение_линеек_видно_одной_командой(self):
        """Проверки, сходятся ли числа команды и фонда, не было вовсе."""
        payload = self.run_json(calibrate.cmd_rulers)
        self.assertTrue(payload["agree"])
        self.assertIn("fund", payload)
        self.assertIn("journal", payload)

class TestShelfEligibility(JournalCase):
    """что попадает в полки, решается по происхождению записи.

    Размер, проставленный без сравнения с эталоном, — не предсказание, а метка.
    Если пускать такие в полки, шкала догоняет собственную ошибку: раздутые
    очки тянут минуты на очко вниз, следующая оценка раздувается сильнее.
    Планы 030 и 034 дали 43 такие записи — факт/полка 0.19 и 0.27.
    """

    def test_размер_без_сравнения_полку_не_двигает(self):
        rejected = calibrate.shelf_rejection(
            {"sp": 8, "actual_seconds": 420, "sized_by": "guess"})
        self.assertEqual(rejected, "размер назван без сравнения с эталоном")

    def test_размер_сравнением_проходит(self):
        self.assertIsNone(calibrate.shelf_rejection(
            {"sp": 3, "actual_seconds": 900, "sized_by": "comparison"}))

    def test_старая_запись_без_пометки_проходит(self):
        """Правило смотрит вперёд: задним числом размеры не переставляются, и
        записи, сделанные до правила, полку двигать не перестают."""
        self.assertIsNone(calibrate.shelf_rejection(
            {"sp": 3, "actual_seconds": 900}))

    def test_выброс_по_длительности_отбрасывается(self):
        """В журнале лежит ход на 21 очко длиной 920 минут — сессия, оставленная
        открытой. Такая запись задаёт и центр полки, и её разброс."""
        rejected = calibrate.shelf_rejection(
            {"sp": 21, "actual_seconds": 920 * 60})
        self.assertEqual(rejected, "длительность вне правдоподобного диапазона")

    def test_отброшенные_видны_с_причиной(self):
        """Запись не теряется: она остаётся в журнале и названа причина."""
        self.write({"history": [
            {"sp": 3, "actual_seconds": 900, "label": "годная"},
            {"sp": 8, "actual_seconds": 420, "sized_by": "guess", "label": "на глаз"},
        ]})
        payload = self.run_json(calibrate.cmd_rulers)
        reasons = {r["label"]: r["reason"] for r in payload["rejected"]}
        self.assertEqual(reasons["на глаз"],
                         "размер назван без сравнения с эталоном")
        self.assertNotIn("годная", reasons)

    def run_json(self, fn, *args):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return json.loads(out.getvalue())



class TestSizedByComparison(JournalCase):
    """как размер получен — часть записи, а не устная договорённость.

    Без этого запись неотличима от размера на глаз, и полку двигают обе. А
    эталоном история стать не может вовсе: у эталона должно быть написано, с
    чем сравнивали.
    """

    PLAN = """---
project: demo
status: draft
---

# 099 — Проверочный

## US-0301. Со сравнением — 3 SP

**Боль.** Я как владелец хочу, чтобы размер был проверяем.

**Размер.** Ось G. Эталон — `references/stories/US-9007.md` (3 SP, 14.8 минуты):
мест два — проверка и её вызов.

**Порядок и зависимости.** Единственная.

## US-0302. Без сравнения — 3 SP

**Боль.** Я как владелец хочу увидеть разницу.

**Порядок и зависимости.** Единственная.

## US-0303. Эталон чужой оси — 3 SP

**Боль.** Я как владелец хочу, чтобы ось эталона совпадала с названной.

**Размер.** Ось K. Эталон — `references/stories/US-9007.md` (3 SP, 14.8 минуты):
мест два.

**Порядок и зависимости.** Единственная.

## US-0304. Места не посчитаны — 3 SP

**Боль.** Я как владелец хочу, чтобы сдвиг от эталона был назван.

**Размер.** Ось G. Эталон — `references/stories/US-9007.md` (3 SP, 14.8 минуты).

**Порядок и зависимости.** Единственная.

## US-0305. Эталона такого нет — 3 SP

**Боль.** Я как владелец хочу, чтобы ссылка вела в фонд.

**Размер.** Ось G. Эталон — `references/stories/US-9999.md`: мест два.

**Порядок и зависимости.** Единственная.
"""

    def plan_file(self):
        path = self.dir / "099-проверочный.md"
        path.write_text(self.PLAN, encoding="utf-8")
        return path

    def test_эталон_сравнения_попадает_в_запись(self):
        calibrate.cmd_story_start("099", "US-0301", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        self.assertEqual(story["sized_by"], "comparison")
        self.assertEqual(story["compared_to"], ["US-9007"])

    def test_эталон_чужой_оси_не_считается_сравнением(self):
        """Сравнение с эталоном другой оси меряет другую работу."""
        calibrate.cmd_story_start("099", "US-0303", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        self.assertEqual(story["sized_by"], "guess")

    def test_без_числа_мест_это_не_сравнение(self):
        """Эталон задаёт уровень, места — сдвиг от него; без них размер взят
        из эталона целиком."""
        calibrate.cmd_story_start("099", "US-0304", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        self.assertEqual(story["sized_by"], "guess")

    def test_ссылка_мимо_фонда_не_считается_сравнением(self):
        """Несуществующий эталон — опечатка или выдуманный номер."""
        calibrate.cmd_story_start("099", "US-0305", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        self.assertEqual(story["sized_by"], "guess")

    def test_размер_без_эталона_помечен_как_на_глаз(self):
        calibrate.cmd_story_start("099", "US-0302", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        self.assertEqual(story["sized_by"], "guess")
        self.assertEqual(story["compared_to"], [])

    def test_такая_запись_полку_не_двигает(self):
        """Прямая связка с US-0261: отбор смотрит на то же поле."""
        calibrate.cmd_story_start("099", "US-0302", plan_path=self.plan_file())
        story = self.read()["stories"][0]
        story["actual_seconds"] = 420
        self.assertEqual(calibrate.shelf_rejection(story),
                         "размер назван без сравнения с эталоном")


class TestStepMode(unittest.TestCase):
    """режим работы записан рядом с фактом.

    Без режима 2282 накопленных замера безымянны: сравнить уровни усилий
    между собой не из чего, хотя данные лежат в транскрипте и просто не
    доезжают до записи.
    """

    def _write(self, entries):
        d = Path(self.tmp.name) / "t.jsonl"
        with io.open(d, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return d

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import extract_steps
        self.ex = extract_steps

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _asst(ts, blocks, **extra):
        e = {"type": "assistant", "timestamp": ts,
             "message": {"content": blocks}}
        e.update(extra)
        return e

    @staticmethod
    def _step(text, cmd="ls"):
        return [{"type": "text", "text": text},
                {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]

    def test_режим_берётся_с_записи_открывшей_шаг(self):
        p = self._write([
            self._asst("2026-01-01T00:00:00Z", self._step("Смотрю — ~10 сек"),
                       effort="high",
                       message={"content": self._step("Смотрю — ~10 сек"),
                                "model": "claude-opus-5"}),
            self._asst("2026-01-01T00:01:00Z", self._step("Ещё — ~10 сек")),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps[0]["model"], "claude-opus-5")
        self.assertEqual(steps[0]["effort"], "high")

    def test_уровень_хода_важнее_уровня_сессии(self):
        """`perTurnEffort` — то, на чём ход реально шёл, а `effort` — настройка."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z", self._step("Смотрю — ~10 сек"),
                       effort="high", perTurnEffort="low"),
            self._asst("2026-01-01T00:01:00Z", self._step("Ещё — ~10 сек")),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertEqual(steps[0]["effort"], "low")

    def test_без_режима_поля_пустые_а_не_угаданные(self):
        """Узкое горлышко US-0279: замер без транскрипта режима не получает."""
        p = self._write([
            self._asst("2026-01-01T00:00:00Z", self._step("Смотрю — ~10 сек")),
            self._asst("2026-01-01T00:01:00Z", self._step("Ещё — ~10 сек")),
        ])
        steps = self.ex.steps_from_transcript(p)
        self.assertIsNone(steps[0]["model"])
        self.assertIsNone(steps[0]["effort"])

    def test_режим_доезжает_до_записи_журнала(self):
        import steps_store
        step = {"sec": 12.0, "kind": "git: состояние", "started": "t",
                "host": "h", "files_edited": 0, "files_created": 0,
                "commands": 1, "model": "claude-opus-5", "effort": "high",
                "session": "abc"}
        rec = steps_store.to_record(step, {"git: состояние": {"id": "STEP-001"}})
        self.assertEqual(rec["model"], "claude-opus-5")
        self.assertEqual(rec["effort"], "high")
        self.assertEqual(rec["session"], "abc")

    def test_единица_хода_доезжает_до_записи(self):
        import steps_store
        rec = steps_store.to_record(
            {"sec": 12.0, "kind": "чтение файлов", "started": "t", "host": "h",
             "files_edited": 0, "files_created": 0, "commands": 0,
             "model": "cursor", "unit": "turn"},
            {"чтение файлов": {"id": "STEP-019"}})
        self.assertEqual(rec["unit"], "turn")

    def test_запись_без_режима_не_несёт_пустых_полей(self):
        """Пустое поле — не то же, что отсутствие: в журнале различаем."""
        import steps_store
        rec = steps_store.to_record(
            {"sec": 12.0, "kind": "git: состояние", "started": "t", "host": "h",
             "files_edited": 0, "files_created": 0, "commands": 1},
            {"git: состояние": {"id": "STEP-001"}})
        self.assertNotIn("model", rec)
        self.assertNotIn("effort", rec)


class TestModesSummary(unittest.TestCase):
    """сводка по режимам с явной долей нераспознанных."""

    def test_считает_по_уровням_и_отделяет_восстановленные(self):
        rows = calibrate.modes_summary([
            {"sec": 10, "effort": "high", "model": "claude-opus-5"},
            {"sec": 20, "effort": "high", "model": "claude-opus-5",
             "restored": True},
            {"sec": 30, "effort": "low", "model": "claude-opus-5",
             "restored": True},
            {"sec": 40},
        ])
        high = next(r for r in rows["modes"] if r["effort"] == "high")
        self.assertEqual(high["n"], 2)
        self.assertEqual(high["restored"], 1)
        self.assertEqual(rows["unknown"]["n"], 1)

    def test_доля_нераспознанных_названа_числом(self):
        """Молчание тут хуже пропуска: по нему не понять, можно ли доверять."""
        rows = calibrate.modes_summary([
            {"sec": 10, "effort": "high"},
            {"sec": 10},
            {"sec": 10},
            {"sec": 10},
        ])
        self.assertEqual(rows["unknown"]["share_pct"], 75.0)

    def test_пустой_журнал_не_делит_на_ноль(self):
        rows = calibrate.modes_summary([])
        self.assertEqual(rows["total"], 0)
        self.assertEqual(rows["unknown"]["share_pct"], 0.0)


class TestEstimateIgnoresMode(unittest.TestCase):
    """Режим пишется рядом с оценкой, но числа не двигает.

    Поправка на режим снята 26.09.2026: смену режима, которая держится, ловит
    версия эталонов, а разовое повышение уровня — законная часть выборки.
    """

    def test_оценка_не_зависит_от_режима_сессии(self):
        import contextlib
        results = []
        for effort in ("high", "xhigh", "medium"):
            out = io.StringIO()
            with mock.patch.object(calibrate, "current_effort",
                                   return_value=(effort, "транскрипт")),                     contextlib.redirect_stdout(out):
                calibrate.cmd_estimate_sp([3], "одна", "turn")
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["mode"]["effort"], effort)
            self.assertNotIn("factor", payload["mode"])
            results.append(payload["minutes"])
        self.assertEqual(len(set(results)), 1)

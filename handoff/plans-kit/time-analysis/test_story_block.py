#!/usr/bin/env python3
"""Состояние «заблокировано» у истории.

Предохранитель плана умел два состояния: закрыта или нет. «Не закрыта, и
закрыть её нечем из доступного агенту» он читал как «ленится» и держал ход —
24.09.2026 это дало полтора десятка холостых оборотов.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibrate as C  # noqa: E402


class BlockingNeedsAReason(unittest.TestCase):
    """Без причины «заблокировано» неотличимо от «не захотелось»."""

    def test_the_kinds_are_a_closed_list(self) -> None:
        self.assertEqual(C.BLOCK_KINDS, ("environment", "owner", "collision"))

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as e:
            C.cmd_story_block("US-9999", kind="устал", why="просто")
        self.assertIn("устал", str(e.exception))

    def test_an_empty_reason_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as e:
            C.cmd_story_block("US-9999", kind="owner", why="   ")
        self.assertIn("причин", str(e.exception).lower())


class BlockingAnOpenStory(unittest.TestCase):
    def setUp(self) -> None:
        # Подменяется чтение и запись состояния, а не путь: где журнал лежит —
        # дело journal_store, и тесту туда заглядывать незачем.
        self.state = {"stories": [{
            "plan": "065", "us": "US-0446", "sp": 2.0, "title": "х",
            "started": "2026-09-24T07:00:00+00:00",
            "finished": None, "actual_seconds": None, "shelf_seconds": 546,
        }]}
        self._read, self._write = C.read_state, C.write_state
        C.read_state = lambda: self.state
        C.write_state = lambda data: self.state.update(data)
        self.addCleanup(lambda: (setattr(C, "read_state", self._read),
                                 setattr(C, "write_state", self._write)))

    def record(self) -> dict:
        return self.state["stories"][0]

    def test_a_missing_open_story_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            C.cmd_story_block("US-0001", kind="owner", why="нет такой")

    def test_the_record_carries_kind_and_reason(self) -> None:
        with redirect_stdout(io.StringIO()):
            C.cmd_story_block("US-0446", kind="owner", why="нужна выдача прав")
        r = self.record()
        self.assertEqual(r["blocked"]["kind"], "owner")
        self.assertEqual(r["blocked"]["why"], "нужна выдача прав")

    def test_the_story_stops_being_open(self) -> None:
        """Иначе предохранитель будет держать ход дальше."""
        with redirect_stdout(io.StringIO()):
            C.cmd_story_block("US-0446", kind="owner", why="нужна выдача прав")
        self.assertIsNotNone(self.record()["finished"])

    def test_the_time_is_not_taken_as_a_fact(self) -> None:
        """Заблокированная история ничего не измерила — в полки её нельзя."""
        with redirect_stdout(io.StringIO()):
            C.cmd_story_block("US-0446", kind="environment", why="Self-Modification")
        r = self.record()
        self.assertIsNone(r["actual_seconds"])
        self.assertEqual(r["source"], "blocked")

    def test_it_says_out_loud_what_happened(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            C.cmd_story_block("US-0446", kind="environment", why="Self-Modification")
        said = json.loads(buf.getvalue())
        self.assertEqual(said["story_blocked"], "US-0446")
        self.assertEqual(said["kind"], "environment")
        self.assertIn("Self-Modification", said["why"])

    def test_blocking_twice_is_refused(self) -> None:
        with redirect_stdout(io.StringIO()):
            C.cmd_story_block("US-0446", kind="owner", why="раз")
        with self.assertRaises(SystemExit):
            C.cmd_story_block("US-0446", kind="owner", why="два")


class BlockedDoesNotStandInTheWay(unittest.TestCase):
    """Снятая блокировка — это новое предсказание, а не продолжение прежнего."""

    def setUp(self) -> None:
        self.state = {"stories": [{
            "plan": "065", "us": "US-0446", "sp": 2.0, "title": "х",
            "started": "2026-09-24T07:00:00+00:00",
            "finished": "2026-09-24T09:00:00+00:00", "actual_seconds": None,
            "source": "blocked", "blocked": {"kind": "owner", "why": "ждёт"},
        }]}
        self._read, self._write = C.read_state, C.write_state
        C.read_state = lambda: self.state
        C.write_state = lambda data: self.state.update(data)
        self.addCleanup(lambda: (setattr(C, "read_state", self._read),
                                 setattr(C, "write_state", self._write)))

    def test_a_blocked_story_may_be_started_again(self) -> None:
        with redirect_stdout(io.StringIO()):
            C.cmd_story_start("065", "US-0446", sp=2.0, title="х")
        self.assertEqual(len(self.state["stories"]), 2)
        self.assertIsNone(self.state["stories"][-1]["finished"])

    def test_the_blocked_record_is_left_alone(self) -> None:
        """Прежняя запись остаётся с причиной: история блокировки не стирается."""
        with redirect_stdout(io.StringIO()):
            C.cmd_story_start("065", "US-0446", sp=2.0, title="х")
        self.assertEqual(self.state["stories"][0]["blocked"]["why"], "ждёт")


class ShelvesIgnoreBlocked(unittest.TestCase):
    def test_a_blocked_record_is_rejected_from_shelves(self) -> None:
        self.assertTrue(C.shelf_rejection(
            {"sized_by": "comparison", "blocked": {"kind": "owner", "why": "x"},
             "actual_seconds": None, "sp": 2.0}))


class IdleTimeIsNotWork(unittest.TestCase):
    """Факт, во много раз больше полки, — это простой, а не медленная работа.

    24.09.2026 US-0443 простояла открытой с утра, пока ждала решения владельца,
    и закрылась с фактом 91.7 минуты при полке 9.1 — отношение 10. Абсолютная
    граница правдоподобия (240 минут) такое пропускает, и выброс поехал в полки.
    """

    def test_the_factor_is_marked_provisional(self) -> None:
        self.assertTrue(C.SHELF_MAX_RATIO_PROVISIONAL)

    def test_a_tenfold_overrun_is_rejected(self) -> None:
        said = C.shelf_rejection({"sized_by": "comparison", "sp": 2.0,
                                  "actual_seconds": 5503, "shelf_seconds": 546})
        self.assertIsNotNone(said)
        self.assertIn("простой", said.lower())

    def test_an_ordinary_overrun_still_counts(self) -> None:
        """Промах оценки вдвое — это факт, а не артефакт измерения."""
        self.assertIsNone(C.shelf_rejection(
            {"sized_by": "comparison", "sp": 2.0,
             "actual_seconds": 1100, "shelf_seconds": 546}))

    def test_being_much_faster_still_counts(self) -> None:
        """Снизу порога нет: быстро — это настоящий факт, и он нужен шкале."""
        self.assertIsNone(C.shelf_rejection(
            {"sized_by": "comparison", "sp": 2.0,
             "actual_seconds": 66, "shelf_seconds": 546}))

    def test_a_named_fact_is_trusted(self) -> None:
        """Названный вручную факт означает, что паузы уже вычтены человеком."""
        self.assertIsNone(C.shelf_rejection(
            {"sized_by": "comparison", "sp": 2.0, "source": "manual",
             "actual_seconds": 5503, "shelf_seconds": 546}))


if __name__ == "__main__":
    unittest.main(verbosity=1)

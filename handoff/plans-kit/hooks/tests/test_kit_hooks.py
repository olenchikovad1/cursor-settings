"""Проверка хуков набора после распаковки.

    py -X utf8 ~/.cursor/hooks/tests/test_kit_hooks.py

Запускается в настоящем ~/.cursor: хуки читают эталоны и пишут состояние хода
туда же, куда и в работе. После прогона временные записи убираются.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent
CURSOR = Path.home() / ".cursor"


def run(script: str, payload: dict, *args: str, bom: bool = True) -> dict:
    # Cursor на Windows подаёт JSON с UTF-8 BOM (EF BB BF); хук обязан
    # разбирать и такой ввод, иначе запрос пустой и ход не меряется.
    raw = json.dumps(payload).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOKS / script), *args],
        input=raw, capture_output=True, env=dict(os.environ))
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return json.loads(p.stdout.decode("utf-8"))


class TimeEstimate(unittest.TestCase):
    def test_памятка_с_эталонами_приходит_на_старте_сессии(self) -> None:
        answer = run("time_estimate.py", {"conversation_id": "kit-start"}, "sessionStart")
        self.assertIn("additional_context", answer)

    def test_ход_с_действиями_без_строки_оценки_получает_замечание(self) -> None:
        turn = CURSOR / "time-analysis" / "turns" / "kit-time.json"
        turn.unlink(missing_ok=True)
        try:
            start = run("time_estimate.py",
                        {"conversation_id": "kit-time", "generation_id": "g"},
                        "beforeSubmitPrompt")
            self.assertTrue(start.get("continue"))
            self.assertTrue(turn.is_file(), "состояние хода не записано")
            run("time_estimate.py",
                {"conversation_id": "kit-time", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "kit-time", "text": "Сделала правку."},
                "afterAgentResponse")
            # ход короче MIN_SECONDS в журнал не пишется — тест журнал не засоряет
            stop = run("time_estimate.py",
                       {"conversation_id": "kit-time", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)

    def test_оценка_из_транскрипта_видна(self) -> None:
        # afterAgentResponse отдаёт только итог хода; строки оценки — в транскрипте
        cid = "kit-tr"
        tdir = CURSOR / "projects" / "kit-hook-proj" / "agent-transcripts" / cid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{cid}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"role": "user", "message": {"content": [{"type": "text", "text": "сделай"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "Читаю хук — ~20 сек (чтение×1)."},
                {"type": "tool_use", "name": "Read", "input": {}}]}},
        ]) + "\n", encoding="utf-8")
        turn = CURSOR / "time-analysis" / "turns" / f"{cid}.json"
        try:
            run("time_estimate.py", {"conversation_id": cid, "generation_id": "g"},
                "beforeSubmitPrompt")
            run("time_estimate.py", {"conversation_id": cid, "tool_name": "Read",
                                     "tool_input": {}}, "postToolUse")
            run("time_estimate.py", {"conversation_id": cid, "text": "Готово."},
                "afterAgentResponse")
            stop = run("time_estimate.py", {"conversation_id": cid, "status": "completed",
                                            "loop_count": 0}, "stop")
            self.assertNotIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)
            import shutil
            shutil.rmtree(tdir.parent.parent, ignore_errors=True)

    def test_оценка_в_тексте_с_битой_кодировкой_видна(self) -> None:
        # Cursor на Windows отдаёт текст ответа как UTF-8, прочитанный в cp1251
        good = "Иду в хук — ~20 сек (чтение×1)."
        broken = "".join(
            bytes([b]).decode("cp1251", errors="replace")
            if bytes([b]).decode("cp1251", errors="replace") != "\ufffd" else chr(b)
            for b in good.encode("utf-8"))
        turn = CURSOR / "time-analysis" / "turns" / "kit-moji.json"
        turn.unlink(missing_ok=True)
        try:
            run("time_estimate.py",
                {"conversation_id": "kit-moji", "generation_id": "g"},
                "beforeSubmitPrompt")
            run("time_estimate.py",
                {"conversation_id": "kit-moji", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "kit-moji", "text": broken},
                "afterAgentResponse")
            stop = run("time_estimate.py",
                       {"conversation_id": "kit-moji", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertNotIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)

    def test_ранняя_оценка_не_затирается_поздним_абзацем(self) -> None:
        turn = CURSOR / "time-analysis" / "turns" / "kit-frag.json"
        turn.unlink(missing_ok=True)
        try:
            run("time_estimate.py",
                {"conversation_id": "kit-frag", "generation_id": "g"},
                "beforeSubmitPrompt")
            run("time_estimate.py",
                {"conversation_id": "kit-frag",
                 "text": "Читаю хук — ~20 сек (чтение×1)."},
                "afterAgentResponse")
            run("time_estimate.py",
                {"conversation_id": "kit-frag", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "kit-frag", "text": "В хуке текст ответа затирается."},
                "afterAgentResponse")
            stop = run("time_estimate.py",
                       {"conversation_id": "kit-frag", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertNotIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)


class LintPlan(unittest.TestCase):
    def test_шаблон_плана_хук_пропускает_молча(self) -> None:
        # файлы с подчёркиванием — не планы; хук на них ничего не печатает
        template = CURSOR / "plans" / "_TEMPLATE.md"
        raw = b"\xef\xbb\xbf" + json.dumps(
            {"tool_name": "Write", "tool_input": {"path": str(template)}}).encode()
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(HOOKS / "lint_plan.py"), "--hook"],
            input=raw, capture_output=True, env=dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout.strip(), b"")

    def test_история_без_эталона_получает_замечание(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "001-test-plan.md"
            plan.write_text(
                "---\nproject: demo\nstatus: draft\nbase_branch: main\nbranch: null\n"
                "depends_on: []\ncreated: 2026-10-01\nstories: 1\nsp_total: 3\n"
                "minutes_total: 20\n---\n\n# 001 — Проверка\n\nПлан для теста.\n\n---\n\n"
                "## US-0001. История без размера по эталону — 3 SP\n\n"
                "**Боль.** Я как тестировщик хочу видеть замечание валидатора.\n\n"
                "**Порядок и зависимости.** Независима.\n\n"
                "**Критерии приёмки.**\n\n- Замечание есть.\n\n"
                "**Порядок демонстрации.**\n\n1. Запустить валидатор.\n",
                encoding="utf-8")
            p = subprocess.run(
                [sys.executable, "-X", "utf8", str(HOOKS / "lint_plan.py"), str(plan)],
                capture_output=True)
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            self.assertTrue(p.returncode != 0 or out.strip(),
                            "валидатор промолчал про историю без эталона")


if __name__ == "__main__":
    unittest.main()

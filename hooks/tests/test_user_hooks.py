import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent


def run(script: str, payload: dict, *args: str, bom: bool = False) -> dict:
    env = {**os.environ, "USERPROFILE": os.environ["USERPROFILE"]}
    # Cursor на Windows подаёт JSON с UTF-8 BOM (EF BB BF). Хук, читающий
    # stdin как чистый utf-8, получает ошибку разбора и пустой запрос.
    raw = json.dumps(payload).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOKS / script), *args],
        input=raw, capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return json.loads(p.stdout.decode("utf-8"))


class Bom(unittest.TestCase):
    """Запрос с BOM обязан разбираться так же, как без него: 02.10.2026 из-за
    BOM предохранитель всё разрешал, а журнал шагов не пополнялся вовсе."""

    def test_guard_читает_запрос_с_bom(self) -> None:
        answer = run("guard_destructive.py",
                     {"command": "git push --force origin main"}, bom=True)
        self.assertEqual(answer["permission"], "deny")

    def test_compact_читает_запрос_с_bom(self) -> None:
        answer = run("compact_hint.py", {"is_first_compaction": False}, bom=True)
        self.assertIn("Новый чат", answer["user_message"])

    def test_time_estimate_пишет_состояние_хода_с_bom(self) -> None:
        turn = Path.home() / ".cursor" / "time-analysis" / "turns" / "t-bom.json"
        turn.unlink(missing_ok=True)
        try:
            run("time_estimate.py",
                {"conversation_id": "t-bom", "generation_id": "g"},
                "beforeSubmitPrompt", bom=True)
            self.assertTrue(turn.is_file(), "состояние хода не записано — запрос не разобран")
        finally:
            turn.unlink(missing_ok=True)


class Guard(unittest.TestCase):
    def test_force_push_в_общую_запрещён(self) -> None:
        answer = run("guard_destructive.py", {"command": "git push --force origin main"})
        self.assertEqual(answer["permission"], "deny")

    def test_миграция_спрашивает(self) -> None:
        answer = run("guard_destructive.py", {"command": "uv run alembic upgrade head"})
        self.assertEqual(answer["permission"], "ask")

    def test_обычный_push_разрешён(self) -> None:
        answer = run("guard_destructive.py", {"command": "git push -u origin HEAD"})
        self.assertEqual(answer["permission"], "allow")

    def test_сброс_файла_разрешён_а_жёсткий_спрашивает(self) -> None:
        soft = run("guard_destructive.py", {"command": "git reset -q -- CLAUDE.md"})
        self.assertEqual(soft["permission"], "allow")
        hard = run("guard_destructive.py", {"command": "git reset --hard HEAD"})
        self.assertEqual(hard["permission"], "ask")

    def test_удаление_временного_разрешено_а_от_корня_нет(self) -> None:
        tmp = run("guard_destructive.py",
                  {"command": "Remove-Item -Recurse -Force .\\tmp"})
        self.assertEqual(tmp["permission"], "allow")
        root = run("guard_destructive.py",
                   {"command": "Remove-Item -Recurse C:\\"})
        self.assertEqual(root["permission"], "deny")


class Compact(unittest.TestCase):
    def test_второе_сжатие_зовёт_новый_чат(self) -> None:
        answer = run("compact_hint.py", {"is_first_compaction": False})
        self.assertIn("Новый чат", answer["user_message"])

    def test_первое_не_зовёт(self) -> None:
        answer = run("compact_hint.py", {"is_first_compaction": True})
        self.assertNotIn("уже не первое", answer["user_message"])


class Time(unittest.TestCase):
    def test_ход_без_оценки_напоминает(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_home = Path(tmp)
            # хук пишет журнал в настоящий ~/.cursor; здесь проверяется только ответ
            start = run("time_estimate.py",
                        {"conversation_id": "t-time", "generation_id": "g"},
                        "beforeSubmitPrompt")
            self.assertTrue(start.get("continue"))
            run("time_estimate.py",
                {"conversation_id": "t-time", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "t-time", "text": "Сделала правку."},
                "afterAgentResponse")
            stop = run("time_estimate.py",
                       {"conversation_id": "t-time", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertIn("без строки оценки", stop.get("followup_message", ""))

    def test_оценка_из_транскрипта_видна(self) -> None:
        """afterAgentResponse отдаёт только итоговое сообщение хода; строки
        оценки перед вызовами лежат в транскрипте Cursor."""
        cid = "t-tr"
        tdir = (Path.home() / ".cursor" / "projects" / "t-hook-proj"
                / "agent-transcripts" / cid)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{cid}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"role": "user", "message": {"content": [{"type": "text", "text": "сделай"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "Читаю хук — ~20 сек (чтение×1)."},
                {"type": "tool_use", "name": "Read", "input": {}}]}},
        ]) + "\n", encoding="utf-8")
        turn = Path.home() / ".cursor" / "time-analysis" / "turns" / f"{cid}.json"
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
        """02.10.2026 Cursor отдавал текст ответа как UTF-8, прочитанный в
        cp1251 («РџСЂРёРЅСЏС‚Рѕ»), и строка оценки не находилась никогда."""
        good = "Иду в хук — ~20 сек (чтение×1)."
        broken = "".join(
            bytes([b]).decode("cp1251", errors="replace")
            if bytes([b]).decode("cp1251", errors="replace") != "\ufffd" else chr(b)
            for b in good.encode("utf-8"))
        turn = Path.home() / ".cursor" / "time-analysis" / "turns" / "t-moji.json"
        turn.unlink(missing_ok=True)
        try:
            run("time_estimate.py",
                {"conversation_id": "t-moji", "generation_id": "g"},
                "beforeSubmitPrompt")
            run("time_estimate.py",
                {"conversation_id": "t-moji", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "t-moji", "text": broken},
                "afterAgentResponse")
            saved = json.loads(turn.read_text(encoding="utf-8"))["text"]
            self.assertEqual(saved, good)
            stop = run("time_estimate.py",
                       {"conversation_id": "t-moji", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertNotIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)

    def test_ранняя_оценка_не_затирается_поздним_абзацем(self) -> None:
        """afterAgentResponse приходит кусками. Итог хода без строки оценки
        не должен стирать кусок, где оценка уже была."""
        turn = Path.home() / ".cursor" / "time-analysis" / "turns" / "t-frag.json"
        turn.unlink(missing_ok=True)
        try:
            run("time_estimate.py",
                {"conversation_id": "t-frag", "generation_id": "g"},
                "beforeSubmitPrompt")
            run("time_estimate.py",
                {"conversation_id": "t-frag",
                 "text": "Читаю хук — ~20 сек (чтение×1)."},
                "afterAgentResponse")
            run("time_estimate.py",
                {"conversation_id": "t-frag", "tool_name": "Read", "tool_input": {}},
                "postToolUse")
            run("time_estimate.py",
                {"conversation_id": "t-frag", "text": "В хуке текст ответа затирается."},
                "afterAgentResponse")
            stop = run("time_estimate.py",
                       {"conversation_id": "t-frag", "status": "completed", "loop_count": 0},
                       "stop")
            self.assertNotIn("без строки оценки", stop.get("followup_message", ""))
        finally:
            turn.unlink(missing_ok=True)


class ClaudeUntouched(unittest.TestCase):
    def test_версия_клода_не_сдвинулась(self) -> None:
        data = json.loads((Path.home() / ".claude" / "references" / "versions.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([v["n"] for v in data["versions"]], [1, 2])
        cursor = json.loads((Path.home() / ".cursor" / "references" / "versions.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(cursor["versions"][-1]["reason"], "переход на курсор")


if __name__ == "__main__":
    unittest.main()

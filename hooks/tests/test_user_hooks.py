import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent


def run(script: str, payload: dict, *args: str) -> dict:
    env = {**os.environ, "USERPROFILE": os.environ["USERPROFILE"]}
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOKS / script), *args],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


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

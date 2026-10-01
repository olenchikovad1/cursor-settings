"""Установка в проект и работа установленных хуков оттуда, как их вызовет
Cursor: относительным путём из корня проекта.

    py -X utf8 -m unittest discover -s ~/.cursor/project_skills/hooks/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[2]
INSTALL = LIB / "install.py"


def run(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", *args], cwd=cwd, input=stdin,
                          capture_output=True, text=True, encoding="utf-8")


class Install(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "p"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.project, check=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, *args: str) -> str:
        p = run(LIB, str(INSTALL), str(self.project), *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def hook(self, event: str, payload: dict) -> dict:
        request = {"conversation_id": "t", "generation_id": "g", "hook_event_name": event,
                   "workspace_roots": [str(self.project)], **payload}
        env_state = Path(self._tmp.name) / "state"
        env_state.mkdir(exist_ok=True)
        p = subprocess.run([sys.executable, "-X", "utf8", ".cursor/hooks/rails.py", event],
                           cwd=self.project, input=json.dumps(request), capture_output=True,
                           text=True, encoding="utf-8",
                           env={**os.environ, "TMP": str(env_state), "TEMP": str(env_state)})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_план_ничего_не_меняет(self) -> None:
        self.assertIn("Это план", self.install("--area", "process"))
        self.assertFalse((self.project / ".cursor").exists())

    def test_установка_раскладывает_скиллы_хуки_и_настройки(self) -> None:
        self.install("--only", "process-git,quality-verification", "--apply")
        index = (self.project / ".cursor/skills/process-git/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: process-git", index)
        self.assertIn("adapted:", index)
        self.assertIn("## Отступления в этом проекте", index)
        self.assertTrue((self.project / ".cursor/skills/process-git/branches.md").is_file())
        hooks = json.loads((self.project / ".cursor/hooks.json").read_text(encoding="utf-8"))
        self.assertIn("stop", hooks["hooks"])
        rails = json.loads((self.project / ".cursor/rails.json").read_text(encoding="utf-8"))
        self.assertEqual(rails["shared_branches"], ["main", "master", "develop"])
        self.assertNotIn("skill_areas", rails)

    def test_повторная_установка_ничего_не_дублирует(self) -> None:
        self.install("--area", "process", "--apply")
        self.assertIn("всё уже на месте", self.install("--area", "process", "--apply"))
        hooks = json.loads((self.project / ".cursor/hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(hooks["hooks"]["beforeShellExecution"]), 1)

    def test_правленая_копия_не_заменяется_и_показывается(self) -> None:
        self.install("--only", "process-git", "--apply")
        leaf = self.project / ".cursor/skills/process-git/commits.md"
        leaf.write_text(leaf.read_text(encoding="utf-8") + "\nПроектный нюанс.\n", encoding="utf-8")
        out = self.install("--only", "process-git", "--apply")
        self.assertIn("ОТЛИЧАЮТСЯ", out)
        self.assertIn("commits.md", out)
        self.assertIn("Проектный нюанс.", leaf.read_text(encoding="utf-8"))

    def test_чужие_записи_hooks_json_сохраняются(self) -> None:
        (self.project / ".cursor").mkdir()
        (self.project / ".cursor/hooks.json").write_text(json.dumps(
            {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}), encoding="utf-8")
        self.install("--only", "quality-verification", "--apply")
        hooks = json.loads((self.project / ".cursor/hooks.json").read_text(encoding="utf-8"))
        self.assertEqual([e["command"] for e in hooks["hooks"]["stop"]][0], "./mine.sh")
        self.assertEqual(len(hooks["hooks"]["stop"]), 2)

    def test_установленные_хуки_работают_из_корня_проекта(self) -> None:
        self.install("--only", "process-git,quality-verification,quality-review", "--apply")
        start = self.hook("sessionStart", {})
        self.assertIn("process-git", start.get("additional_context", ""))
        push = self.hook("beforeShellExecution", {"command": "git push origin main",
                                                  "cwd": str(self.project)})
        self.assertEqual(push["permission"], "ask")
        self.hook("afterAgentResponse", {"text": "Готово."})
        stop = self.hook("stop", {"status": "completed", "loop_count": 0})
        self.assertIn("followup_message", stop)


if __name__ == "__main__":
    unittest.main()

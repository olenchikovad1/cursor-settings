"""Тесты хуков библиотеки под Cursor. Запуск:

    py -X utf8 -m unittest discover -s ~/.cursor/project_skills/hooks/tests -v

Проверяется поведение, а не устройство: на вход идёт то, что Cursor присылает
хуку, на выходе смотрится ответ. Хук стоит между человеком и командой: ошибка
стоит либо пропущенного пуша в общую ветку, либо остановки на каждой второй
команде — а мешающие остановки выключают вместе с проверкой.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent
RAILS = HOOKS / "rails.py"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.conversation = "conv-1"
        self.generation = "gen-1"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_hook(self, event: str, payload: dict) -> dict:
        request = {
            "conversation_id": self.conversation,
            "generation_id": self.generation,
            "hook_event_name": event,
            "workspace_roots": [str(self.root)],
            **payload,
        }
        env = {**os.environ, "TMP": str(self.state), "TEMP": str(self.state),
               "TMPDIR": str(self.state), "HOME": str(self.base / "home"),
               "USERPROFILE": str(self.base / "home")}
        env.pop("CURSOR_PROJECT_DIR", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.pop("CURSOR_NO_AUTO_PUSH", None)
        env.pop("CLAUDE_NO_AUTO_PUSH", None)
        p = subprocess.run([sys.executable, "-X", "utf8", str(RAILS), event],
                           input=json.dumps(request), capture_output=True, text=True,
                           encoding="utf-8", env=env, cwd=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("упал", p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def shell_before(self, command: str) -> dict:
        return self.run_hook("beforeShellExecution", {"command": command, "cwd": str(self.root)})

    def shell_after(self, command: str, exit_code: int = 0) -> dict:
        return self.run_hook("postToolUse", {
            "tool_name": "Shell", "cwd": str(self.root),
            "tool_input": {"command": command},
            "tool_output": json.dumps({"exitCode": exit_code, "stdout": ""}),
        })

    def rails(self, folder: str = ".cursor", **settings) -> None:
        (self.root / folder).mkdir(exist_ok=True)
        (self.root / folder / "rails.json").write_text(json.dumps(settings), encoding="utf-8")

    def init_repo(self, branch: str = "main") -> Path:
        remote = self.base / "remote.git"
        git(self.base, "init", "--bare", "-q", str(remote))
        git(self.root, "init", "-q", "-b", branch)
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        git(self.root, "remote", "add", "origin", str(remote))
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        git(self.root, "add", "a.txt")
        git(self.root, "commit", "-q", "-m", "init")
        return remote


class SharedBranchPush(Case):
    def test_пуш_в_общую_ветку_спрашивает_человека(self) -> None:
        for command in ("git push origin main", "git push origin HEAD:develop",
                        "git add -A && git push -u origin +master"):
            self.assertEqual(self.shell_before(command)["permission"], "ask", command)

    def test_ветка_задачи_и_упоминание_пропускаются(self) -> None:
        for command in ("git push origin feature/x", "echo 'git push origin main'", "ls"):
            self.assertEqual(self.shell_before(command)["permission"], "allow", command)

    def test_общие_ветки_берутся_из_rails(self) -> None:
        self.rails(shared_branches=["prod"])
        self.assertEqual(self.shell_before("git push origin main")["permission"], "allow")
        self.assertEqual(self.shell_before("git push origin prod")["permission"], "ask")

    def test_настройка_claude_читается_когда_нет_своей(self) -> None:
        self.rails(".claude", shared_branches=["prod"])
        self.assertEqual(self.shell_before("git push origin prod")["permission"], "ask")

    def test_пуш_без_ссылки_смотрит_текущую_ветку(self) -> None:
        self.init_repo("main")
        self.assertEqual(self.shell_before("git push")["permission"], "ask")


class CommitTrailers(Case):
    def test_подпись_в_команде_останавливает(self) -> None:
        answer = self.shell_before('git commit -m "fix: x\n\nCo-Authored-By: bot <b@x>"')
        self.assertEqual(answer["permission"], "deny")
        self.assertIn("Co-Authored-By", answer["agent_message"])

    def test_обычный_коммит_проходит(self) -> None:
        self.assertEqual(self.shell_before('git commit -m "fix: x"')["permission"], "allow")


class PushTaskBranch(Case):
    def test_коммит_в_ветке_задачи_уезжает(self) -> None:
        remote = self.init_repo("main")
        git(self.root, "checkout", "-q", "-b", "feature/x")
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        git(self.root, "add", "b.txt")
        git(self.root, "commit", "-q", "-m", "feat: b")
        answer = self.shell_after('git commit -m "feat: b"')
        self.assertIn("отправлена в origin", answer.get("additional_context", ""))
        self.assertIn("feature/x", git(remote, "branch", "--list"))

    def test_общая_ветка_не_пушится(self) -> None:
        remote = self.init_repo("main")
        self.assertEqual(self.shell_after('git commit -m "x"'), {})
        self.assertEqual(git(remote, "branch", "--list").strip(), "")

    def test_подпись_cursor_останавливает_пуш_и_называется(self) -> None:
        remote = self.init_repo("main")
        git(self.root, "checkout", "-q", "-b", "feature/y")
        (self.root / "c.txt").write_text("c", encoding="utf-8")
        git(self.root, "add", "c.txt")
        git(self.root, "commit", "-q", "-m",
            "feat: c\n\nCo-authored-by: Cursor <cursoragent@cursor.com>")
        context = self.shell_after('git commit -m "feat: c"').get("additional_context", "")
        self.assertIn("Attribution", context)
        self.assertIn("не отправлена", context)
        self.assertNotIn("feature/y", git(remote, "branch", "--list"))

    def test_автопуш_выключается_настройкой(self) -> None:
        self.init_repo("main")
        git(self.root, "checkout", "-q", "-b", "feature/z")
        self.rails(auto_push=False)
        context = self.shell_after('git commit -m "x"').get("additional_context", "")
        self.assertIn("автопуш отключён", context)


class ClaimWithoutRun(Case):
    def test_готово_без_прогона_напоминает(self) -> None:
        self.run_hook("afterAgentResponse", {"text": "Поправила обработчик. Готово."})
        answer = self.run_hook("stop", {"status": "completed", "loop_count": 0})
        self.assertIn("quality-verification", answer.get("followup_message", ""))

    def test_прогон_в_этом_ходе_снимает_напоминание(self) -> None:
        self.shell_after("py -m pytest tests/test_x.py")
        self.run_hook("afterAgentResponse", {"text": "Готово, тесты зелёные."})
        self.assertEqual(self.run_hook("stop", {"status": "completed", "loop_count": 0}), {})

    def test_прогон_прошлого_хода_не_считается(self) -> None:
        self.shell_after("py -m pytest")
        self.generation = "gen-2"
        self.run_hook("afterAgentResponse", {"text": "Готово."})
        answer = self.run_hook("stop", {"status": "completed", "loop_count": 0})
        self.assertIn("followup_message", answer)

    def test_ручная_проверка_и_незаконченное_не_заявление(self) -> None:
        for text in ("Готово. Проверила вручную в интерфейсе.", "Почти готово, осталось одно."):
            self.generation = text
            self.run_hook("afterAgentResponse", {"text": text})
            self.assertEqual(self.run_hook("stop", {"status": "completed", "loop_count": 0}), {})


class RequireSkills(Case):
    def put_skill(self, name: str) -> None:
        folder = self.root / ".cursor" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: >-\n  Rule. Use when x.\n---\n# x\n", encoding="utf-8")

    def test_перечень_на_старте_и_проектная_копия_главнее_личной(self) -> None:
        self.put_skill("frontend-ui")
        home = self.base / "home" / ".claude" / "skills" / "frontend-ui"
        home.mkdir(parents=True)
        (home / "SKILL.md").write_text("---\nname: frontend-ui\n---\n", encoding="utf-8")
        context = self.run_hook("sessionStart", {"session_id": self.conversation})
        text = context.get("additional_context", "")
        self.assertIn("frontend (web/): frontend-ui", text)
        self.assertIn("действует проектная копия", text)

    def test_правка_области_без_открытого_скилла_напоминает_один_раз(self) -> None:
        self.put_skill("frontend-ui")
        self.run_hook("sessionStart", {})
        edit = {"tool_name": "Write", "tool_input": {"path": str(self.root / "web" / "a.tsx")}}
        self.assertIn("frontend-ui", self.run_hook("postToolUse", edit).get("additional_context", ""))
        self.assertEqual(self.run_hook("postToolUse", edit), {})

    def test_открытый_скилл_не_напоминается(self) -> None:
        self.put_skill("frontend-ui")
        self.run_hook("postToolUse", {"tool_name": "Read", "tool_input": {
            "path": str(self.root / ".cursor" / "skills" / "frontend-ui" / "SKILL.md")}})
        edit = {"tool_name": "Write", "tool_input": {"path": str(self.root / "web" / "a.tsx")}}
        self.assertEqual(self.run_hook("postToolUse", edit), {})

    def test_правка_скриптом_в_оболочке_тоже_видна(self) -> None:
        self.init_repo("main")
        self.put_skill("frontend-ui")
        self.run_hook("sessionStart", {})
        (self.root / "web").mkdir()
        (self.root / "web" / "b.tsx").write_text("x", encoding="utf-8")
        context = self.shell_after("py fix.py").get("additional_context", "")
        self.assertIn("frontend-ui", context)


class UiBoundary(Case):
    def test_рукописная_таблица_предупреждается(self) -> None:
        page = self.root / "web" / "src" / "pages" / "list.tsx"
        page.parent.mkdir(parents=True)
        page.write_text("export const L = () => <table><tr/></table>;\n", encoding="utf-8")
        answer = self.run_hook("postToolUse", {"tool_name": "Write",
                                               "tool_input": {"path": str(page)}})
        self.assertIn("рукописная таблица", answer.get("additional_context", ""))

    def test_внутри_набора_молчит(self) -> None:
        part = self.root / "web" / "src" / "components" / "ui" / "table.tsx"
        part.parent.mkdir(parents=True)
        part.write_text("import * as T from '@tanstack/react-table'; <table/>", encoding="utf-8")
        self.assertEqual(self.run_hook("postToolUse", {"tool_name": "Write",
                                                       "tool_input": {"path": str(part)}}), {})


if __name__ == "__main__":
    unittest.main()

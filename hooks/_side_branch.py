"""Работа с боковой веткой `calibration` в ~/.cursor без checkout.

Журнал замеров живёт отдельно от `master`: меняется каждый ход и не должен
забивать историю планов и скиллов. Ветка обычная, с историей, пушится без
force — иначе вторая машина затёрла бы записи первой.

Сборка плумбингом: hash-object → update-index в отдельный индекс →
commit-tree → update-ref. Рабочее дерево не трогается.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

CURSOR_HOME = Path.home() / ".cursor"
PUSH_TIMEOUT = 120


def git(*args: str, cwd: Path | str | None = None, timeout: int = 20):
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def ref_exists(ref: str) -> bool:
    rc, _, _ = git("rev-parse", "--verify", "--quiet", ref, cwd=CURSOR_HOME)
    return rc == 0


def resolve_ref(ref: str) -> str | None:
    rc, sha, _ = git("rev-parse", "--verify", "--quiet", ref, cwd=CURSOR_HOME)
    return sha or None if rc == 0 else None


def fetch_branch(branch: str, timeout: int = 60) -> bool:
    rc, _, _ = git("fetch", "--quiet", "origin",
                   f"+{branch}:refs/remotes/origin/{branch}",
                   cwd=CURSOR_HOME, timeout=timeout)
    return rc == 0


def read_tree(ref: str) -> dict[str, str]:
    if not ref_exists(ref):
        return {}
    rc, out, _ = git("ls-tree", "-r", "--full-tree", ref, cwd=CURSOR_HOME)
    if rc != 0:
        return {}
    tree: dict[str, str] = {}
    for line in out.splitlines():
        try:
            meta, path = line.split("\t", 1)
            _mode, kind, sha = meta.split()
        except ValueError:
            continue
        if kind == "blob":
            tree[path] = sha
    return tree


def blob_bytes(sha: str) -> bytes:
    p = subprocess.run(
        ["git", "-C", str(CURSOR_HOME), "cat-file", "blob", sha],
        capture_output=True, timeout=120,
    )
    return p.stdout if p.returncode == 0 else b""


def hash_file(path: Path) -> str | None:
    rc, out, _ = git("hash-object", "-w", "--", str(path), cwd=CURSOR_HOME,
                     timeout=120)
    return out if rc == 0 and out else None


def hash_bytes(data: bytes) -> str | None:
    p = subprocess.run(
        ["git", "-C", str(CURSOR_HOME), "hash-object", "-w", "--stdin"],
        input=data, capture_output=True, timeout=60,
    )
    return p.stdout.decode().strip() if p.returncode == 0 else None


def build_tree(entries: dict[str, str]) -> str | None:
    if not entries:
        return None
    lines = "".join(f"100644 {sha}\t{path}\n"
                    for path, sha in sorted(entries.items()))
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(td) / "index"))
        p = subprocess.run(
            ["git", "-C", str(CURSOR_HOME), "update-index", "--add",
             "--index-info"],
            input=lines.encode("utf-8"), capture_output=True, env=env,
            timeout=120,
        )
        if p.returncode != 0:
            return None
        p = subprocess.run(["git", "-C", str(CURSOR_HOME), "write-tree"],
                           capture_output=True, env=env, timeout=120)
        if p.returncode != 0:
            return None
        return p.stdout.decode().strip()


def commit_tree(tree: str, message: str, parents: list[str] | None = None) -> str | None:
    args = ["commit-tree", tree]
    for p in parents or []:
        args += ["-p", p]
    args += ["-m", message]
    rc, sha, _ = git(*args, cwd=CURSOR_HOME)
    return sha if rc == 0 and sha else None


def update_ref(branch: str, sha: str) -> bool:
    rc, _, _ = git("update-ref", f"refs/heads/{branch}", sha, cwd=CURSOR_HOME)
    return rc == 0


def push_detached(branch: str, force: bool = False) -> None:
    cmd = ["git", "-C", str(CURSOR_HOME), "push", "--quiet"]
    if force:
        cmd.append("--force")
    cmd += ["origin", f"refs/heads/{branch}:refs/heads/{branch}"]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError:
        pass


def push_sync(branch: str, force: bool = False, timeout: int = PUSH_TIMEOUT):
    args = ["push", "--quiet"]
    if force:
        args.append("--force")
    args += ["origin", f"refs/heads/{branch}:refs/heads/{branch}"]
    rc, _out, err = git(*args, cwd=CURSOR_HOME, timeout=timeout)
    return rc, err

"""Синхронизация ~/.cursor между домом и офисом.

В течение сессии коммит катится на ветке wip/<роль> и пушится с
--force-with-lease: веткой владеет одна машина. master публикуется обычным
push на границе сессии. Чужую недоехавшую ветку хук не сливает.

    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --status
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --stop
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --session-start
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --session-end
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --show-wip office
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --adopt-wip office
    py -X utf8 ~/.cursor/hooks/autosync_cursor.py --drop-wip office --yes

Хук не блокирует ход и не ждёт сеть на push.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home() / ".cursor"
LOG_DIR = HOME / "logs"
PUSH_LOG = LOG_DIR / "push.log"
ROLL_STATE = LOG_DIR / ".rolling.json"
LOCAL = HOME / "local.json"
MAX_LOG_BYTES = 200_000
WIP_PREFIX = "wip/"
ROLES = ("home", "office")


def git(*args: str, timeout: int = 20) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", "-C", str(HOME), *args], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with PUSH_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def role() -> str:
    try:
        data = json.loads(LOCAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = str(data.get("role") or "").strip().lower()
    return value if value in ROLES else ""


def host() -> str:
    return os.environ.get("COMPUTERNAME") or "cursor"


def mid_operation() -> str | None:
    gitdir = HOME / ".git"
    for name, label in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                        ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick")):
        if (gitdir / name).exists():
            return label
    return None


def current_branch() -> str:
    rc, b, _ = git("branch", "--show-current")
    return b or "master"


def has_changes() -> bool:
    rc, out, _ = git("status", "--porcelain")
    return rc == 0 and bool(out.strip())


def read_roll() -> dict:
    try:
        data = json.loads(ROLL_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_roll(sha: str, base: str, started: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    ROLL_STATE.write_text(json.dumps({"sha": sha, "base": base, "started": started}),
                          encoding="utf-8")


def clear_roll(why: str) -> None:
    if ROLL_STATE.exists():
        ROLL_STATE.unlink(missing_ok=True)
        log(f"roll закрыт: {why}")


def roll_open() -> dict | None:
    st = read_roll()
    if not st.get("sha"):
        return None
    rc, head, _ = git("rev-parse", "HEAD")
    if rc != 0 or head != st.get("sha"):
        clear_roll("HEAD разошёлся с катящимся коммитом")
        return None
    return st


def describe() -> str:
    rc, out, _ = git("diff", "--cached", "--name-only")
    names = [Path(p).name for p in out.splitlines() if p][:4]
    if not names:
        return "правки"
    more = ""
    rc2, count, _ = git("diff", "--cached", "--name-only")
    n = len([p for p in count.splitlines() if p])
    if n > len(names):
        more = f" +{n - len(names)}"
    return ", ".join(names) + more


def detached_push(branch: str, lease: bool) -> None:
    cmd = ["git", "-C", str(HOME), "push"]
    if lease:
        cmd.append("--force-with-lease")
    cmd += ["origin", f"HEAD:{branch}"]
    LOG_DIR.mkdir(exist_ok=True)
    try:
        out = open(PUSH_LOG, "a", encoding="utf-8")
        out.write(f"\n=== {dt.datetime.now().isoformat(timespec='seconds')} {' '.join(cmd[3:])}\n")
        out.flush()
    except OSError:
        out = None
    kwargs = {"stdin": subprocess.DEVNULL,
              "stdout": out or subprocess.DEVNULL,
              "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        log(f"push {branch} {'lease' if lease else 'обычный'}")
    except OSError as e:
        log(f"push не запустился: {e}")
    finally:
        if out is not None:
            out.close()


def remote_exists(branch: str) -> bool:
    rc, _, _ = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    return rc == 0


def sync_calibration(flag: str) -> None:
    """Журнал замеров едет своей веткой, не через master."""
    script = HOME / "hooks" / "sync_calibration.py"
    if not script.is_file():
        return
    try:
        subprocess.run([sys.executable, "-X", "utf8", str(script), flag],
                       cwd=str(HOME), timeout=90,
                       capture_output=True)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"calib {flag}: {e}")


def cmd_stop() -> int:
    if mid_operation():
        log("stop: пропуск, идёт операция")
        return 0
    if current_branch() != "master":
        log(f"stop: пропуск, ветка {current_branch()}")
        return 0
    sync_calibration("--out")
    lease = False
    if has_changes():
        rc, _, err = git("add", "-A")
        if rc != 0:
            log(f"stop: add: {err}")
            return 0
        st = roll_open()
        msg = f"auto: {describe()} · {host()}"
        if st:
            rc, _, err = git("commit", "--quiet", "--amend", "-m", msg)
            lease = True
        else:
            rc, _, err = git("commit", "--quiet", "-m", msg)
        if rc != 0:
            log(f"stop: commit: {err}")
            return 0
        rc1, sha, _ = git("rev-parse", "HEAD")
        rc2, parent, _ = git("rev-parse", "HEAD^")
        if rc1 == 0 and rc2 == 0:
            write_roll(sha, parent, (st or {}).get("started")
                       or dt.datetime.now().isoformat(timespec="seconds"))
    target = f"{WIP_PREFIX}{role()}" if role() else ""
    if not target:
        print("[autosync] роль машины не задана в ~/.cursor/local.json "
              "(home или office) — в master ничего не пушу.")
        return 0
    rc1, head, _ = git("rev-parse", "HEAD")
    known = remote_exists(target)
    rc2, rem, _ = git("rev-parse", f"origin/{target}")
    if not known or rc1 != 0 or rc2 != 0 or head != rem:
        detached_push(target, lease=known)
    return 0


def promote_master() -> None:
    rc, out, _ = git("rev-list", "--count", "origin/master..HEAD")
    ahead = int(out) if rc == 0 and out.isdigit() else 0
    if ahead <= 0:
        return
    detached_push("master", lease=False)
    log(f"продвижение: {ahead} -> master")


def fetch_wips() -> bool:
    rc, _, _ = git("fetch", "--quiet", "--prune", "origin",
                   f"+refs/heads/{WIP_PREFIX}*:refs/remotes/origin/{WIP_PREFIX}*",
                   timeout=25)
    return rc == 0


def report_foreign() -> None:
    mine = role()
    rc, out, _ = git("for-each-ref", "--format=%(refname:short)",
                     f"refs/remotes/origin/{WIP_PREFIX}")
    if rc != 0:
        return
    lines = []
    for ref in out.splitlines():
        r = ref.removeprefix("origin/").removeprefix(WIP_PREFIX)
        if r == mine or r not in ROLES:
            continue
        rc1, cnt, _ = git("rev-list", "--count", f"origin/master..{ref}")
        n = int(cnt) if rc1 == 0 and cnt.isdigit() else 0
        if n <= 0:
            continue
        rc2, meta, _ = git("log", "-1", "--format=%cr|%s", ref)
        when, _, subj = meta.partition("|") if rc2 == 0 else ("", "", "")
        lines.append(f"  {ref} — {n} коммит(ов), {when}\n      «{subj}»")
    if not lines:
        return
    print("[autosync] работа с другой машины, не дошедшая до master:")
    print("\n".join(lines))
    print("  Само не сольётся. py -X utf8 ~/.cursor/hooks/autosync_cursor.py "
          "--show-wip <роль> | --adopt-wip <роль> | --drop-wip <роль> --yes")


def cmd_session_start() -> int:
    busy = mid_operation()
    if busy:
        print(f"[autosync] в ~/.cursor идёт {busy} — синк пропущен")
        return 0
    clear_roll("новая сессия")
    if current_branch() != "master":
        print(f"[autosync] ~/.cursor не на master ({current_branch()}), синк пропущен")
        return 0
    rc, _, err = git("fetch", "--quiet", "origin", "master", timeout=25)
    if rc != 0:
        low = err.lower()
        if any(s in low for s in ("could not resolve", "unable to access", "failed to connect", "timeout")):
            log("session-start: сети нет")
        else:
            print(f"[autosync] fetch не прошёл: {err[:400]}")
        return 0
    rc_mb, _, _ = git("merge-base", "HEAD", "origin/master")
    if rc_mb != 0:
        print("[autosync] история master разошлась с origin без общего предка.")
        print("  git -C ~/.cursor log --oneline origin/master..HEAD")
        return 0
    rc, _, err = git("rebase", "--autostash", "--quiet", "origin/master", timeout=40)
    if rc != 0:
        print("[autosync] rebase ~/.cursor не прошёл:")
        print(f"  {err[:500]}")
        return 0
    promote_master()
    sync_calibration("--in")
    if fetch_wips():
        report_foreign()
    return 0


def cmd_session_end() -> int:
    if mid_operation() or not role() or current_branch() != "master":
        return 0
    clear_roll("конец сессии")
    git("fetch", "--quiet", "origin", "master", timeout=25)
    promote_master()
    return 0


def cmd_show_wip(value: str) -> int:
    fetch_wips()
    ref = f"origin/{WIP_PREFIX}{value}"
    rc, _, _ = git("rev-parse", "--verify", "--quiet", ref)
    if rc != 0:
        print(f"[autosync] ветки {ref} нет")
        return 1
    rc, out, _ = git("log", "--oneline", f"origin/master..{ref}")
    print(out or "(пусто — уже в master)")
    return 0


def cmd_adopt_wip(value: str) -> int:
    if current_branch() != "master":
        print("[autosync] adopt только с master")
        return 1
    fetch_wips()
    ref = f"origin/{WIP_PREFIX}{value}"
    rc, _, err = git("merge", "--no-ff", "--no-edit", ref, timeout=40)
    if rc != 0:
        print(f"[autosync] слияние не прошло, конфликт оставлен: {err[:400]}")
        return 1
    print(f"[autosync] {ref} слита в master. Уедет на конце сессии.")
    return 0


def cmd_drop_wip(value: str, yes: bool) -> int:
    ref = f"{WIP_PREFIX}{value}"
    if not yes:
        print(f"[autosync] это удалит origin/{ref}. Повтори с --yes.")
        return 1
    rc, _, err = git("push", "origin", "--delete", ref, timeout=40)
    print(err or f"[autosync] origin/{ref} удалена")
    return rc


def cmd_status() -> int:
    print(f"роль: {role() or 'не задана'}")
    print(f"ветка: {current_branch()}")
    rc, out, _ = git("status", "-sb")
    print(out)
    st = read_roll()
    print(f"катится: {st.get('sha', 'нет')[:8] if st.get('sha') else 'нет'}")
    script = HOME / "hooks" / "sync_calibration.py"
    if script.is_file():
        try:
            subprocess.run([sys.executable, "-X", "utf8", str(script), "--status"],
                           cwd=str(HOME), timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--stop" in args:
        return cmd_stop()
    if "--session-start" in args:
        return cmd_session_start()
    if "--session-end" in args:
        return cmd_session_end()
    if "--status" in args:
        return cmd_status()
    if "--show-wip" in args:
        i = args.index("--show-wip")
        return cmd_show_wip(args[i + 1]) if i + 1 < len(args) else 1
    if "--adopt-wip" in args:
        i = args.index("--adopt-wip")
        return cmd_adopt_wip(args[i + 1]) if i + 1 < len(args) else 1
    if "--drop-wip" in args:
        i = args.index("--drop-wip")
        value = args[i + 1] if i + 1 < len(args) else ""
        return cmd_drop_wip(value, "--yes" in args) if value else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"сбой: {e}")
        sys.exit(0)

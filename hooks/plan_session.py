#!/usr/bin/env python3
"""Кто ведёт план в Cursor: привязка к conversation/session id.

    py -X utf8 ~/.cursor/hooks/plan_session.py --claim 015 --session <uuid>
    py -X utf8 ~/.cursor/hooks/plan_session.py --release 015
    py -X utf8 ~/.cursor/hooks/plan_session.py --status
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

HOME = Path.home() / ".cursor"
STATE = HOME / "logs" / "plan-sessions.json"
PLANS = HOME / "plans"

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_NUM_RE = re.compile(r"^\d{3}$")


def load() -> dict:
    try:
        with io.open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(STATE)


def plan_title(number: str) -> str:
    for p in sorted(PLANS.glob(f"{number}-*.md")):
        return p.stem
    return f"{number}-?"


def cmd_claim(number: str, session: str, cwd: str | None, force: bool) -> int:
    if not _NUM_RE.match(number):
        print(f"[plan-session] номер плана — три цифры, не {number!r}")
        return 1
    if not _UUID_RE.match(session):
        print(f"[plan-session] не похоже на session_id: {session!r}")
        return 1
    data = load()
    was = data.get(number)
    if was and was.get("session") != session and not force:
        print(
            f"[plan-session] план {number} уже ведёт {was['session'][:8]}… "
            "(повтори с --force, если та сессия точно закрыта)"
        )
        return 1
    data[number] = {
        "session": session,
        "cwd": cwd or "",
        "claimed": dt.datetime.now().isoformat(timespec="seconds"),
        "blocks": 0,
        "idle_blocks": 0,
        "fingerprint": "",
        "last_block": None,
    }
    save(data)
    print(
        f"[plan-session] план {number} ({plan_title(number)}) "
        f"ведёт сессия {session}"
    )
    return 0


def cmd_release(number: str) -> int:
    data = load()
    if number not in data:
        print(f"[plan-session] план {number} никто не заявлял")
        return 0
    session = data.pop(number).get("session", "?")
    save(data)
    print(f"[plan-session] план {number} освобождён (вела {session[:8]}…)")
    return 0


def cmd_status() -> int:
    data = load()
    if not data:
        print("заявок нет — Stop-хук ни один план не держит")
        return 0
    for number in sorted(data):
        rec = data[number]
        print(f"{number} ({plan_title(number)})")
        print(f"  сессия : {rec.get('session', '?')}")
        if rec.get("cwd"):
            print(f"  каталог: {rec['cwd']}")
        print(f"  заявлен: {rec.get('claimed', '?')}")
        print(
            f"  blocks={rec.get('blocks', 0)} "
            f"idle={rec.get('idle_blocks', 0)} "
            f"last={rec.get('last_block')}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim", metavar="NNN")
    parser.add_argument("--release", metavar="NNN")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.status:
        return cmd_status()
    if args.release:
        return cmd_release(args.release)
    if args.claim:
        return cmd_claim(args.claim, args.session, args.cwd or None, args.force)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

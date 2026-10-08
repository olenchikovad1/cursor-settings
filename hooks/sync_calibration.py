#!/usr/bin/env python3
"""Журнал калибровки времени на отдельной ветке `calibration` в ~/.cursor.

    py -X utf8 ~/.cursor/hooks/sync_calibration.py --out
    py -X utf8 ~/.cursor/hooks/sync_calibration.py --in
    py -X utf8 ~/.cursor/hooks/sync_calibration.py --status

Замеры пишутся каждый ход и в `master` не входят: иначе история планов
тонет в служебных коммитах. Ветка обычная, с историей, пушится без force —
вторая машина не должна затирать записи первой.

Расхождение машин (офис без сети, дом тоже писал) разрешается union-ом строк
и коммитом с двумя родителями. Для append-only журнала это верное слияние.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _side_branch as sb  # noqa: E402

CURSOR_HOME = Path.home() / ".cursor"
TIME_DIR = CURSOR_HOME / "time-analysis"

BRANCH = "calibration"
JSONL_FILES = (
    "records/history.jsonl",
    "records/plans.jsonl",
    "records/workflows.jsonl",
    "records/steps.jsonl",
    "records/stories.jsonl",
)
MATRIX_FILE = "matrix.json"
REMOTE_REF = f"refs/remotes/origin/{BRANCH}"
LOCAL_REF = f"refs/heads/{BRANCH}"


def host() -> str:
    return os.environ.get("COMPUTERNAME") or "cursor"


def _canon(rec: dict) -> str:
    try:
        return json.dumps(rec, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(rec)


def parse_jsonl(data: bytes) -> list[dict]:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def union_records(base: list[dict], extra: list[dict]) -> tuple[list[dict], int]:
    seen = {_canon(r) for r in base}
    out = list(base)
    added = 0
    for r in extra:
        k = _canon(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        added += 1
    return out, added


def local_records() -> dict[str, list[dict]]:
    out = {}
    for rel in JSONL_FILES:
        p = TIME_DIR / rel
        out[rel] = parse_jsonl(p.read_bytes()) if p.exists() else []
    return out


def branch_records(ref: str) -> dict[str, list[dict]]:
    tree = sb.read_tree(ref)
    out = {}
    for rel in JSONL_FILES:
        sha = tree.get(rel)
        out[rel] = parse_jsonl(sb.blob_bytes(sha)) if sha else []
    return out


def write_local(rel: str, records: list[dict]) -> None:
    p = TIME_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def restore_references(ref: str) -> int:
    tree = sb.read_tree(ref)
    sha = tree.get(MATRIX_FILE)
    if not sha:
        return 0
    try:
        theirs = json.loads(sb.blob_bytes(sha).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    their_refs = theirs.get("references") or []
    if not their_refs:
        return 0
    local_path = TIME_DIR / MATRIX_FILE
    mine: dict = {}
    if local_path.exists():
        try:
            mine = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mine = {}
    if len(mine.get("references") or []) >= len(their_refs):
        return 0
    mine.setdefault("matrix", theirs.get("matrix") or {})
    mine["references"] = their_refs
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps(mine, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(their_refs)


def recompute() -> None:
    script = TIME_DIR / "calibrate.py"
    if not script.exists():
        return
    try:
        subprocess.run([sys.executable, "-X", "utf8", str(script), "recompute"],
                       capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        pass


def cmd_out() -> int:
    sb.fetch_branch(BRANCH)
    remote_sha = sb.resolve_ref(REMOTE_REF)
    local_sha = sb.resolve_ref(LOCAL_REF)

    mine = local_records()
    theirs = branch_records(REMOTE_REF) if remote_sha else {}

    merged: dict[str, list[dict]] = {}
    pulled_in = 0
    for rel in JSONL_FILES:
        merged[rel], added = union_records(mine[rel], theirs.get(rel, []))
        pulled_in += added
        if added:
            write_local(rel, merged[rel])
    if pulled_in:
        recompute()

    entries: dict[str, str] = {}
    for rel in JSONL_FILES:
        blob = sb.hash_bytes(
            "".join(json.dumps(r, ensure_ascii=False) + "\n"
                    for r in merged[rel]).encode("utf-8"))
        if blob:
            entries[rel] = blob
    matrix = TIME_DIR / MATRIX_FILE
    if matrix.exists():
        blob = sb.hash_file(matrix)
        if blob:
            entries[MATRIX_FILE] = blob

    if not entries:
        return 0

    tree = sb.build_tree(entries)
    if not tree:
        print("[calib] не удалось собрать дерево")
        return 0

    if local_sha:
        rc, cur_tree, _ = sb.git("rev-parse", f"{local_sha}^{{tree}}",
                                 cwd=CURSOR_HOME)
        if rc == 0 and cur_tree == tree and (
                not remote_sha or remote_sha == local_sha):
            return 0

    parents = [s for s in (local_sha, remote_sha) if s]
    if len(parents) == 2 and parents[0] == parents[1]:
        parents = parents[:1]

    total = sum(len(v) for v in merged.values())
    msg = f"calib: {total} записей · {host()}"
    if len(parents) == 2:
        msg += f"\n\nСлияние расхождения: подтянуто {pulled_in} чужих записей."
    sha = sb.commit_tree(tree, msg, parents)
    if not sha or not sb.update_ref(BRANCH, sha):
        print("[calib] не удалось записать ветку")
        return 0

    sb.push_detached(BRANCH, force=False)
    return 0


def cmd_in() -> int:
    sb.fetch_branch(BRANCH)
    ref = REMOTE_REF if sb.ref_exists(REMOTE_REF) else LOCAL_REF
    if not sb.ref_exists(ref):
        return 0

    restored_refs = restore_references(ref)
    theirs = branch_records(ref)
    mine = local_records()
    total_added = 0
    for rel in JSONL_FILES:
        merged, added = union_records(mine[rel], theirs.get(rel, []))
        if added:
            write_local(rel, merged)
            total_added += added

    remote_sha = sb.resolve_ref(REMOTE_REF)
    local_sha = sb.resolve_ref(LOCAL_REF)
    if remote_sha and remote_sha != local_sha:
        rc, _, _ = sb.git("merge-base", "--is-ancestor",
                          local_sha or remote_sha, remote_sha, cwd=CURSOR_HOME)
        if rc == 0:
            sb.update_ref(BRANCH, remote_sha)

    if total_added or restored_refs:
        recompute()
    bits = []
    if total_added:
        bits.append(f"подтянуто {total_added} замеров с другой машины")
    if restored_refs:
        bits.append(f"восстановлено {restored_refs} эталонов шкалы")
    if bits:
        print("[calib] " + ", ".join(bits))
    return 0


def cmd_status() -> int:
    sb.fetch_branch(BRANCH)
    local_sha = sb.resolve_ref(LOCAL_REF)
    remote_sha = sb.resolve_ref(REMOTE_REF)
    print(f"ветка {BRANCH}: локально {local_sha or 'нет'} / "
          f"origin {remote_sha or 'нет'}")
    if local_sha:
        rc, n, _ = sb.git("rev-list", "--count", LOCAL_REF, cwd=CURSOR_HOME)
        print(f"  коммитов в истории: {n}")
    mine = local_records()
    for rel in JSONL_FILES:
        in_branch = len(branch_records(LOCAL_REF).get(rel, [])) if local_sha else 0
        print(f"  {rel:28} локально {len(mine[rel]):4}  в ветке {in_branch:4}")
    if local_sha and remote_sha and local_sha != remote_sha:
        rc, ahead, _ = sb.git("rev-list", "--count", f"{remote_sha}..{local_sha}",
                              cwd=CURSOR_HOME)
        rc2, behind, _ = sb.git("rev-list", "--count", f"{local_sha}..{remote_sha}",
                                cwd=CURSOR_HOME)
        print(f"  не запушено {ahead}, не подтянуто {behind}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--in", dest="do_in", action="store_true")
    g.add_argument("--out", dest="do_out", action="store_true")
    g.add_argument("--status", action="store_true")
    a = ap.parse_args()
    try:
        if a.do_in:
            return cmd_in()
        if a.do_out:
            return cmd_out()
        return cmd_status()
    except Exception as e:
        print(f"[calib] сбой: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())

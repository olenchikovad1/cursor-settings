import json
import re
from collections import Counter
from pathlib import Path

files = [
    Path.home() / ".cursor/projects/d-Laretto-hrportal/agent-transcripts/d9d46e9b-3e09-4c7e-973e-b9b4e03e846b/d9d46e9b-3e09-4c7e-973e-b9b4e03e846b.jsonl",
    Path.home() / ".cursor/projects/d-Laretto-plm/agent-transcripts/d8bf55d1-e87b-4ff7-8fb8-41fbd16120d4/d8bf55d1-e87b-4ff7-8fb8-41fbd16120d4.jsonl",
]
allow = {
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "git fetch", "git ls-files", "git blame", "git rev-parse", "git add",
    "git commit", "git merge", "git rebase", "git checkout", "git switch",
    "git restore", "git cherry-pick", "git revert", "git stash", "git pull",
    "git push", "git worktree",
    "py", "python", "pytest", "uv", "pip", "poetry", "ruff", "mypy",
    "npm", "pnpm", "yarn", "tsc", "eslint", "oxlint", "make",
    "docker", "ssh", "scp", "rsync", "psql", "pg_dump", "pg_restore", "redis-cli",
    "rg", "ls", "dir",
    "gh", "npx", "node", "vitest", "bash", "curl",
    "awk", "cat", "cp", "diff", "echo", "find", "grep", "head", "tail", "wc",
    "mkdir", "mv", "sed", "sort", "stat", "which", "xxd", "netstat",
}

def first_stmt(cmd: str) -> str:
    cmd = cmd.strip()
    # drop leading assignments like $x = ...
    return cmd.split("\n", 1)[0][:180]

heads = Counter()
unmatched = Counter()
examples = {}
n = 0
for f in files:
    for line in f.read_text(encoding="utf-8").splitlines():
        if '"name":"Shell"' not in line and '"name": "Shell"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = ev.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("name") != "Shell":
                continue
            cmd = (block.get("input") or {}).get("command") or ""
            if not cmd:
                continue
            n += 1
            head = first_stmt(cmd)
            token = head.split(";", 1)[0].strip()
            # two-word git
            m = re.match(r"^(git\s+\S+|\S+)", token)
            key = m.group(1) if m else token[:40]
            heads[key] += 1
            if not any(token.startswith(a) or head.startswith(a) for a in allow):
                unmatched[key] += 1
                examples.setdefault(key, head)

print("shell calls", n)
print("--- unmatched ---")
for k, c in unmatched.most_common(60):
    print(f"{c:3} {k}  || {examples[k][:140]}")

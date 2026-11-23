"""PreToolUse hook for healing sessions: blocks what a healer must never do, whatever it was told
(by the logs, a web page or its own reasoning). Exit 2 with a reason denies the call.

The run directory (CODEMEDIC_RUN) holds rules.json: the project directory, the files it must not edit
(protected ones and those the author had uncommitted changes in) and the tick-lock holder's pid file."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

BASH_DENY = [
    (r"\bgit\s+push\b", "CodeMedic pushes after checking the tests itself; commit only"),
    (r"\bgit\s+reset\b", "no git reset (it can discard the author's work)"),
    (r"\bgit\s+(clean|stash|rebase|filter-branch|filter-repo|reflog\s+expire|gc)\b", "history/working-tree rewrite"),
    (r"\bgit\s+(checkout|restore|switch)\b", "no checkout/restore/switch (the author's uncommitted work)"),
    (r"\bgit\s+commit\b[^|;&]*(--amend|\s-(?!-)[a-zA-Z]*a[a-zA-Z]*\b|--all\b)", "commit explicit paths, never --amend/-a"),
    (r"\bgit\s+add\b[^|;&]*(\s-A\b|\s--all\b|\s-u\b|\s--update\b|\s\.(\s|$)|\s\*)", "git add explicit file paths only"),
    (r"\bgit\s+(branch|tag|remote|config|worktree|submodule|update-ref|replace)\b", "no refs/config changes"),
    (r"\bgh\b", "no GitHub CLI (it acts in public)"),
    (r"\b(sudo|launchctl|crontab|osascript|security|pmset|networksetup|scutil|csrutil|systemsetup|tccutil)\b",
     "no system settings, keychain or launchd"),
    (r"\bdefaults\s+(write|delete)\b", "no system settings"),
    (r"\b(pip3?|uv|poetry|npm|npx|pnpm|yarn|brew|gem|cargo)\s+(install|add|uninstall|remove|update|upgrade)\b",
     "no installing or removing packages"),
    (r"\b(curl|wget|http|httpie)\b[^|;&]*(\s-X\s*(?!GET)\w+|\s--request\s+(?!GET)\w+|\s(-d|--data\S*|-F|--form|-T|--upload-file)\b)",
     "read-only HTTP only (GET)"),
    (r"\b(pkill|killall)\b", "no killing processes"),
    (r"(^|[;&|]\s*)(sqlite3)\b[^|;&]*\b(drop|delete|truncate|vacuum)\b", "no deleting database rows or tables"),
    (r"\b(DROP\s+TABLE|DELETE\s+FROM|TRUNCATE)\b", "no deleting database rows or tables"),
    (r"(^|\s|/)\.env\b|\.claude[\w-]*(/|\b)|\.ssh/|\.aws/|\.netrc|keychain|id_rsa|id_ed25519",
     "secrets and logins are off limits"),
    (r"\b(chmod|chown)\s+-R\b", "no recursive permission changes"),
    (r"\bdd\s+if=|\bmkfs|\bdiskutil\b", "no disk tools"),
]


def deny(reason: str) -> None:
    print(f"CodeMedic guard: {reason}", file=sys.stderr)
    sys.exit(2)


def rules() -> dict:
    run = os.environ.get("CODEMEDIC_RUN")
    if not run:
        deny("no CODEMEDIC_RUN: this hook only runs inside a CodeMedic session")
    return json.loads((Path(run) / "rules.json").read_text())


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_edit(path_s: str, r: dict) -> None:
    path = Path(path_s).expanduser()
    if not path.is_absolute():
        path = Path(r["project"]) / path
    proj, run = Path(r["project"]), Path(r["run"])
    if _inside(path, run):
        return
    if not _inside(path, proj):
        deny(f"edits outside the project are not allowed ({path})")
    rel = path.resolve().relative_to(proj.resolve()).as_posix()
    if rel.startswith((".git/", ".venv/")) or rel.split("/")[0] in (".git", ".venv"):
        deny(f"{rel} is not source code")
    for p in r["protected"]:
        if rel == p.rstrip("/") or (p.endswith("/") and rel.startswith(p)):
            deny(f"{rel} is the author's configuration or content; report the change it needs instead")
    if rel in r["dirty"]:
        deny(f"{rel} has the author's uncommitted changes; don't touch it, report the fix instead")


def check_read(path_s: str) -> None:
    if re.search(r"(^|/)\.env(\.|$)|/\.claude[\w-]*(/|$)|/\.ssh/|/\.aws/|\.netrc", path_s):
        deny("secrets and logins are off limits")


def check_rm(cmd: str, r: dict) -> None:
    """rm only inside the run directory (scratch files)."""
    for seg in re.split(r"[;&|]+|\$\(|`", cmd):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        for k, w in enumerate(words):
            if Path(w).name in ("rm", "rmdir", "unlink", "shred", "trash"):
                targets = [t for t in words[k + 1:] if not t.startswith("-")]
                for t in targets:
                    if not _inside(Path(t).expanduser() if Path(t).is_absolute() else Path(r["cwd"]) / t,
                                   Path(r["run"])):
                        deny(f"deleting {t} is not allowed (only scratch files in the run directory)")


def check_kill(cmd: str, r: dict) -> None:
    if re.search(r"(^|[;&|]\s*|\s)kill\b", cmd) and r["hold_pid_file"] not in cmd:
        deny(f"the only process you may kill is the tick-lock holder: kill $(cat {r['hold_pid_file']})")


def check_bash(cmd: str, r: dict) -> None:
    for pat, why in BASH_DENY:
        if re.search(pat, cmd, re.I):
            deny(why)
    check_rm(cmd, r)
    check_kill(cmd, r)
    # shell writes to protected or dirty files (> file, tee file, sed -i file)
    for m in re.finditer(r"(?:>>?|\btee\s+(?:-a\s+)?|\bsed\s+-i\s*(?:''|\"\")?\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+)"
                         r"\s*([^\s;&|<>]+)", cmd):
        target = m.group(1)
        if target.startswith(("/dev/", "&")):
            continue
        check_edit(target, r)


def main() -> None:
    event = json.load(sys.stdin)
    r = rules()
    r.setdefault("cwd", event.get("cwd") or r["project"])
    tool, inp = event.get("tool_name", ""), event.get("tool_input", {}) or {}
    if tool == "Bash":
        check_bash(inp.get("command", ""), r)
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        check_edit(inp.get("file_path") or inp.get("notebook_path") or "", r)
    elif tool in ("Read", "Grep", "Glob"):
        check_read(str(inp.get("file_path") or inp.get("path") or ""))
    elif tool.startswith("mcp__"):
        deny("no MCP tools in a healing session (use the project's own commands)")
    sys.exit(0)


if __name__ == "__main__":
    main()

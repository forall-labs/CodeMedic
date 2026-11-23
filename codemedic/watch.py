"""Reading the projects' logs: only what was written since the last look, turned into incidents
(one per distinct error, keyed by a signature that ignores ids, numbers and URLs)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_READ = 2_000_000          # a log that grew more than this since the last look: read its end only
STAMP = re.compile(r"^\s*(\[?\d{4}-\d\d-\d\d[T ][\d:.]+\]?|\[\d\d:\d\d:\d\d\])\s*")


@dataclass
class Incident:
    sig: str                  # normalised error, the key for dedupe and limits
    text: str                 # the first line (or traceback) seen, as written
    source: str               # which log
    count: int = 1
    examples: list[str] = field(default_factory=list)


def read_new(path: Path, offsets: dict, key: str) -> list[str]:
    """Complete lines added to path since the last call. The first look at a file only records
    where it ends (old failures were dealt with before CodeMedic existed)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    seen = offsets.get(key)
    if seen is None:
        offsets[key] = {"pos": st.st_size, "inode": st.st_ino}
        return []
    pos = seen["pos"]
    if seen.get("inode") != st.st_ino or st.st_size < pos:     # rotated or truncated
        pos = 0
    if st.st_size == pos:
        return []
    start = max(pos, st.st_size - MAX_READ)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(st.st_size - start)
    end = data.rfind(b"\n")
    if end < 0:                                   # no complete line yet
        offsets[key] = {"pos": pos, "inode": st.st_ino}
        return []
    offsets[key] = {"pos": start + end + 1, "inode": st.st_ino}
    lines = data[:end].decode("utf-8", "replace").split("\n")
    if start > pos and lines:
        lines = lines[1:]                         # the first one was cut
    return lines


def normalise(text: str) -> str:
    t = STAMP.sub("", text.strip())
    t = re.sub(r"^tick( error)?:\s*", "", t)
    t = re.sub(r"https?://\S+", "<url>", t)
    t = re.sub(r"/(Users|private|var|tmp)/\S+", "<path>", t)
    t = re.sub(r"\b[0-9a-f]{7,40}\b", "<hex>", t)
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"@\w+", "@<user>", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t[:160]


def _clauses(line: str) -> list[str]:
    """A summary line ("tick: a; b; c") holds several outcomes; judge each on its own."""
    body = re.sub(r"^tick( error)?:\s*", "", STAMP.sub("", line))
    return [c.strip() for c in body.split("; ")] if "; " in body else [body.strip()]


def extract(lines: list[str], proj: dict, source: str) -> list[Incident]:
    fail = re.compile(proj["fail"], re.I)
    ignore = [re.compile(p, re.I) for p in proj["ignore"]]
    found: dict[str, Incident] = {}

    def add(text: str, sig_text: str) -> None:
        sig = normalise(sig_text)
        for pat, rep in proj.get("sig_rewrite", []):
            sig = re.sub(pat, rep, sig)
        if not sig:
            return
        if sig in found:
            found[sig].count += 1
            if len(found[sig].examples) < 3:
                found[sig].examples.append(text[:2000])
        else:
            found[sig] = Incident(sig=sig, text=text[:4000], source=source, examples=[text[:2000]])

    i = 0
    while i < len(lines):
        if "Traceback (most recent call last)" in lines[i]:
            block, i = _traceback(lines, i)
            text = "\n".join(block)
            if not any(p.search(text) for p in ignore):
                add(text, block[-1])
            continue
        for clause in _clauses(lines[i]):
            if fail.search(clause) and not any(p.search(clause) for p in ignore):
                add(clause, clause)
        i += 1
    return list(found.values())


CHAIN = ("Traceback", "During handling", "The above exception")


def _traceback(lines: list[str], i: int) -> tuple[list[str], int]:
    """The traceback starting at lines[i], through its final exception line (chained ones included).
    Returns (block, index after it)."""
    block = [lines[i]]
    i += 1
    while i < len(lines):
        ln = lines[i]
        block.append(ln)
        i += 1
        if ln[:1] in (" ", "\t") or not ln.strip() or ln.startswith(CHAIN):
            continue
        j = i                                   # an exception line: is another traceback chained after it?
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and lines[j].startswith(CHAIN[1:]):
            block += lines[i:j]
            i = j
            continue
        break
    while block and not block[-1].strip():
        block.pop()
    return block, i


def is_transient(text: str, patterns: list[str]) -> bool:
    """Judged on the error itself (a traceback's last line), not on everything printed around it."""
    last = text.strip().splitlines()[-1] if text.strip() else ""
    return any(re.search(p, last, re.I) for p in patterns)


def log_files(proj: dict) -> list[tuple[str, Path]]:
    return [(f"{proj['key']}:{rel}", Path(proj["dir"]) / rel) for rel in proj["logs"]]


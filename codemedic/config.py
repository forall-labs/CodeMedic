from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("CODEMEDIC_HOME", ROOT))      # state, runs and logs (tests and trials use another)
RUNS = HOME / "runs"
LOGS = HOME / "logs"
STATE = HOME / "state.json"
LOCK = HOME / "codemedic.lock"
EXAMPLE = ROOT / "config.example.toml"


def load(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("CODEMEDIC_CONFIG", ROOT / "config.toml"))
    if not path.exists():
        raise SystemExit(f"no config at {path}: copy config.example.toml to config.toml and describe your projects")
    cfg = tomllib.loads(path.read_text())
    cfg.setdefault("projects", {})
    for key, p in cfg["projects"].items():
        p["key"] = key
        p["dir"] = str(Path(p["dir"]).expanduser())
        p.setdefault("name", key)
        p.setdefault("logs", [])
        p.setdefault("ignore", [])
        p.setdefault("protected", [".env"])
        p.setdefault("fail", r"\berror\b|\bfailed\b|traceback")
        p.setdefault("push", False)
        p.setdefault("notes", "")
    return cfg


def env_file(path: Path) -> dict[str, str]:
    """KEY=value lines (quotes stripped). Values are never logged."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out

"""Reports go through the failing project's own Slack app (SLACK_BOT_TOKEN in its .env), as a DM to
the author: a message when a session starts, edited in place with the outcome. Without a token or a
[codemedic] slack_user, CodeMedic stays silent and the run directory is the only record."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .config import env_file


def _token(proj: dict) -> str | None:
    return env_file(Path(proj["dir"]) / ".env").get("SLACK_BOT_TOKEN")


def _api(token: str, method: str, body: dict) -> dict:
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def post(cfg: dict, proj: dict, text: str) -> tuple[str, str] | None:
    """(channel, ts) of the new message, or None when the project has no Slack app or Slack failed."""
    tok, user = _token(proj), cfg["codemedic"].get("slack_user")
    if not tok or not user:
        return None
    try:
        r = _api(tok, "chat.postMessage", {"channel": user, "text": text[:3900],
                                           "unfurl_links": False, "unfurl_media": False})
    except Exception:
        return None
    return (r["channel"], r["ts"]) if r.get("ok") else None


def update(proj: dict, where: tuple[str, str] | None, text: str, cfg: dict) -> None:
    """Edit the start message; post a new one when there is none."""
    tok = _token(proj)
    if not tok:
        return
    if where:
        try:
            if _api(tok, "chat.update", {"channel": where[0], "ts": where[1], "text": text[:3900]}).get("ok"):
                return
        except Exception:
            pass
    post(cfg, proj, text)

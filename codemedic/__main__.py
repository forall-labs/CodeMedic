"""python -m codemedic tick | status | heal <project> | install | uninstall | test-slack <project> | unmute"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config, heal, slack, watch
from .watch import Incident

LABEL = "io.github.forall-labs.codemedic"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DAY = timedelta(hours=24)


def now() -> datetime:
    return datetime.now().astimezone()


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def log(msg: str) -> None:
    config.LOGS.mkdir(exist_ok=True)
    line = f"{iso(now())} {msg}"
    with open(config.LOGS / "codemedic.log", "a") as f:
        f.write(line + "\n")
    print(line)


def load_state() -> dict:
    try:
        s = json.loads(config.STATE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    for k in ("offsets", "sigs", "pending", "sessions", "alerts"):
        s.setdefault(k, {} if k != "sessions" else [])
    return s


def save_state(s: dict) -> None:
    tmp = config.STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(config.STATE)


def recent(times: list[str], t: datetime) -> list[str]:
    return [x for x in times if t - parse(x) < DAY]


# ---- one cycle ------------------------------------------------------------------------------

def scan(cfg: dict, state: dict, t: datetime) -> None:
    """Read new log lines; record every failure and queue it."""
    for key, proj in cfg["projects"].items():
        for lkey, path in watch.log_files(proj):
            lines = watch.read_new(path, state["offsets"], lkey)
            for inc in watch.extract(lines, proj, path.name):
                sk = f"{key}|{inc.sig}"
                sig = state["sigs"].setdefault(sk, {"first": iso(t), "hits": [], "heals": []})
                sig["hits"] = recent(sig["hits"], t) + [iso(t)] * min(inc.count, 20)
                sig["last"] = iso(t)
                q = state["pending"].setdefault(key, {})
                if sk in q:
                    q[sk]["count"] += inc.count
                    q[sk]["examples"] = (q[sk]["examples"] + inc.examples)[-3:]
                else:
                    q[sk] = {**inc.__dict__, "queued": iso(t)}


def eligible(cfg: dict, state: dict, key: str, t: datetime) -> tuple[list[Incident], list[str]]:
    """The queued incidents of one project that a session should take now, and alerts to send
    (errors that came back after the daily number of sessions for them)."""
    m = cfg["codemedic"]
    take, alerts = [], []
    q = state["pending"].get(key, {})
    for sk, item in list(q.items()):
        sig = state["sigs"].get(sk, {"hits": [], "heals": []})
        if t - parse(item["queued"]) > DAY:
            q.pop(sk)
            continue
        muted = parse(sig.get("muted_until"))
        if muted and muted > t:
            q.pop(sk)
            continue
        if len(recent([h["ts"] for h in sig["heals"]], t)) >= m["max_heals_per_error_per_day"]:
            sig["muted_until"] = iso(t + timedelta(hours=m["mute_hours"]))
            alerts.append(item["text"].splitlines()[-1][:300])
            q.pop(sk)
            continue
        if watch.is_transient(item["text"], m["transient"]) and len(recent(sig["hits"], t)) < m["transient_threshold"]:
            continue                                   # stays queued until it repeats (or expires)
        take.append(Incident(sig=item["sig"], text=item["text"], source=item["source"], count=item["count"],
                             examples=item["examples"]))
    return take, alerts


def cycle(cfg: dict, state: dict, dry_run: bool = False, only: str | None = None) -> str:
    m, t = cfg["codemedic"], now()
    scan(cfg, state, t)
    paused = parse(state.get("paused_until"))
    if paused and paused > t:
        return f"paused until {paused:%H:%M} (usage limit)"
    sessions = [s for s in state["sessions"] if t - parse(s["ts"]) < DAY]
    done = []
    for key, proj in cfg["projects"].items():
        if only and key != only:
            continue
        take, alerts = eligible(cfg, state, key, t)
        for a in alerts:
            done.append(f"{key}: gave up on a repeating error")
            if not dry_run:
                slack.post(cfg, proj, f":warning: *CodeMedic · {proj['name']}*: this error keeps coming back after "
                                      f"{m['max_heals_per_error_per_day']} sessions today, so CodeMedic stops trying "
                                      f"for {m['mute_hours']} h. Please take a look:\n`{a}`")
        if not take:
            continue
        if len(sessions) >= m["max_sessions_per_day"] or \
                sum(s["project"] == key for s in sessions) >= m["max_sessions_per_project_per_day"]:
            alert_key = f"cap|{key}|{t:%Y-%m-%d}"
            if alert_key not in state["alerts"] and not dry_run:
                state["alerts"][alert_key] = iso(t)
                slack.post(cfg, proj, f":warning: *CodeMedic · {proj['name']}*: daily limit of healing sessions reached; "
                                      f"{len(take)} error(s) wait for you or tomorrow:\n`{take[0].text.splitlines()[-1][:300]}`")
            done.append(f"{key}: {len(take)} waiting (daily session limit)")
            continue
        if dry_run:
            done.append(f"{key}: would heal {len(take)}: " + " | ".join(i.sig[:80] for i in take))
            continue
        log(f"heal {key}: {[i.sig for i in take]}")
        out = heal.heal(cfg, proj, take)
        if out["outcome"] == "deferred":
            if out.get("usage_limited"):
                state["paused_until"] = iso(t + timedelta(hours=1))
            elif out.get("offline"):
                state["paused_until"] = iso(t + timedelta(minutes=20))
            done.append(f"{key}: deferred ({out.get('summary')})")
            break
        state["sessions"].append({"ts": iso(t), "project": key, "run": out["run"], "outcome": out["outcome"]})
        for i in take:
            sk = f"{key}|{i.sig}"
            sig = state["sigs"].setdefault(sk, {"first": iso(t), "hits": [], "heals": []})
            sig["heals"] = [h for h in sig["heals"] if t - parse(h["ts"]) < DAY] + [{"ts": iso(t), "outcome": out["outcome"]}]
            if out["outcome"] in ("needs_author", "gave_up"):
                sig["muted_until"] = iso(t + timedelta(hours=m["mute_hours"]))
            state["pending"].get(key, {}).pop(sk, None)
        # What the session itself wrote to the logs (its retries, its experiments) is not a new incident.
        for lkey, path in watch.log_files(proj):
            if path.exists():
                st = path.stat()
                state["offsets"][lkey] = {"pos": st.st_size, "inode": st.st_ino}
        done.append(f"{key}: {out['outcome']}: {out.get('summary', '')[:200]} ({out['run']})")
        break                                          # one session per cycle
    state["sigs"] = {k: v for k, v in state["sigs"].items()
                     if t - parse(v.get("last") or v["first"]) < 7 * DAY}
    state["sessions"] = [s for s in state["sessions"] if t - parse(s["ts"]) < 7 * DAY]
    return "; ".join(done) or "all quiet"


# ---- commands -----------------------------------------------------------------------------------

def cmd_tick(dry_run: bool, only: str | None) -> None:
    config.RUNS.mkdir(parents=True, exist_ok=True)
    lock = open(config.LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("a healing session is running")
        return
    cfg, state = config.load(), load_state()
    try:
        msg = cycle(cfg, state, dry_run, only)
    finally:
        if not dry_run:
            save_state(state)
    if msg != "all quiet" or os.environ.get("CODEMEDIC_VERBOSE"):
        log(("dry run: " if dry_run else "tick: ") + msg)


def cmd_status() -> None:
    cfg, state, t = config.load(), load_state(), now()
    print(f"paused_until={state.get('paused_until')}")
    for s in state["sessions"][-10:]:
        print(f"session {s['ts']} {s['project']}: {s['outcome']} {s['run']}")
    for key, q in state["pending"].items():
        for sk, item in q.items():
            print(f"queued {key}: {item['sig'][:100]} (x{item['count']})")
    for sk, sig in state["sigs"].items():
        if sig.get("muted_until") and parse(sig["muted_until"]) > t:
            print(f"muted until {sig['muted_until']}: {sk[:120]}")
    print(f"watching {sum(len(p['logs']) for p in cfg['projects'].values())} logs in {len(cfg['projects'])} projects")


def cmd_heal(key: str, text: str, dry_run: bool) -> None:
    """Start a session now on the given error text (or the project's queued incidents)."""
    cfg, state = config.load(), load_state()
    proj = cfg["projects"][key]
    if text:
        take = [Incident(sig=watch.normalise(text), text=text, source="manual", examples=[text])]
    else:
        take = [Incident(**{k: v for k, v in i.items() if k != "queued"}) for i in state["pending"].get(key, {}).values()]
    if not take:
        sys.exit(f"nothing queued for {key}; pass the error text")
    out = heal.heal(cfg, proj, take, dry_run=dry_run)
    print(json.dumps(out, indent=1))


def cmd_install() -> None:
    if sys.platform != "darwin":
        print("install sets up a macOS launchd job. Elsewhere, run a tick every 10 minutes from cron, e.g.\n"
              f"*/10 * * * * cd {config.ROOT} && {sys.executable} -m codemedic tick")
        return
    path = os.pathsep.join([str(Path(subprocess.run(["which", "claude"], capture_output=True, text=True)
                                     .stdout.strip() or "/usr/local/bin/claude").parent),
                            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    config.LOGS.mkdir(exist_ok=True)
    plist = {"Label": LABEL, "ProgramArguments": [sys.executable, "-m", "codemedic", "tick"],
             "WorkingDirectory": str(config.ROOT), "StartInterval": 600, "RunAtLoad": True,
             "EnvironmentVariables": {"PATH": path, "HOME": str(Path.home())},
             "StandardOutPath": str(config.LOGS / "launchd.out.log"),
             "StandardErrorPath": str(config.LOGS / "launchd.err.log")}
    PLIST.write_bytes(plistlib.dumps(plist))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    print(f"Installed {PLIST}: checks every 10 minutes.")


def cmd_uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Uninstalled.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m codemedic")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick", help="what launchd runs every 10 minutes")
    p.add_argument("--dry-run", action="store_true", help="show what would be healed; change nothing")
    p.add_argument("--project")
    sub.add_parser("status")
    p = sub.add_parser("heal", help="start a healing session now")
    p.add_argument("project")
    p.add_argument("text", nargs="?", default="", help="the error (default: what is queued)")
    p.add_argument("--dry-run", action="store_true", help="write the prompt only")
    sub.add_parser("unmute", help="forget mutes and queued incidents")
    p = sub.add_parser("test-slack")
    p.add_argument("project")
    p = sub.add_parser("hold", help=argparse.SUPPRESS)
    p.add_argument("lock")
    p.add_argument("run")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    a = ap.parse_args()
    if a.cmd == "tick":
        try:
            cmd_tick(a.dry_run, a.project)
        except Exception as e:
            log(f"tick error: {e!r}")
            raise
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "heal":
        cmd_heal(a.project, a.text, a.dry_run)
    elif a.cmd == "unmute":
        s = load_state()
        s["pending"] = {}
        for v in s["sigs"].values():
            v.pop("muted_until", None)
            v["heals"] = []
        save_state(s)
        print("cleared")
    elif a.cmd == "test-slack":
        cfg = config.load()
        print(slack.post(cfg, cfg["projects"][a.project], f":stethoscope: CodeMedic can reach you through "
                                                           f"{cfg['projects'][a.project]['name']}'s Slack app.")
              or "no Slack token in the project's .env, or Slack refused")
    elif a.cmd == "hold":
        heal.hold(a.lock, a.run)
    elif a.cmd == "install":
        cmd_install()
    elif a.cmd == "uninstall":
        cmd_uninstall()


if __name__ == "__main__":
    main()

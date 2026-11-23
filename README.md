<div align="center">

# CodeMedic

### Self-healing coding agents for unattended LLM pipelines

**Sajjad Pakdamansavoji**<br>
Forall Labs

[![Project page](https://img.shields.io/badge/Project-page-0072B2.svg)](https://forall-labs.github.io/CodeMedic/)
[![Tests](https://github.com/forall-labs/CodeMedic/actions/workflows/tests.yml/badge.svg)](https://github.com/forall-labs/CodeMedic/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgray.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)

</div>

---

Scheduled agents break while nobody is watching: a site changes its markup, an API changes its shape,
a network drop leaves work half done. **CodeMedic** reads each project's logs, turns new failures
into incidents and, when one needs fixing, starts a headless [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
session inside that project to find the cause, fix it, add a test and finish the work the failure
left stuck. It keeps a fix only if the project's own tests still pass, and tells the author on Slack,
with a one-line undo.

## Highlights

From two weeks of use (8 to 23 November 2026) on four of the author's unattended agents: a
cross-platform publishing pipeline, an open-source PR agent, an awesome-list maintainer and a daily digest.

- **32 sessions.** Each one held the project's lock and ran its tests before the agent started.
- **5 code fixes, 0 reverts.** CodeMedic re-ran the project's tests after every fix, and none broke them
  (for example: an editor turned a hyphen in a title into an en dash, so a check comparing titles failed).
- **24 transient causes left alone.** Most were network outages; those sessions changed no code.
- **About $0.46 and 71 seconds** per session (medians), with a hard budget of $8 and 45 minutes.

## How it works

1. **Watch.** Every 10 minutes, read only what each log gained since the last look. Failure lines and
   Python tracebacks become incidents, keyed by a signature that ignores ids, numbers, URLs and paths,
   so one bug on many items is one incident. Errors that usually clear up on their own (network,
   rate limits) wait until they repeat.
2. **Heal.** Hold the project's tick lock (its own scheduled runs wait), run its tests, then start
   `claude -p` in the project with the incident, the recent commits, the files the author is editing,
   a budget cap and a guard hook. The session classifies the failure (transient, bug, needs the
   author), fixes the root cause with the smallest change, adds a test, commits explicit paths with a
   `CodeMedic-Run` trailer and re-runs the step the failure stopped.
3. **Verify.** CodeMedic re-runs the tests itself. If the session's commits broke tests that passed
   before, it reverts them; otherwise it pushes. The author gets one Slack message with the cause, the
   fix and the `git revert` line, and the prompt, transcript and report stay in `runs/`.

## Guard rails

The guard ([`codemedic/guard.py`](codemedic/guard.py)) is a Claude Code `PreToolUse` hook that blocks,
whatever the session was told:

- `git push`, reset, checkout, stash and rebase, `add -A` and amending commits (CodeMedic pushes after its own test run);
- `gh`, `sudo`, `launchctl` and the keychain; installing packages; non-GET HTTP requests;
- reading or touching `.env` files, Claude logins, SSH and cloud credentials; MCP tools;
- deleting files outside the run directory, and deleting database rows;
- edits outside the project, to its protected files (configuration, content), or to files with the
  author's uncommitted changes.

The instructions add the rest ([`codemedic/heal.py`](codemedic/heal.py) `SYSTEM`): public actions only by
re-running the project's own step for something it was already doing, after checking it did not go out
already; never change caps, limits, schedules or who gets contacted; when only the author can fix
something, say exactly what to do. Limits cap the cost: 2 sessions per error per day (then the error is
muted and the author alerted), 3 per project and 4 in all.

## Installation

```bash
git clone https://github.com/forall-labs/CodeMedic && cd CodeMedic
cp config.example.toml config.toml        # describe your projects (see the comments)
python3 -m codemedic tick --dry-run       # what would be healed now; changes nothing
python3 -m codemedic install              # macOS: a launchd job every 10 minutes
```

It needs Python 3.11+, git and the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI,
and nothing else. On Linux, run `python3 -m codemedic tick` from cron every 10 minutes instead of `install`.

A project fits CodeMedic when it has a git repository, log files, a test command and, ideally, a tick
lock (a file its scheduled runs `flock`) so a session never races the project's own runs.

## Commands

```bash
python3 -m codemedic status                    # sessions, queued and muted errors
python3 -m codemedic tick [--dry-run]          # one cycle (what launchd runs)
python3 -m codemedic heal my-app "the error"   # start a session by hand
python3 -m codemedic unmute                    # forget mutes and the queue
python3 -m codemedic test-slack my-app
python3 -m unittest discover -s tests          # 15 tests, including end-to-end runs with a fake claude
```

## Repository layout

```
codemedic/      watch.py (logs to incidents), heal.py (one session), guard.py (the hook),
                slack.py (reports), __main__.py (the cycle, limits and commands)
tests/          signatures, the guard's allow/deny lists, the cycle's limits, and heal() end to end
                against a fake claude and a local git remote (push, revert, lock, offline)
website/        the project page (GitHub Pages)
```

## Citation

```bibtex
@misc{pakdamansavoji2026codemedic,
  title        = {CodeMedic: Self-Healing Coding Agents for Unattended LLM Pipelines},
  author       = {Pakdamansavoji, Sajjad},
  year         = {2026},
  howpublished = {\url{https://github.com/forall-labs/CodeMedic}}
}
```

## License

The code is released under the [MIT License](LICENSE). Claude Code keeps its own terms.

## Acknowledgements

The healing sessions run on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) in headless
mode, with its hooks doing the guarding.

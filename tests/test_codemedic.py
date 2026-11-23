import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codemedic import __main__ as cli, config, heal, watch  # noqa: E402
from codemedic.watch import Incident  # noqa: E402

PROJ = {"key": "app", "name": "App", "fail": r"\berror\b|\bfailed\b|traceback",
        "ignore": ["not ready", r"\bwaiting\b"], "logs": [], "protected": ["config.toml", ".env", "styles/"]}


class Watch(unittest.TestCase):
    def test_first_look_records_the_end_then_reads_only_new_complete_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.log"
            p.write_text("old error\n")
            off = {}
            self.assertEqual(watch.read_new(p, off, "k"), [])
            with open(p, "a") as f:
                f.write("new 1\nnew 2\npartial")
            self.assertEqual(watch.read_new(p, off, "k"), ["new 1", "new 2"])
            with open(p, "a") as f:
                f.write(" line\n")
            self.assertEqual(watch.read_new(p, off, "k"), ["partial line"])
            p.write_text("rotated\n")                       # truncated: start again from the top
            self.assertEqual(watch.read_new(p, off, "k"), ["rotated"])

    def test_a_tick_summary_line_yields_only_the_failing_clauses(self):
        line = ("2026-11-06T12:59:40 tick: #47 blog publish error: 'str' object has no attribute 'get'; "
                "#48 forum published https://forum.example.com/posts/178116076044617; "
                "#8 forum not ready: post limit reached; website: pushed (Blog: update)")
        incs = watch.extract([line], PROJ, "app.log")
        self.assertEqual([i.text for i in incs], ["#47 blog publish error: 'str' object has no attribute 'get'"])

    def test_the_same_error_on_another_post_has_the_same_signature(self):
        a = watch.extract(["2026-11-07T13:44:24 tick: #53 x publish error: X post 3 was sent but not found: "
                           "[{\"status\": \"ok\", \"url\": \"https://social.example.com/u/status/2119137739580858545\"}]"],
                          PROJ, "l")[0]
        b = watch.extract(["2026-11-08T13:44:24 tick: #57 x publish error: X post 2 was sent but not found: "
                           "[{\"status\": \"ok\", \"url\": \"https://social.example.com/u/status/2119999999999999999\"}]"],
                          PROJ, "l")[0]
        self.assertEqual(a.sig, b.sig)

    def test_tracebacks_become_one_incident_keyed_by_the_exception(self):
        lines = textwrap.dedent("""\
            [21:14:51] scan
            Traceback (most recent call last):
              File "x.py", line 3, in <module>
                go()
            KeyError: 'posts'

            During handling of the above exception, another exception occurred:

            Traceback (most recent call last):
              File "x.py", line 5, in <module>
                raise ModelError(str(out))
            oss.llm.ModelError: API Error: bad
            [21:15:00] next step fine""").splitlines()
        incs = watch.extract(lines, {**PROJ, "fail": "traceback|^\\w*error:"}, "out.log")
        self.assertEqual(len(incs), 1)
        self.assertIn("KeyError", incs[0].text)
        self.assertTrue(incs[0].sig.startswith("oss.llm.modelerror"))

    def test_transient(self):
        cfg = config.load(config.EXAMPLE)
        self.assertTrue(watch.is_transient("oss.gh.GitHubError: error connecting to api.github.com",
                                           cfg["codemedic"]["transient"]))
        self.assertFalse(watch.is_transient("#502 x publish error: X post 3 failed: []", cfg["codemedic"]["transient"]))


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.run = Path(self.tmp.name) / "run"
        self.proj.mkdir()
        self.run.mkdir()
        (self.run / "rules.json").write_text(json.dumps({
            "project": str(self.proj), "run": str(self.run), "protected": ["config.toml", "styles/", ".env"],
            "dirty": ["app/wip.py"], "hold_pid_file": str(self.run / "hold.pid")}))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, tool, inp):
        p = subprocess.run([sys.executable, "-m", "codemedic.guard"], cwd=ROOT, capture_output=True, text=True,
                           input=json.dumps({"tool_name": tool, "tool_input": inp, "cwd": str(self.proj)}),
                           env=dict(os.environ, CODEMEDIC_RUN=str(self.run)))
        return p.returncode, p.stderr

    def assertDenied(self, tool, inp):
        code, err = self.call(tool, inp)
        self.assertEqual(code, 2, f"{tool} {inp} was allowed")

    def assertAllowed(self, tool, inp):
        code, err = self.call(tool, inp)
        self.assertEqual(code, 0, f"{tool} {inp} was denied: {err}")

    def test_bash(self):
        for cmd in ["git push", "git push --force origin master", "git reset --hard HEAD~1", "git checkout -- x.py",
                    "git stash", "git add -A", "git add .", "git commit -am 'x'", "git commit --amend",
                    "gh pr merge 3", "sudo ls", "launchctl bootout gui/501/x", "pip install requests",
                    "curl -X POST https://slack.com/api/chat.postMessage", "curl -d a=b https://x",
                    "cat .env", "cat ~/.claude-alt/x", "cat ~/.claude/settings.json", "sqlite3 data/app.db 'delete from posts'",
                    "rm data/app.db", "rm -rf /", "pkill python", "kill 123", "echo x > config.toml",
                    "sed -i '' 's/a/b/' config.toml", "echo x >> app/wip.py"]:
            self.assertDenied("Bash", {"command": cmd})
        for cmd in ["git status", "git log --oneline -5", "git diff", "git add app/platforms/x.py tests/t.py",
                    "git commit -F - <<'EOF'\nFix\n\nCodeMedic-Run: r\nEOF", ".venv/bin/python -m pytest -q",
                    "curl -s https://blog.example.com/api/v1/archive", "sqlite3 data/app.db 'select * from posts'",
                    "sqlite3 data/app.db \"update kv set value='0' where key='publish_tries_53'\"",
                    f"rm {self.run}/scratch.py", f"kill $(cat {self.run}/hold.pid)", "ls 2>&1 > /dev/null",
                    ".venv/bin/python -m app publish 53"]:
            self.assertAllowed("Bash", {"command": cmd})

    def test_edits(self):
        self.assertAllowed("Edit", {"file_path": str(self.proj / "app/platforms/x.py")})
        self.assertAllowed("Write", {"file_path": str(self.run / "scratch.py")})
        for p in [self.proj / "config.toml", self.proj / "styles/post.md", self.proj / "app/wip.py",
                  self.proj / ".git/config", Path.home() / ".zshrc", self.proj / ".env"]:
            self.assertDenied("Edit", {"file_path": str(p)})
        self.assertDenied("Read", {"file_path": str(self.proj / ".env")})
        self.assertAllowed("Read", {"file_path": str(self.proj / "README.md")})
        self.assertDenied("mcp__claude-in-chrome__navigate", {"url": "https://example.com"})


class Cycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log = d / "proj" / "app.log"
        self.log.parent.mkdir()
        self.log.write_text("")
        self.cfg = config.load(config.EXAMPLE)
        self.cfg["projects"] = {"p": {**PROJ, "key": "p", "name": "P", "dir": str(d / "proj"), "logs": ["app.log"]}}
        self.state = {"offsets": {}, "sigs": {}, "pending": {}, "sessions": [], "alerts": {}}
        mock.patch.object(config, "LOGS", d / "logs").start()
        self.calls = []
        self.p1 = mock.patch.object(heal, "heal", side_effect=self.fake_heal)
        self.p2 = mock.patch.object(cli.slack, "post", return_value=None)
        self.p1.start()
        self.posts = self.p2.start()
        cli.cycle(self.cfg, self.state)                     # first look

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def fake_heal(self, cfg, proj, incidents, dry_run=False):
        self.calls.append([i.sig for i in incidents])
        return {"outcome": self.outcome, "run": "r", "summary": "s"}

    def write(self, *lines):
        with open(self.log, "a") as f:
            f.write("".join(f"2026-11-07T13:00:00 tick: {ln}\n" for ln in lines))

    def test_a_new_error_is_healed_once_and_a_repeat_stops_after_the_daily_number(self):
        self.outcome = "fixed"
        self.write("#53 x publish error: boom")
        cli.cycle(self.cfg, self.state)
        self.assertEqual(len(self.calls), 1)
        cli.cycle(self.cfg, self.state)                     # nothing new: nothing to do
        self.assertEqual(len(self.calls), 1)
        self.write("#54 x publish error: boom")
        cli.cycle(self.cfg, self.state)
        self.assertEqual(len(self.calls), 2)
        self.write("#55 x publish error: boom")
        cli.cycle(self.cfg, self.state)                     # third time today: alert, no session
        self.assertEqual(len(self.calls), 2)
        self.assertIn("keeps coming back", self.posts.call_args[0][2])

    def test_transient_errors_wait_until_they_repeat(self):
        self.outcome = "transient"
        for k in range(3):
            self.write(f"#1 fetch error: error connecting to api.github.com ({k})")
            cli.cycle(self.cfg, self.state)
        self.assertEqual(self.calls, [])
        self.write("#1 fetch error: error connecting to api.github.com (4)")
        cli.cycle(self.cfg, self.state)
        self.assertEqual(len(self.calls), 1)

    def test_needs_author_is_muted(self):
        self.outcome = "needs_author"
        self.write("#1 blog draft error: signed out")
        cli.cycle(self.cfg, self.state)
        self.write("#2 blog draft error: signed out")
        cli.cycle(self.cfg, self.state)
        self.assertEqual(len(self.calls), 1)

    def test_a_deferred_session_keeps_the_incident_queued(self):
        self.outcome = "deferred"
        self.write("#1 x publish error: boom")
        cli.cycle(self.cfg, self.state)
        self.outcome = "fixed"
        cli.cycle(self.cfg, self.state)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.state["pending"]["p"], {})


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
prompt = sys.stdin.read()
run = Path(os.environ["CODEMEDIC_RUN"])
run_id = run.name
mode = os.environ["FAKE_MODE"]
# the lock is held for us
import fcntl
f = open("data/tick.lock", "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); held = False
except BlockingIOError:
    held = True
Path("app.py").write_text("def add(a, b):\n    return a + b\n" if mode == "good" else "def add(a, b):\n    return 0\n")
subprocess.run(["git", "add", "app.py"], check=True)
subprocess.run(["git", "commit", "-q", "-m", f"Fix add\n\nCodeMedic-Run: {run_id}\nCo-Authored-By: Claude <noreply@anthropic.com>"], check=True)
(run / "report.json").write_text(json.dumps({"outcome": "fixed", "summary": "add works", "cause": "typo",
    "fix": "app.py", "carried_on": "", "author_action": "", "followups": "", "lock_was_held": held}))
print(json.dumps({"type": "result", "result": "done", "is_error": False, "total_cost_usd": 0.01}))
'''


class Session(unittest.TestCase):
    """heal() end to end with a fake claude: lock held, commits found, tests, push or revert."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.repo, self.remote = d / "proj", d / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (self.repo / "data").mkdir()
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "test_app.py").write_text("import unittest\nfrom app import add\nclass T(unittest.TestCase):\n"
                                               "    def test(self):\n        self.assertEqual(add(1, 1), 2)\n")
        (self.repo / ".gitignore").write_text("data/\n__pycache__/\n")
        g("add", ".")
        g("commit", "-q", "-m", "init")
        g("remote", "add", "origin", str(self.remote))
        g("push", "-q", "-u", "origin", "main")
        (self.repo / "notes.md").write_text("the author's work in progress\n")     # untracked: must stay so
        fake = d / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.cfg = config.load(config.EXAMPLE)
        self.cfg["codemedic"].update(claude_bin=str(fake), config_dir="", lock_wait_minutes=0.5)
        self.proj = {"key": "toy", "name": "Toy", "dir": str(self.repo), "logs": [], "lock": "data/tick.lock",
                     "test": f"{sys.executable} -m unittest -q test_app", "protected": [], "push": True, "notes": ""}
        self.runs = mock.patch.object(config, "RUNS", d / "runs")
        self.runs.start()
        mock.patch.object(heal.slack, "post", return_value=None).start()
        self.update = mock.patch.object(heal.slack, "update").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def heal(self, mode):
        with mock.patch.dict(os.environ, FAKE_MODE=mode):
            return heal.heal(self.cfg, self.proj, [Incident(sig="s", text="add is wrong", source="t")])

    def test_a_good_fix_is_pushed(self):
        out = self.heal("good")
        self.assertEqual(out["outcome"], "fixed")
        self.assertTrue(out["lock_was_held"])
        self.assertEqual(len(out["commits"]), 1)
        self.assertTrue(out["pushed"])
        remote_head = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"],
                                     capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_head, out["commits"][0])
        self.assertTrue((self.repo / "notes.md").exists())
        self.assertIn("Undo:", self.update.call_args[0][2])

    def test_a_fix_that_breaks_the_tests_is_reverted_and_not_pushed(self):
        # the tests fail before too (a - b): make them pass first so the regression is CodeMedic's
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "noop"], check=True)
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-am", "ok"], check=True)
        out = self.heal("bad")
        self.assertEqual(out["outcome"], "reverted")
        self.assertFalse(out["pushed"])
        self.assertIn("return a + b", (self.repo / "app.py").read_text())

    def test_an_offline_session_is_deferred_not_given_up(self):
        fake = Path(self.cfg["codemedic"]["claude_bin"])
        fake.write_text("#!/bin/sh\necho \"API Error: Can't reach the API server (ENOTFOUND)\" >&2\nexit 1\n")
        out = self.heal("good")
        self.assertEqual(out["outcome"], "deferred")
        self.assertTrue(out["offline"])

    def test_no_lock_means_try_later(self):
        import fcntl
        (self.repo / "data" / "tick.lock").touch()
        f = open(self.repo / "data" / "tick.lock", "a")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            out = self.heal("good")
        finally:
            f.close()
        self.assertEqual(out["outcome"], "deferred")


if __name__ == "__main__":
    unittest.main()

"""Offline unit tests for prune_sessions.py: which herdr agent bindings survive.

Run with:
    python3 -m unittest discover -s tests -t .

Worktrees and session files are built in a temp dir per test; the herd state
files come from the same fixtures resume.py's tests use.
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import prune_sessions

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATES = FIXTURES / "states"


class PruneCase(unittest.TestCase):
    """A fake home with worktrees, and a session.json binding panes to them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.session_path = self.home / ".config" / "herdr" / "session.json"
        self.session_path.parent.mkdir(parents=True)
        self.manifest = self.home / ".local" / "state" / "agentbox" / "pruned.tsv"
        self.transcripts = self.home / ".claude" / "projects"
        self._next = 0
        # No herdr server in a unit test, whatever the host happens to be running.
        patch = mock.patch.object(prune_sessions, "herdr_running", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def worktree(self, name, state="running", mtime=None) -> Path:
        """A worktree directory with a herd state file, or none if state is None."""
        wt = self.home / "git" / "wt" / "repo" / name
        wt.mkdir(parents=True, exist_ok=True)
        if state is not None:
            run = wt / ".herd" / "run"
            run.mkdir(parents=True, exist_ok=True)
            shutil.copy(STATES / f"{state}.state", run / "state")
            if mtime is not None:
                import os
                os.utime(run / "state", (mtime, mtime))
        return wt

    def pane(self, cwd, bound=True) -> dict:
        """One pane dict, as herdr saves it."""
        self._next += 1
        pane: dict = {"cwd": str(cwd)}
        if bound:
            pane.update(agent_name="claude", managed_agent_kind="claude",
                        agent_session={"source": "herdr:claude", "agent": "claude",
                                       "kind": "id", "value": f"session-{self._next:04d}"})
        return pane

    def write_session(self, panes: list) -> dict:
        """A session.json with one workspace per pane, panes keyed by id as herdr writes them."""
        session = {"version": 3, "workspaces": [
            {"id": f"ws{i}", "tabs": [{"root_pane": "1", "layout": {"pane": "1"},
                                       "panes": {"1": p}}]}
            for i, p in enumerate(panes)]}
        self.session_path.write_text(json.dumps(session))
        return session

    def report_rows(self, max_live=6, **kw) -> list:
        session = prune_sessions.load_session(self.session_path)
        return prune_sessions.decide(session, max_live, transcripts=self.transcripts, **kw)

    def transcript(self, pane, mtime) -> None:
        """A Claude transcript for the pane's session, last written at mtime."""
        import os
        d = self.transcripts / str(pane["cwd"]).replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{pane['agent_session']['value']}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (mtime, mtime))

    def apply(self, *extra) -> tuple:
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune_sessions.main(["apply", "--session", str(self.session_path),
                                        "--manifest", str(self.manifest),
                                        "--transcripts", str(self.transcripts), *extra], environ={})
        return code, out.getvalue()


class TestClassify(PruneCase):
    def test_in_progress_plan_is_live_and_kept(self):
        self.write_session([self.pane(self.worktree("feat-a", "running"))])
        rows = self.report_rows()
        self.assertEqual([(r["class"], r["action"]) for r in rows], [("live", "keep")])

    def test_complete_and_failed_plans_are_stripped(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete")),
                            self.pane(self.worktree("feat-bad", "failed"))])
        rows = self.report_rows()
        self.assertEqual({r["class"] for r in rows}, {"done"})
        self.assertEqual({r["action"] for r in rows}, {"strip"})

    def test_unknown_status_is_stripped_like_resume_skips_it(self):
        self.write_session([self.pane(self.worktree("feat-odd", "unknown-status"))])
        self.assertEqual(self.report_rows()[0]["class"], "done")

    def test_missing_directory_is_gone(self):
        self.write_session([self.pane(self.home / "git" / "wt" / "repo" / "never-existed")])
        row = self.report_rows()[0]
        self.assertEqual((row["class"], row["action"]), ("gone", "strip"))

    def test_directory_without_a_plan_is_noplan(self):
        self.write_session([self.pane(self.worktree("ad-hoc", state=None))])
        row = self.report_rows()[0]
        self.assertEqual((row["class"], row["action"]), ("noplan", "strip"))

    def test_garbled_state_is_not_treated_as_live(self):
        self.write_session([self.pane(self.worktree("feat-garbled", "garbled"))])
        self.assertEqual(self.report_rows()[0]["class"], "noplan")

    def test_unbound_panes_are_not_listed(self):
        self.write_session([self.pane(self.worktree("feat-a"), bound=False)])
        self.assertEqual(self.report_rows(), [])


class TestMaxLive(PruneCase):
    def test_live_bindings_over_the_cap_are_stripped_newest_kept(self):
        panes = [self.pane(self.worktree(f"feat-{i}", "running", mtime=1000 + i))
                 for i in range(5)]
        self.write_session(panes)
        rows = self.report_rows(max_live=2)
        kept = [r for r in rows if r["action"] == "keep"]
        self.assertEqual(len(kept), 2)
        self.assertEqual([r["mtime"] for r in kept], [1004, 1003])  # newest plans
        self.assertEqual(len([r for r in rows if r["action"] == "strip"]), 3)

    def test_zero_keeps_nothing(self):
        self.write_session([self.pane(self.worktree("feat-a", "running"))])
        self.assertEqual([r["action"] for r in self.report_rows(max_live=0)], ["strip"])

    def test_done_bindings_never_use_up_the_cap(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete")),
                            self.pane(self.worktree("feat-live", "running"))])
        rows = self.report_rows(max_live=1)
        kept = [r for r in rows if r["action"] == "keep"]
        self.assertEqual([r["class"] for r in kept], ["live"])


class TestAdhoc(PruneCase):
    """noplan bindings: the recently active ones survive a restart."""
    NOW = 1_000_000.0

    def adhoc(self, name, hours_ago=None) -> dict:
        pane = self.pane(self.worktree(name, state=None))
        if hours_ago is not None:
            self.transcript(pane, self.NOW - hours_ago * 3600)
        return pane

    def actions(self, **kw) -> dict:
        rows = self.report_rows(now=self.NOW, **kw)
        return {Path(r["cwd"]).name: r["action"] for r in rows}

    def test_recently_active_adhoc_session_is_kept(self):
        self.write_session([self.adhoc("recent", hours_ago=0.5)])
        self.assertEqual(self.actions(max_adhoc=6), {"recent": "keep"})

    def test_idle_adhoc_session_is_stripped(self):
        self.write_session([self.adhoc("stale", hours_ago=30)])
        self.assertEqual(self.actions(max_adhoc=6), {"stale": "strip"})

    def test_adhoc_hours_sets_the_window(self):
        self.write_session([self.adhoc("stale", hours_ago=30)])
        self.assertEqual(self.actions(max_adhoc=6, adhoc_hours=48), {"stale": "keep"})

    def test_no_transcript_is_stripped(self):
        self.write_session([self.adhoc("never-ran")])
        self.assertEqual(self.actions(max_adhoc=6), {"never-ran": "strip"})

    def test_most_recent_are_kept_up_to_the_cap(self):
        self.write_session([self.adhoc(f"s{h}", hours_ago=h) for h in (3, 1, 2)])
        self.assertEqual(self.actions(max_adhoc=2), {"s1": "keep", "s2": "keep", "s3": "strip"})

    def test_zero_keeps_no_adhoc_session(self):
        self.write_session([self.adhoc("recent", hours_ago=0.5)])
        self.assertEqual(self.actions(max_adhoc=0), {"recent": "strip"})

    def test_adhoc_and_live_caps_are_separate(self):
        live = self.pane(self.worktree("feat-live", "running"))
        self.write_session([live, self.adhoc("recent", hours_ago=1)])
        self.assertEqual(self.actions(max_live=1, max_adhoc=1),
                         {"feat-live": "keep", "recent": "keep"})

    def test_done_plan_is_stripped_even_with_a_fresh_transcript(self):
        pane = self.pane(self.worktree("feat-done", "complete"))
        self.transcript(pane, self.NOW - 60)
        self.write_session([pane])
        self.assertEqual(self.actions(max_adhoc=6), {"feat-done": "strip"})

    def test_apply_keeps_the_binding_by_default(self):
        import time
        pane = self.adhoc("recent")
        self.transcript(pane, time.time() - 60)
        self.write_session([pane, self.adhoc("never-ran")])
        code, out = self.apply()
        self.assertEqual(code, 0)
        saved = prune_sessions.bound_panes(json.loads(self.session_path.read_text()))
        self.assertEqual([p["cwd"] for p in saved], [pane["cwd"]])

    def test_negative_max_adhoc_is_rejected(self):
        self.write_session([self.adhoc("recent", hours_ago=1)])
        self.assertEqual(prune_sessions.main(
            ["report", "--session", str(self.session_path), "--max-adhoc", "-1"], environ={}), 2)


class TestApply(PruneCase):
    def test_strips_only_the_binding_keys_and_leaves_the_pane(self):
        wt = self.worktree("feat-done", "complete")
        self.write_session([self.pane(wt)])
        code, _ = self.apply()
        self.assertEqual(code, 0)
        session = json.loads(self.session_path.read_text())
        tab = session["workspaces"][0]["tabs"][0]
        pane = tab["panes"]["1"]
        self.assertEqual(pane, {"cwd": str(wt)})          # cwd kept, binding gone
        self.assertEqual(tab["root_pane"], "1")            # layout still resolves
        self.assertEqual(tab["layout"], {"pane": "1"})

    def test_keeps_a_live_binding_untouched(self):
        self.write_session([self.pane(self.worktree("feat-a", "running"))])
        before = json.loads(self.session_path.read_text())
        self.apply()
        self.assertEqual(json.loads(self.session_path.read_text()), before)

    def test_backs_up_before_rewriting(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        before = self.session_path.read_text()
        self.apply()
        backups = list(self.session_path.parent.glob("session.json.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)

    def test_manifest_records_every_stripped_id(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete")),
                            self.pane(self.worktree("feat-live", "running"))])
        self.apply()
        lines = [l for l in self.manifest.read_text().splitlines() if not l.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertIn("session-0001", lines[0])
        self.assertIn("done", lines[0])

    def test_leaves_no_temp_file_behind(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        self.apply()
        self.assertEqual(list(self.session_path.parent.glob("*.tmp")), [])

    def test_nothing_to_strip_writes_no_backup(self):
        self.write_session([self.pane(self.worktree("feat-a", "running"))])
        code, out = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("nothing to strip", out)
        self.assertEqual(list(self.session_path.parent.glob("*.bak")), [])

    def test_refuses_while_a_herdr_server_is_running(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        with mock.patch.object(prune_sessions, "herdr_running", return_value=True):
            code, _ = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("agent_session", self.session_path.read_text())  # untouched

    def test_force_overrides_the_running_server_guard(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        with mock.patch.object(prune_sessions, "herdr_running", return_value=True):
            code, _ = self.apply("--force")
        self.assertEqual(code, 0)
        self.assertNotIn("agent_session", self.session_path.read_text())


class TestReportIsReadOnly(PruneCase):
    def test_report_changes_nothing(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        before = self.session_path.read_text()
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune_sessions.main(["report", "--session", str(self.session_path)], environ={})
        self.assertEqual(code, 0)
        self.assertEqual(self.session_path.read_text(), before)
        self.assertFalse(self.manifest.exists())

    def test_a_bare_run_reports_rather_than_applying(self):
        self.write_session([self.pane(self.worktree("feat-done", "complete"))])
        before = self.session_path.read_text()
        out = io.StringIO()
        with redirect_stdout(out):
            prune_sessions.main(["--session", str(self.session_path)], environ={})
        self.assertEqual(self.session_path.read_text(), before)


class TestBadInput(PruneCase):
    def test_missing_session_file_is_an_error_not_a_traceback(self):
        code = prune_sessions.main(["report", "--session", str(self.home / "nope.json")], environ={})
        self.assertEqual(code, 1)

    def test_invalid_json_is_an_error(self):
        self.session_path.write_text("{not json")
        self.assertEqual(prune_sessions.main(
            ["report", "--session", str(self.session_path)], environ={}), 1)

    def test_session_without_workspaces_is_an_error(self):
        self.session_path.write_text('{"version": 3}')
        self.assertEqual(prune_sessions.main(
            ["report", "--session", str(self.session_path)], environ={}), 1)

    def test_panes_as_a_list_are_handled_too(self):
        wt = self.worktree("feat-done", "complete")
        self.session_path.write_text(json.dumps({"version": 3, "workspaces": [
            {"tabs": [{"panes": [self.pane(wt)]}]}]}))
        self.assertEqual([r["class"] for r in self.report_rows()], ["done"])

    def test_bad_max_live_env_falls_back(self):
        self.assertEqual(prune_sessions.max_live_default({"AGENTBOX_MAX_LIVE_AGENTS": "lots"}),
                         prune_sessions.DEFAULT_MAX_LIVE)

    def test_negative_max_live_is_rejected(self):
        self.write_session([self.pane(self.worktree("feat-a", "running"))])
        self.assertEqual(prune_sessions.main(
            ["report", "--session", str(self.session_path), "--max-live", "-1"], environ={}), 2)


if __name__ == "__main__":
    unittest.main()

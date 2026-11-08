"""Offline unit tests for resume.py: which herd plans the entrypoint resumes.

Run with:
    python3 -m unittest discover -s tests -t .

The fixtures under fixtures/ are static state and args files. Worktrees are
built in a temp dir per test, because git refuses to track a file named .git.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import resume

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATES = FIXTURES / "states"
ARGS = FIXTURES / "args"


class TreeCase(unittest.TestCase):
    """A fake home: <tmp>/git/<repo> source checkouts, <tmp>/git/wt/<repo>/<name> worktrees."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.root = self.home / "git" / "wt"
        self.args_dir = self.home / ".local" / "state" / "agentbox" / "plans"

    def worktree(self, branch, state="running", repo="homelab-gitops", head=None):
        """A linked worktree as git lays it out, with a herd state file copied from fixtures."""
        name = resume.agent_name(branch)
        src = self.home / "git" / repo
        gitdir = src / ".git" / "worktrees" / name
        gitdir.mkdir(parents=True, exist_ok=True)
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "HEAD").write_text(head if head is not None else f"ref: refs/heads/{branch}\n")
        wt = self.root / repo / name
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        (gitdir / "gitdir").write_text(f"{wt}/.git\n")
        if state is not None:
            run = wt / ".herd" / "run"
            run.mkdir(parents=True)
            shutil.copy(STATES / f"{state}.state", run / "state")
        return wt

    def sidecar(self, branch, fixture):
        self.args_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(ARGS / f"{fixture}.args", self.args_dir / f"{resume.agent_name(branch)}.args")

    def scan(self, **kw):
        return resume.scan(self.root, self.args_dir, **kw)


class AgentNameTest(unittest.TestCase):
    """Must equal herd's agent_name(): tr 'A-Z/._' 'a-z---' | tr -cd 'a-z0-9_-', strip one leading [-_0-9], 32 chars."""

    def test_slashes_and_case(self):
        self.assertEqual(resume.agent_name("feat/AgentBox"), "feat-agentbox")

    def test_dots_and_underscores_become_dashes(self):
        self.assertEqual(resume.agent_name("fix/v1.2_x"), "fix-v1-2-x")

    def test_other_characters_dropped(self):
        self.assertEqual(resume.agent_name("feat/a+b@c"), "feat-abc")

    def test_one_leading_digit_or_dash_stripped(self):
        self.assertEqual(resume.agent_name("12-x"), "2-x")
        self.assertEqual(resume.agent_name("/x"), "x")

    def test_truncated_to_32(self):
        self.assertEqual(resume.agent_name("feat/" + "a" * 40), "feat-" + "a" * 27)


class ParseStateTest(unittest.TestCase):
    def test_full_state(self):
        st = resume.parse_state((STATES / "running.state").read_text())
        self.assertEqual(st["status"], "running")
        self.assertEqual(st["stage"], "2")
        self.assertEqual(st["kind"], "claude")
        self.assertEqual(st["verify"], "make test")
        self.assertEqual(st["bgwait"], "60")

    def test_value_may_contain_equals(self):
        st = resume.parse_state("stage=1\nstatus=running\nverify=FOO=1 make test\n")
        self.assertEqual(st["verify"], "FOO=1 make test")

    def test_older_herd_without_bgwait(self):
        st = resume.parse_state((STATES / "running-codex-old-herd.state").read_text())
        self.assertEqual(st["kind"], "codex")
        self.assertNotIn("bgwait", st)

    def test_kind_defaults_to_claude(self):
        self.assertEqual(resume.parse_state("stage=1\nstatus=running\n")["kind"], "claude")

    def test_garbled_raises(self):
        with self.assertRaises(resume.StateError):
            resume.parse_state((STATES / "garbled.state").read_text(errors="replace"))

    def test_empty_raises(self):
        with self.assertRaises(resume.StateError):
            resume.parse_state((STATES / "empty.state").read_text())

    def test_missing_status_raises(self):
        with self.assertRaises(resume.StateError):
            resume.parse_state((STATES / "no-status.state").read_text())


class DecisionTest(TreeCase):
    def test_in_progress_statuses_resume(self):
        for status in ("planning", "replanning", "pending", "running", "blocked", "fixing", "verified"):
            with self.subTest(status=status):
                wt = self.worktree(f"feat/{status}", state=status)
                plan = resume.decide(wt / ".herd/run/state", self.args_dir)
                self.assertEqual(plan["action"], "resume", plan)
                self.assertEqual(plan["status"], status)

    def test_complete_and_failed_skip(self):
        for status in ("complete", "failed"):
            with self.subTest(status=status):
                wt = self.worktree(f"feat/{status}", state=status)
                plan = resume.decide(wt / ".herd/run/state", self.args_dir)
                self.assertEqual(plan["action"], "skip")
                self.assertIn(status, plan["reason"])

    def test_unknown_status_skips_with_reason(self):
        wt = self.worktree("feat/odd", state="unknown-status")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["action"], "skip")
        self.assertIn("exploded", plan["reason"])

    def test_garbled_state_is_an_error_report(self):
        wt = self.worktree("feat/bad", state="garbled")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["action"], "error")
        self.assertEqual(plan["worktree"], str(wt))
        self.assertTrue(plan["reason"])

    def test_missing_state_file_is_an_error_report(self):
        wt = self.worktree("feat/gone", state=None)
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["action"], "error")
        self.assertIn("state", plan["reason"])

    def test_plan_fields(self):
        wt = self.worktree("feat/agentbox", state="running")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["branch"], "feat/agentbox")
        self.assertEqual(plan["agent"], "feat-agentbox")
        self.assertEqual(plan["worktree"], str(wt))
        self.assertEqual(plan["repo"], str(self.home / "git" / "homelab-gitops"))
        self.assertEqual(plan["kind"], "claude")
        self.assertEqual(plan["stage"], "2")

    def test_branch_with_slashes_from_head(self):
        wt = self.worktree("feat/a/b", state="pending")
        self.assertEqual(resume.decide(wt / ".herd/run/state", self.args_dir)["branch"], "feat/a/b")

    def test_detached_head_is_an_error(self):
        wt = self.worktree("feat/det", state="running", head="0123456789abcdef0123456789abcdef01234567\n")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["action"], "error")
        self.assertIn("branch", plan["reason"])

    def test_relative_gitdir_and_commondir(self):
        wt = self.worktree("feat/rel", state="running")
        gitdir = self.home / "git" / "homelab-gitops" / ".git" / "worktrees" / "feat-rel"
        (wt / ".git").write_text("gitdir: ../../../homelab-gitops/.git/worktrees/feat-rel\n")
        self.assertTrue((wt / "../../../homelab-gitops/.git/worktrees/feat-rel").resolve() == gitdir.resolve())
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["branch"], "feat/rel")
        self.assertEqual(plan["repo"], str((self.home / "git" / "homelab-gitops").resolve()))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitFallbackTest(TreeCase):
    def test_real_git_worktree_resolved_without_or_with_git(self):
        src = self.home / "git" / "repo"
        src.mkdir(parents=True)
        g = ["git", "-c", "user.email=a@b", "-c", "user.name=a"]
        subprocess.run(g + ["init", "-q", "-b", "main", str(src)], check=True)
        subprocess.run(g + ["-C", str(src), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wt = self.root / "repo" / "feat-x"
        subprocess.run(g + ["-C", str(src), "worktree", "add", "-q", "-b", "feat/x", str(wt)], check=True)
        (wt / ".herd" / "run").mkdir(parents=True)
        shutil.copy(STATES / "running.state", wt / ".herd/run/state")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["action"], "resume", plan)
        self.assertEqual(plan["branch"], "feat/x")
        self.assertEqual(Path(plan["repo"]).resolve(), src.resolve())

    def test_git_fallback_when_dotgit_is_unreadable(self):
        src = self.home / "git" / "repo2"
        src.mkdir(parents=True)
        g = ["git", "-c", "user.email=a@b", "-c", "user.name=a"]
        subprocess.run(g + ["init", "-q", "-b", "main", str(src)], check=True)
        subprocess.run(g + ["-C", str(src), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wt = self.root / "repo2" / "feat-y"
        subprocess.run(g + ["-C", str(src), "worktree", "add", "-q", "-b", "feat/y", str(wt)], check=True)
        # The direct reader gives up (e.g. a layout it doesn't know), so git -C has to answer.
        branch, repo = resume.worktree_git_info(wt, gitdir_reader=lambda _wt: None)
        self.assertEqual(branch, "feat/y")
        self.assertEqual(Path(repo).resolve(), src.resolve())


class AgentArgsTest(TreeCase):
    def test_no_sidecar_means_no_permission_flag(self):
        wt = self.worktree("feat/plain", state="running")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["saved_args"], [])
        self.assertFalse(plan["permission_auto"])
        self.assertEqual(plan["agent_args"], ["--continue"])

    def test_sidecar_with_permission_auto(self):
        wt = self.worktree("feat/auto", state="running")
        self.sidecar("feat/auto", "permission-auto")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["saved_args"], ["--permission-mode", "auto"])
        self.assertTrue(plan["permission_auto"])
        self.assertEqual(plan["agent_args"], ["--continue", "--permission-mode", "auto"])

    def test_sidecar_without_permission_flag(self):
        wt = self.worktree("feat/model", state="running")
        self.sidecar("feat/model", "model-sonnet")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertFalse(plan["permission_auto"])
        self.assertEqual(plan["agent_args"], ["--continue", "--model", "sonnet"])

    def test_permission_mode_equals_form(self):
        self.assertTrue(resume.has_permission_auto(["--permission-mode=auto"]))
        self.assertFalse(resume.has_permission_auto(["--permission-mode", "plan"]))
        self.assertFalse(resume.has_permission_auto(["auto"]))

    def test_non_claude_kind_gets_no_continue(self):
        wt = self.worktree("feat/codex", state="running-codex-old-herd")
        plan = resume.decide(wt / ".herd/run/state", self.args_dir)
        self.assertEqual(plan["kind"], "codex")
        self.assertEqual(plan["agent_args"], [])

    def test_blank_lines_in_sidecar_ignored(self):
        self.args_dir.mkdir(parents=True)
        (self.args_dir / "x.args").write_text("--permission-mode\n\nauto\n")
        self.assertEqual(resume.read_saved_args(self.args_dir, "x"), ["--permission-mode", "auto"])


class ScanTest(TreeCase):
    def test_scan_finds_repo_nested_worktrees_and_reports_each(self):
        self.worktree("feat/a", state="running")
        self.worktree("feat/b", state="complete", repo="herd")
        self.worktree("feat/c", state="garbled")
        self.worktree("feat/d", state=None)  # no plan here: not reported at all
        plans = self.scan()
        by_branch = {p.get("branch") or Path(p["worktree"]).name: p["action"] for p in plans}
        self.assertEqual(by_branch, {"feat/a": "resume", "feat/b": "skip", "feat-c": "error"})

    def test_scan_also_finds_worktrees_directly_under_root(self):
        wt = self.root / "solo"
        src = self.home / "src"
        gitdir = src / ".git" / "worktrees" / "solo"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "HEAD").write_text("ref: refs/heads/solo\n")
        (wt / ".herd" / "run").mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        shutil.copy(STATES / "pending.state", wt / ".herd/run/state")
        self.assertEqual([p["branch"] for p in self.scan()], ["solo"])

    def test_scan_sorted_and_missing_root_is_empty(self):
        self.worktree("feat/z", state="running")
        self.worktree("feat/a", state="running")
        self.assertEqual([p["branch"] for p in self.scan()], ["feat/a", "feat/z"])
        self.assertEqual(resume.scan(self.home / "nope", self.args_dir), [])

    def test_main_prints_one_json_line_per_plan(self):
        self.worktree("feat/a", state="running")
        self.worktree("feat/b", state="failed")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = resume.main(["scan", "--root", str(self.root), "--args-dir", str(self.args_dir)])
        self.assertEqual(rc, 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([p["action"] for p in lines], ["resume", "skip"])

    def test_main_resume_only_filters(self):
        self.worktree("feat/a", state="running")
        self.worktree("feat/b", state="failed")
        self.worktree("feat/c", state="garbled")
        out = io.StringIO()
        with redirect_stdout(out):
            resume.main(["scan", "--root", str(self.root), "--args-dir", str(self.args_dir), "--resume-only"])
        self.assertEqual([json.loads(line)["branch"] for line in out.getvalue().splitlines()], ["feat/a"])

    def test_root_and_args_dir_from_env(self):
        self.worktree("feat/a", state="running")
        self.sidecar("feat/a", "permission-auto")
        env = {"AGENTBOX_WT_ROOT": str(self.root), "AGENTBOX_PLAN_ARGS_DIR": str(self.args_dir)}
        out = io.StringIO()
        with redirect_stdout(out):
            resume.main(["scan"], environ=env)
        self.assertTrue(json.loads(out.getvalue())["permission_auto"])


class RecordArgsTest(TreeCase):
    """`agentbox-plan` runs `resume.py record-args -- <herd plan argv>` before exec'ing herd plan."""

    def record(self, *argv):
        return resume.main(["record-args", "--args-dir", str(self.args_dir), "--", *argv])

    def test_args_after_double_dash_written_one_per_line(self):
        self.assertEqual(self.record("-v", "make test", "feat/x", "plan.md", "--", "--permission-mode", "auto"), 0)
        self.assertEqual((self.args_dir / "feat-x.args").read_text(), "--permission-mode\nauto\n")
        self.assertEqual((self.args_dir / "feat-x.args").stat().st_mode & 0o777, 0o600)

    def test_bundled_and_attached_options(self):
        self.record("-Pengineer", "-W30", "feat/y", "-", "--", "--model", "sonnet")
        self.assertEqual(resume.read_saved_args(self.args_dir, "feat-y"), ["--model", "sonnet"])

    def test_no_agent_args_removes_stale_sidecar(self):
        self.sidecar("feat/x", "permission-auto")
        self.record("feat/x", "plan.md")
        self.assertFalse((self.args_dir / "feat-x.args").exists())

    def test_resume_and_dry_run_leave_sidecar_alone(self):
        self.sidecar("feat/x", "permission-auto")
        self.record("-R", "feat/x")
        self.record("-n", "feat/x", "plan.md")
        self.assertEqual(resume.read_saved_args(self.args_dir, "feat-x"), ["--permission-mode", "auto"])

    def test_unparseable_argv_writes_nothing_and_succeeds(self):
        # herd itself reports the usage error; the wrapper must not block it.
        self.assertEqual(self.record("-v"), 0)
        self.assertEqual(self.record(), 0)
        self.assertFalse(self.args_dir.exists() and any(self.args_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()

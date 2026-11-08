#!/usr/bin/env python3
"""Which herd plans agentbox's entrypoint resumes after a pod restart.

Stdlib only: the image runs it as a plain script, without this repo's venv.

    resume.py scan [--root DIR] [--args-dir DIR] [--resume-only]
        One JSON object per line, one per <root>/*/.herd/run/state or
        <root>/*/*/.herd/run/state, sorted by worktree path. "action" is
        "resume", "skip" or "error", with a "reason" for the last two.
        entrypoint.sh acts on the "resume" lines and logs the rest.

    resume.py record-args [--args-dir DIR] -- <herd plan argv>
        Run by agentbox-plan before it execs `herd plan`. Saves the agent args
        given after `--` to <args-dir>/<agent>.args, one per line, so a later
        resume can start the agent the same way. herd's own state does not
        record them (docs/agentbox.md, Finding 3).

Defaults: --root $AGENTBOX_WT_ROOT or /home/agent/git/wt, --args-dir
$AGENTBOX_PLAN_ARGS_DIR or /home/agent/.local/state/agentbox/plans.

Resume rules (docs/agentbox.md §Decisions, herd-resume): planning, replanning,
pending, running, blocked, fixing and verified were in progress and resume.
complete is done and failed needs a human, so both are skipped, as is any
status this herd doesn't know. An unreadable state file, or a worktree whose
branch can't be read, is reported as an error and not resumed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

DEFAULT_ROOT = "/home/agent/git/wt"
DEFAULT_ARGS_DIR = "/home/agent/.local/state/agentbox/plans"

RESUME_STATUSES = ("planning", "replanning", "pending", "running", "blocked", "fixing", "verified")
SKIP_STATUSES = {"complete": "plan is complete", "failed": "plan failed and needs a human"}

# `herd plan`'s getopts string (bin/herd at the pinned commit): letters before
# a ':' take a value.
HERD_PLAN_OPTS = "r:b:k:m:p:P:v:W:nRh"

_KEY = re.compile(r"^[a-z_]+$")


class StateError(ValueError):
    """A .herd/run/state file that isn't herd's key=value format."""


def agent_name(branch: str) -> str:
    """herd's agent_name(): the herdr agent and worktree directory name for a branch."""
    n = _ascii_lower(branch).translate(str.maketrans("/._", "---"))
    n = "".join(c for c in n if c in "abcdefghijklmnopqrstuvwxyz0123456789_-")
    if n[:1] and n[0] in "-_0123456789":
        n = n[1:]
    return n[:32]


def _ascii_lower(s: str) -> str:
    # tr 'A-Z' 'a-z' touches ASCII only; str.lower() would also fold e.g. 'Ä'.
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def parse_state(text: str) -> dict:
    """herd's load_state(), strict: every non-blank line is key=value, and status is set."""
    state: dict = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or not _KEY.match(key):
            raise StateError(f"not a key=value line: {line[:60]!r}")
        state[key] = value
    if not state.get("status"):
        raise StateError("no status= line")
    state.setdefault("stage", "")
    state.setdefault("kind", "claude")
    return state


def _read_gitdir(wt: Path) -> Optional[Path]:
    """The gitdir a linked worktree's .git file points at, or None."""
    dotgit = wt / ".git"
    try:
        if not dotgit.is_file():
            return None
        first = dotgit.read_text().splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(first.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (wt / gitdir).resolve()
    return gitdir


def _direct_git_info(wt: Path, gitdir_reader: Callable[[Path], Optional[Path]]):
    """(branch, repo) read from the files git writes, or None if the layout isn't the usual one."""
    gitdir = gitdir_reader(wt)
    if gitdir is None:
        return None
    try:
        head = (gitdir / "HEAD").read_text().strip()
        commondir = (gitdir / "commondir").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    common = Path(commondir)
    if not common.is_absolute():
        common = (gitdir / common).resolve()
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    return branch, str(common.parent)


def _git_info(wt: Path):
    """(branch, repo) from git itself; branch is "" on a detached HEAD."""
    def git(*args):
        return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True,
                              timeout=30, check=True).stdout.strip()
    try:
        branch = git("branch", "--show-current")
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    except (OSError, subprocess.SubprocessError):
        return None
    return branch, str(Path(common).parent)


def worktree_git_info(wt: Path, gitdir_reader: Callable[[Path], Optional[Path]] = _read_gitdir):
    """(branch, source checkout) of a worktree: from its files if possible, else `git -C`.

    The source checkout is the parent of the git common dir; `herd plan -R` must
    run from there, because find_wt lists the repo's linked worktrees."""
    info = _direct_git_info(Path(wt), gitdir_reader) or _git_info(Path(wt))
    if info is None:
        return None, None
    return info


def read_saved_args(args_dir, agent: str) -> list:
    """The agent args agentbox-plan saved for this agent; [] when there is no sidecar."""
    try:
        text = (Path(args_dir) / f"{agent}.args").read_text()
    except (OSError, UnicodeDecodeError):
        return []
    return [line for line in text.splitlines() if line != ""]


def has_permission_auto(args) -> bool:
    for i, arg in enumerate(args):
        if arg == "--permission-mode=auto":
            return True
        if arg == "--permission-mode" and i + 1 < len(args) and args[i + 1] == "auto":
            return True
    return False


def decide(state_path, args_dir) -> dict:
    """What to do with one plan, as a dict entrypoint.sh reads from JSON."""
    state_path = Path(state_path)
    wt = state_path.parent.parent.parent
    plan: dict = {"action": "error", "reason": "", "worktree": str(wt)}
    try:
        state = parse_state(state_path.read_text())
    except FileNotFoundError:
        plan["reason"] = f"no state file at {state_path}"
        return plan
    except (OSError, UnicodeDecodeError, StateError) as e:
        plan["reason"] = f"unreadable state file: {e}"
        return plan

    status = state["status"]
    plan.update(status=status, stage=state["stage"], kind=state["kind"])
    branch, repo = worktree_git_info(wt)
    if branch:
        agent = agent_name(branch)
        saved = read_saved_args(args_dir, agent)
        plan.update(branch=branch, repo=repo, agent=agent, saved_args=saved,
                    permission_auto=has_permission_auto(saved),
                    # --continue reopens the worktree's last Claude conversation, so a
                    # stage in flight resumes in a session that remembers it. Other
                    # kinds have their own resume flags; start them fresh.
                    agent_args=(["--continue"] if state["kind"] == "claude" else []) + saved)

    if status in SKIP_STATUSES:
        plan.update(action="skip", reason=f"status {status}: {SKIP_STATUSES[status]}")
    elif status not in RESUME_STATUSES:
        plan.update(action="skip", reason=f"status {status}: not a herd status agentbox knows")
    elif not branch:
        plan["reason"] = ("worktree is not on a branch (detached HEAD?)" if repo
                          else "can't read the worktree's branch")
    else:
        plan.update(action="resume", reason="")
    return plan


def find_state_files(root) -> list:
    root = Path(root)
    found = set(root.glob("*/.herd/run/state")) | set(root.glob("*/*/.herd/run/state"))
    return sorted(found)


def scan(root, args_dir, resume_only: bool = False) -> list:
    plans = [decide(p, args_dir) for p in find_state_files(root)]
    return [p for p in plans if p["action"] == "resume"] if resume_only else plans


def parse_plan_argv(argv):
    """(branch, agent args) from a `herd plan` argv, mirroring herd's parsing.

    None for a resume (-R), a dry run (-n), help, or an argv herd will reject:
    none of those starts an agent, so the sidecar is left alone."""
    takes_value = {c for i, c in enumerate(HERD_PLAN_OPTS) if c != ":" and HERD_PLAN_OPTS[i + 1:i + 2] == ":"}
    flags = set(HERD_PLAN_OPTS.replace(":", ""))
    i = 0
    seen = set()
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        j = 1
        while j < len(arg):
            c = arg[j]
            if c not in flags:
                return None
            seen.add(c)
            if c in takes_value:
                if j + 1 < len(arg):
                    break
                i += 1
                if i >= len(argv):
                    return None
                break
            j += 1
        i += 1
    if seen & {"R", "n", "h"}:
        return None
    rest = argv[i:]
    if len(rest) < 2 or rest[1] == "--":
        return None
    branch = "".join(rest[0].split())
    rest = rest[2:]
    if rest and rest[0] == "--":
        return branch, rest[1:]
    if rest:
        return None
    return branch, []


def record_args(args_dir, argv) -> None:
    parsed = parse_plan_argv(argv)
    if parsed is None or not parsed[0]:
        return
    branch, args = parsed
    path = Path(args_dir) / f"{agent_name(branch)}.args"
    if not args:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".args.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{a}\n" for a in args))
    os.replace(tmp, path)


def main(argv=None, environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    p = argparse.ArgumentParser(prog="resume.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="report every herd plan under --root")
    s.add_argument("--root", default=env.get("AGENTBOX_WT_ROOT", DEFAULT_ROOT))
    s.add_argument("--args-dir", default=env.get("AGENTBOX_PLAN_ARGS_DIR", DEFAULT_ARGS_DIR))
    s.add_argument("--resume-only", action="store_true", help="print only the plans to resume")
    r = sub.add_parser("record-args", help="save the agent args of a `herd plan` argv")
    r.add_argument("--args-dir", default=env.get("AGENTBOX_PLAN_ARGS_DIR", DEFAULT_ARGS_DIR))
    r.add_argument("plan_argv", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)

    if a.cmd == "scan":
        for plan in scan(a.root, a.args_dir, a.resume_only):
            print(json.dumps(plan, sort_keys=True))
        return 0
    plan_argv = a.plan_argv[1:] if a.plan_argv[:1] == ["--"] else a.plan_argv
    try:
        record_args(a.args_dir, plan_argv)
    except OSError as e:
        # Never block the plan itself: herd still runs, it just won't resume with these args.
        print(f"agentbox-plan: could not save agent args: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

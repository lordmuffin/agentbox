#!/usr/bin/env python3
"""Prune the agent bindings herdr restores from its saved session.

Stdlib only: the image runs it as a plain script, without this repo's venv.

herdr saves every pane it has ever opened in ~/.config/herdr/session.json,
including the Claude session each pane was running. On start it restores all of
them at once, one `claude --resume <id>` per bound pane. Each of those is
~200-250 MB, so a box that has accumulated a pane per feature worktree asks for
far more memory than it has and the container is OOM-killed mid-restore --
every restart, because the state that causes it is on the PVC
(docs/agentbox.md, Finding 4).

This keeps the bindings worth restoring and strips the rest. A stripped pane
keeps its place, its directory and its scrollback; it just comes back as a
plain shell. No transcript is touched, so anything stripped can be resumed by
hand with the id the manifest records:

    claude --resume <session-id>      # run in the pane's cwd

    prune_sessions.py report [--session FILE] [--max-live N]
        What a prune would do, as a table and a one-line summary. Writes
        nothing. This is the default command.

    prune_sessions.py apply [--session FILE] [--max-live N] [--manifest FILE]
                           [--force]
        Back session.json up next to itself, strip the bindings report lists
        as strip, and write the manifest. Refuses to run while a herdr server
        is up, because herdr rewrites session.json from memory as it goes and
        would undo the prune: stop it first, or pass --force if you know it is
        not running (the entrypoint prunes before it starts herdr).

Which bindings are kept, in order:
  live    the pane's worktree has a herd plan still in progress -- the
          statuses resume.py resumes. Kept, newest plan first, up to
          --max-live of them.
  noplan  a checkout with no herd plan (an ad-hoc session). Stripped.
  done    the worktree's plan is complete, failed, or a status this herd does
          not know -- resume.py would skip it too. Stripped.
  gone    the pane's directory no longer exists, usually a worktree that was
          removed after its branch merged. Stripped.

Defaults: --session $AGENTBOX_HERDR_SESSION or
~/.config/herdr/session.json, --max-live $AGENTBOX_MAX_LIVE_AGENTS or 6, --manifest
$AGENTBOX_PRUNE_MANIFEST or
~/.local/state/agentbox/pruned-sessions.tsv.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

import resume

DEFAULT_SESSION = "~/.config/herdr/session.json"
DEFAULT_MANIFEST = "~/.local/state/agentbox/pruned-sessions.tsv"
DEFAULT_MAX_LIVE = 6

# The keys that bind a pane to an agent. herdr restores a pane as a plain
# shell when none of them is set, so they come out together.
BINDING_KEYS = ("agent_session", "agent_name", "managed_agent_kind")

# Roughly what one restored `claude --resume` costs, measured on agentbox-0
# (200-250 MB on arrival, climbing as it loads its transcript). Only used to
# print a projection.
AGENT_MB = 240


def max_live_default(env: Mapping[str, str]) -> int:
    """--max-live's default, falling back if the env var isn't a number."""
    raw = env.get("AGENTBOX_MAX_LIVE_AGENTS")
    try:
        return DEFAULT_MAX_LIVE if raw is None else int(raw)
    except ValueError:
        print(f"prune: AGENTBOX_MAX_LIVE_AGENTS={raw!r} is not a number; "
              f"using {DEFAULT_MAX_LIVE}", file=sys.stderr)
        return DEFAULT_MAX_LIVE


class SessionError(ValueError):
    """A session.json that isn't the shape this script knows how to prune."""


def load_session(path) -> dict:
    """herdr's saved session, checked far enough to be safe to rewrite."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise SessionError(f"cannot read {path}: {e}") from e
    try:
        session = json.loads(text)
    except json.JSONDecodeError as e:
        raise SessionError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(session, dict):
        raise SessionError(f"{path} is not a JSON object")
    if not isinstance(session.get("workspaces"), list):
        raise SessionError(f"{path} has no workspaces list")
    return session


def _values(container) -> list:
    """The dicts in a list, or in an id-keyed object. herdr writes tabs as a
    list but panes as an object keyed by pane id, which its layout and
    root_pane refer to -- which is why panes are only ever edited in place
    here, never removed."""
    if isinstance(container, dict):
        items = container.values()
    elif isinstance(container, list):
        items = container
    else:
        return []
    return [v for v in items if isinstance(v, dict)]


def bound_panes(session: dict) -> list:
    """Every pane with an agent binding, in the order herdr saved them."""
    found = []
    for ws in _values(session.get("workspaces")):
        for tab in _values(ws.get("tabs")):
            for pane in _values(tab.get("panes")):
                if pane.get("agent_session"):
                    found.append(pane)
    return found


def session_id(pane: dict) -> str:
    """The Claude session id a pane is bound to, or "" if it isn't an id."""
    binding = pane.get("agent_session")
    if not isinstance(binding, dict) or binding.get("kind") != "id":
        return ""
    return str(binding.get("value") or "")


def plan_status(cwd: Path):
    """(status, mtime) of the herd plan in cwd, or (None, 0) if there is none."""
    state = cwd / ".herd" / "run" / "state"
    try:
        text = state.read_text()
        mtime = state.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None, 0.0
    try:
        return resume.parse_state(text)["status"], mtime
    except resume.StateError:
        return None, mtime


def classify(pane: dict) -> dict:
    """What a pane's binding is: its class, and the plan mtime that orders it."""
    cwd = Path(str(pane.get("cwd") or ""))
    row = {"cwd": str(cwd), "id": session_id(pane), "mtime": 0.0, "pane": pane}
    if not cwd.is_dir():
        return {**row, "class": "gone", "why": "directory no longer exists"}
    status, mtime = plan_status(cwd)
    row["mtime"] = mtime
    if status is None:
        return {**row, "class": "noplan", "why": "no herd plan in this directory"}
    if status in resume.RESUME_STATUSES:
        return {**row, "class": "live", "why": f"plan is {status}"}
    known = resume.SKIP_STATUSES.get(status)
    return {**row, "class": "done", "why": known or f"status {status}: not a herd status agentbox knows"}


def decide(session: dict, max_live: int) -> list:
    """One row per bound pane: what it is, and whether to keep or strip it.

    Keeps the live bindings, newest plan first, up to max_live. Everything
    else is stripped. Rows come back in a stable order: the keeps, then the
    strips, each newest first.
    """
    rows = [classify(p) for p in bound_panes(session)]
    live = sorted((r for r in rows if r["class"] == "live"), key=lambda r: -r["mtime"])
    rest = sorted((r for r in rows if r["class"] != "live"), key=lambda r: -r["mtime"])
    for i, r in enumerate(live):
        if i < max_live:
            r["action"] = "keep"
        else:
            r["action"] = "strip"
            r["why"] = f"{r['why']}, over the {max_live} kept"
    for r in rest:
        r["action"] = "strip"
    return live + rest


def strip_binding(pane: dict) -> None:
    for key in BINDING_KEYS:
        pane.pop(key, None)


def herdr_running() -> bool:
    """Whether a herdr server is up, so a rewrite now would be overwritten."""
    try:
        return subprocess.run(["pgrep", "-x", "herdr"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False  # no pgrep: let --force / the caller decide


def backup_path(session_path: Path, now: Optional[float] = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return session_path.with_name(f"{session_path.name}.{stamp}.bak")


def write_manifest(path, rows: list, now: Optional[float] = None) -> None:
    """Append the stripped bindings, so each can be resumed by hand later."""
    stripped = [r for r in rows if r["action"] == "strip" and r["id"]]
    if not stripped:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    with path.open("a") as f:
        if new:
            f.write("# pruned\tclass\tsession_id\tcwd\n")
        for r in stripped:
            f.write(f"{stamp}\t{r['class']}\t{r['id']}\t{r['cwd']}\n")


def write_session(session_path: Path, session: dict) -> None:
    """Replace session.json atomically, so a crash cannot leave a half-written one."""
    tmp = session_path.with_name(session_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(session, f)
    os.replace(tmp, session_path)


def summary(rows: list) -> str:
    kept = [r for r in rows if r["action"] == "keep"]
    counts: dict = {}
    for r in rows:
        if r["action"] == "strip":
            counts[r["class"]] = counts.get(r["class"], 0) + 1
    detail = ", ".join(f"{n} {c}" for c, n in sorted(counts.items())) or "nothing"
    return (f"{len(rows)} bound panes: keeping {len(kept)}, stripping {detail}. "
            f"Restore now asks for about {len(kept) * AGENT_MB} MB, was about "
            f"{len(rows) * AGENT_MB} MB.")


def report(rows: list, out=sys.stdout) -> None:
    for r in rows:
        print(f"{r['action']:<5} {r['class']:<6} {r['id'] or '-':<36} {r['cwd']}  ({r['why']})", file=out)
    print(summary(rows), file=out)


def main(argv=None, environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    p = argparse.ArgumentParser(prog="prune_sessions.py", description=__doc__.splitlines()[0])

    def common(sub):
        sub.add_argument("--session", default=env.get("AGENTBOX_HERDR_SESSION", DEFAULT_SESSION))
        sub.add_argument("--max-live", type=int, default=max_live_default(env))

    sub = p.add_subparsers(dest="cmd")
    common(sub.add_parser("report", help="what a prune would do; writes nothing"))
    a = sub.add_parser("apply", help="back up session.json and strip the bindings")
    common(a)
    a.add_argument("--manifest", default=env.get("AGENTBOX_PRUNE_MANIFEST", DEFAULT_MANIFEST))
    a.add_argument("--force", action="store_true", help="prune even if a herdr server looks up")
    # report is the default, so a bare run -- or one with only flags -- is
    # always the safe one that writes nothing.
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("report", "apply", "-h", "--help"):
        raw = ["report", *raw]
    args = p.parse_args(raw)

    session_path = Path(args.session).expanduser()
    if args.max_live < 0:
        print("prune: --max-live cannot be negative", file=sys.stderr)
        return 2
    try:
        session = load_session(session_path)
    except SessionError as e:
        print(f"prune: {e}", file=sys.stderr)
        return 1

    rows = decide(session, args.max_live)
    if args.cmd == "report":
        report(rows)
        return 0

    if herdr_running() and not args.force:
        print("prune: a herdr server is running; it would rewrite session.json and undo this. "
              "Stop it (herdr server stop) or pass --force.", file=sys.stderr)
        return 1
    if not any(r["action"] == "strip" for r in rows):
        print(f"prune: nothing to strip. {summary(rows)}")
        return 0

    backup = backup_path(session_path)
    shutil.copy2(session_path, backup)
    for r in rows:
        if r["action"] == "strip":
            strip_binding(r["pane"])
    write_session(session_path, session)
    write_manifest(Path(args.manifest).expanduser(), rows)
    report(rows)
    print(f"prune: backed up to {backup}; stripped ids recorded in "
          f"{Path(args.manifest).expanduser()} (claude --resume <id> to pick one up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

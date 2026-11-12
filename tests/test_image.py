"""Static rules for the image, its entrypoint and its build workflow.

Ported from homelab-gitops tests/tdd/test_agentbox.py when the image moved here.

- Every FROM is pinned by digest, so a rebuild of the same VERSION gets the same base.
- tether and herd are private Forgejo repos. The read token for them reaches the
  build only through a BuildKit secret mount (`RUN --mount=type=secret`). An ARG,
  ENV or COPY would put it in the image's history or a layer, and GHCR images are
  pulled by every node.
- It runs as `agent`, uid/gid 1000, because the PVC mounted at /home/agent is
  owned by that uid (fsGroup 1000).
- The workflow publishes ghcr.io/lordmuffin/agentbox:v<VERSION> and refuses to
  overwrite a tag, so VERSION and the StatefulSet's tag move together.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
VERSION = ROOT / "VERSION"
ENTRYPOINT = ROOT / "entrypoint.sh"
WORKFLOW = ROOT / ".forgejo/workflows/build.yml"
TESTS = ROOT / ".forgejo/workflows/test.yml"
GH_WORKFLOW = ROOT / ".github/workflows/build.yml"
IMAGE = "ghcr.io/lordmuffin/agentbox"
SECRET_ID = "forgejo_read_token"
TOKENISH = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY\b", re.I)
ADDR = "tcp://buildkitd.buildkit.svc.cluster.local:1234"


def instructions():
    """The Dockerfile as (INSTRUCTION, args) pairs, continuations joined, comments dropped."""
    out, pending = [], ""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        full = pending + stripped
        pending = ""
        word, _, rest = full.partition(" ")
        out.append((word.upper(), rest.strip()))
    return out


def triggers(wf):
    # PyYAML reads the bare key `on:` as True.
    return wf.get(True, wf.get("on"))


class Version(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(VERSION.read_text().strip(), r"^\d+\.\d+\.\d+$")


class Dockerfile(unittest.TestCase):
    def setUp(self):
        self.ins = instructions()

    def test_every_from_is_pinned_by_digest(self):
        stages = set()
        for word, args in self.ins:
            if word != "FROM":
                continue
            ref = args.split()[0]
            if ref not in stages:  # else FROM <earlier stage>
                self.assertRegex(ref, r"@sha256:[0-9a-f]{64}$", args)
            if " AS " in args.upper():
                stages.add(args.split()[-1])

    def test_no_arg_or_env_carries_a_token(self):
        for word, args in self.ins:
            if word in ("ARG", "ENV"):
                names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", args) or [args.split("=")[0]]
                for name in names:
                    self.assertNotRegex(name, TOKENISH, f"{word} {args}")

    def test_the_read_token_is_used_only_through_a_secret_mount(self):
        uses = [(w, a) for w, a in self.ins if SECRET_ID in a or "/run/secrets/" in a]
        self.assertTrue(uses, "the Dockerfile never reads the Forgejo read token")
        for word, args in uses:
            self.assertEqual(word, "RUN", args)
            self.assertIn(f"--mount=type=secret,id={SECRET_ID}", args)
        # Not copied in and not put in a URL, where git would keep it in .git/config.
        for word, args in self.ins:
            self.assertNotIn(SECRET_ID, args if word in ("COPY", "ADD") else "", args)
        self.assertNotRegex(DOCKERFILE.read_text(), r"https://[^/\s]*:[^@\s]*@")

    def test_runs_as_uid_1000(self):
        users = [a for w, a in self.ins if w == "USER"]
        self.assertTrue(users)
        self.assertIn(users[-1], ("1000", "1000:1000", "agent", "agent:agent"))
        text = DOCKERFILE.read_text()
        self.assertRegex(text, r"(?:groupadd|addgroup)[^\n]*(?:-g|--gid) 1000\b")
        self.assertRegex(text, r"(?:useradd|adduser)[^\n]*(?:-u|--uid) 1000\b")
        self.assertIn("/home/agent", text)

    def test_the_entrypoint_is_the_script(self):
        entry = [a for w, a in self.ins if w == "ENTRYPOINT"]
        self.assertEqual(len(entry), 1)
        self.assertIn("entrypoint.sh", entry[0])
        copied = " ".join(a for w, a in self.ins if w == "COPY")
        for f in ("entrypoint.sh", "resume.py", "resume-plan.sh", "agentbox-plan.sh"):
            self.assertIn(f, copied)

    def test_tests_and_ci_stay_out_of_the_image(self):
        ignore = (ROOT / ".dockerignore").read_text().split()
        for entry in ("tests/", ".forgejo/", ".github/", ".git"):
            self.assertIn(entry, ignore)


class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.text = ENTRYPOINT.read_text()

    def test_tetherd_needs_no_home_assistant_token(self):
        # Web Push only since tether #48: an HA token gate would keep tetherd down.
        start = self.text.split("start_tetherd() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("HA_TOKEN", start)
        self.assertNotRegex(self.text, r"export TETHER_HA_")

    def test_app_sign_in_is_all_three_settings_or_none(self):
        # tetherd refuses to start on a partial --app-* set (homelab-gitops #895).
        start = self.text.split("start_tetherd() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('private_copy "$AGENTBOX_APP_SESSION_KEY_FILE" app-session.key', start)
        self.assertIn("unset TETHER_APP_OIDC_ISSUER TETHER_APP_OIDC_CLIENT_ID TETHER_APP_SESSION_KEY_FILE", start)

    def test_vapid_key_is_a_private_copy_or_on_the_home_volume(self):
        start = self.text.split("start_tetherd() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('private_copy "$AGENTBOX_VAPID_KEY_FILE" vapid.key', start)
        self.assertIn("TETHER_WEBPUSH_KEY=$HOME/.tether/vapid.key", start)

    def test_proxy_listens_on_the_pod_ip(self):
        # tetherd refuses 0.0.0.0 and names, so --proxy-addr comes from POD_IP
        # (homelab-gitops test_tether_proxy.py checks the StatefulSet sets it).
        self.assertIn("export TETHER_PROXY_ADDR=$POD_IP:7443", self.text)

    def test_proxy_stays_off_without_a_pem_ca(self):
        # A non-empty file is not enough: the listener needs a PEM CA.
        self.assertIn("BEGIN CERTIFICATE", self.text)


    def test_tetherd_gets_a_private_copy_of_the_suggest_key(self):
        # tetherd refuses a group-readable key file and exits, and fsGroup makes the
        # mount 0440. So the entrypoint copies it to the runtime dir at 0600, and turns
        # suggestions off (rather than sending no key) when the item doesn't exist yet.
        self.assertIn('private_copy "$TETHER_SUGGEST_KEY_FILE" litellm.key', self.text)
        self.assertIn("unset TETHER_SUGGEST_URL TETHER_SUGGEST_KEY_FILE", self.text)


class Federation(unittest.TestCase):
    """Federation secrets (tether docs/federation.md): private copies, never a stopped hub."""

    def setUp(self):
        self.text = ENTRYPOINT.read_text()

    def test_start_tetherd_handles_federation_after_the_hmac_copy(self):
        start = self.text.split("start_tetherd() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(start.index("export TETHER_HMAC_SECRET_FILE=$hmac"), start.index("federation_secrets"))
        self.assertLess(start.index("federation_secrets"), start.index("tetherd serve"))

    def test_every_secret_is_a_private_copy(self):
        body = self.text.split("federation_secrets() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('private_copy "$file" "satellite-$name.secret"', body)
        self.assertIn('private_copy "$TETHER_HUB_SECRET_FILE" hub.secret', body)

    def test_sourcing_does_not_run_main(self):
        self.assertTrue(self.text.rstrip().endswith('if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi'))


class FederationShell(unittest.TestCase):
    """Sources entrypoint.sh and runs federation_secrets in a clean environment."""

    def setUp(self):
        # Without the guard, sourcing runs main(): it would start herdr, tetherd and sshd.
        if 'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi' not in ENTRYPOINT.read_text():
            self.fail("entrypoint.sh runs main when sourced; not sourcing it")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir(mode=0o700)
        self.hmac = self.secret("hmac", "h" * 64)

    def secret(self, name, value, mode=0o440):
        path = self.tmp / "mnt" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(value + "\n")
        path.chmod(mode)
        return path

    def federate(self, **env):
        """Run federation_secrets; return (log lines, resulting TETHER_* env)."""
        script = (
            'source "$1" && TETHER_HMAC_SECRET_FILE=$(private_copy "$2" tether-hmac.key) '
            '&& federation_secrets && env | grep "^TETHER_" | grep -v "^TETHER_\\(HMAC\\|PROXY\\|APPROVAL\\|LOKI\\)" || true'
        )
        base = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "AGENTBOX_RUNTIME_DIR": str(self.run_dir)}
        p = subprocess.run(["bash", "-c", script, "bash", str(ENTRYPOINT), str(self.hmac)],
                           env={**base, **env}, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        logs = [l for l in p.stdout.splitlines() if l.startswith("agentbox: ")]
        out = dict(l.split("=", 1) for l in p.stdout.splitlines() if l.startswith("TETHER_"))
        return logs, out

    def test_satellites_are_rewritten_to_0600_copies(self):
        a, b = self.secret("pc", "a" * 64), self.secret("mac", "b" * 64)
        logs, out = self.federate(TETHER_SATELLITES=f" andrew-gamingpc={a}, Mac={b} ")
        pc, mac = self.run_dir / "satellite-andrew-gamingpc.secret", self.run_dir / "satellite-mac.secret"
        self.assertEqual(out["TETHER_SATELLITES"], f"andrew-gamingpc={pc},mac={mac}")
        for copy, src in ((pc, a), (mac, b)):
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
            self.assertEqual(copy.read_text(), src.read_text())
        self.assertEqual(logs, ["agentbox: tetherd federation hub: satellites andrew-gamingpc,mac"])

    def test_a_missing_or_unusable_satellite_is_dropped_and_logged(self):
        good = self.secret("pc", "a" * 64)
        short, hmac_twin, twin = self.secret("short", "c" * 31), self.secret("hm", "h" * 64), self.secret("twin", "a" * 64)
        empty = self.secret("empty", "")
        sats = ",".join([f"andrew-gamingpc={good}", f"gone={self.tmp}/mnt/nope", f"empty={empty}",
                         f"short={short}", f"hm={hmac_twin}", f"twin={twin}", f"andrew-gamingpc={good}",
                         f"Bad_Name={good}", "garbage"])
        logs, out = self.federate(TETHER_SATELLITES=sats)
        self.assertEqual(out["TETHER_SATELLITES"], f"andrew-gamingpc={self.run_dir}/satellite-andrew-gamingpc.secret")
        text = "\n".join(logs)
        for name, why in (("gone", "no secret of at least 32 bytes"), ("empty", "no secret of at least 32 bytes"),
                          ("short", "no secret of at least 32 bytes"), ("hm", "its secret is the HMAC key"),
                          ("twin", "its secret is another satellite's"), ("andrew-gamingpc", "named twice"),
                          ('"bad_name"', "not a DNS label"), ('entry "garbage"', "not name=secret-file")):
            self.assertRegex(text, rf"satellite {re.escape(name)} dropped: {re.escape(why)}")
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()),
                         ["satellite-andrew-gamingpc.secret", "tether-hmac.key"])
        for secret in ("a" * 32, "h" * 32, "c" * 31):
            self.assertNotIn(secret, text)

    def test_no_usable_satellite_turns_the_hub_off(self):
        logs, out = self.federate(TETHER_SATELLITES=f"andrew-gamingpc={self.tmp}/mnt/nope")
        self.assertNotIn("TETHER_SATELLITES", out)
        self.assertEqual(logs[-1], "agentbox: tetherd federation hub off: no usable satellite")

    def test_hub_wins_over_satellite_settings(self):
        a = self.secret("pc", "a" * 64)
        logs, out = self.federate(TETHER_SATELLITES=f"andrew-gamingpc={a}", TETHER_HUB_URL="https://hub.example",
                                  TETHER_HUB_SECRET_FILE=str(a), TETHER_SATELLITE_NAME="x")
        self.assertEqual(set(out), {"TETHER_SATELLITES"})
        self.assertIn("ignoring TETHER_HUB_URL", logs[0])

    def test_satellite_secret_is_a_0600_copy(self):
        s = self.secret("hub", "d" * 64)
        logs, out = self.federate(TETHER_HUB_URL="https://hub.example", TETHER_HUB_SECRET_FILE=str(s),
                                  TETHER_SATELLITE_NAME="agentbox")
        copy = self.run_dir / "hub.secret"
        self.assertEqual(out, {"TETHER_HUB_URL": "https://hub.example", "TETHER_HUB_SECRET_FILE": str(copy),
                               "TETHER_SATELLITE_NAME": "agentbox"})
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(logs, ["agentbox: tetherd federation satellite of https://hub.example as agentbox"])

    def test_satellite_without_a_usable_secret_is_off(self):
        for env in ({"TETHER_HUB_URL": "https://hub.example", "TETHER_HUB_SECRET_FILE": f"{self.tmp}/mnt/nope"},
                    {"TETHER_HUB_URL": "https://hub.example", "TETHER_HUB_SECRET_FILE": str(self.hmac)},
                    {"TETHER_HUB_SECRET_FILE": str(self.secret("hub", "d" * 64)), "TETHER_SATELLITE_NAME": "x",
                     "TETHER_HUB_HANDOFF_TIMEOUT": "5s"}):
            with self.subTest(env=env):
                logs, out = self.federate(**env)
                self.assertEqual(out, {})
                self.assertIn("tetherd federation satellite off", logs[-1])

    def test_no_federation_settings_log_nothing(self):
        self.assertEqual(self.federate(), ([], {}))


# --- sshd for the PC's herdr (homelab-gitops docs/agentbox.md §herdr machine) -----------
# tests/test_sshd_config.py pins sshd_config itself; these pin how the image and the
# entrypoint use it. homelab-gitops tests the workload, RBAC and port-forward side.
class SshdImage(unittest.TestCase):
    def setUp(self):
        self.steps = instructions()
        self.runs = " ".join(a for i, a in self.steps if i == "RUN")
        self.entry = ENTRYPOINT.read_text()

    def test_openssh_server_installed_and_its_build_host_keys_deleted(self):
        self.assertRegex(self.runs, r"apt-get install[^&]*\bopenssh-server\b")
        self.assertIn("rm -f /etc/ssh/ssh_host_*", self.runs)

    def test_config_is_root_owned_in_the_image(self):
        at = [n for n, (i, a) in enumerate(self.steps) if i == "COPY" and "sshd_config" in a]
        self.assertEqual([self.steps[n][1] for n in at], ["--chmod=0644 sshd_config /etc/agentbox/sshd_config"])
        # Copied before USER 1000, so root owns it and `agent` can't edit it.
        user_at = max(n for n, (i, _) in enumerate(self.steps) if i == "USER")
        self.assertLess(at[0], user_at)
        # COPY --chmod=0644 makes /etc/agentbox 0644 too, which nobody can enter.
        self.assertIn("chmod 0755 /etc/agentbox", self.runs)

    def test_entrypoint_runs_sshd_with_that_config_in_the_foreground(self):
        self.assertIn("AGENTBOX_SSHD_CONFIG=${AGENTBOX_SSHD_CONFIG:-/etc/agentbox/sshd_config}", self.entry)
        # sshd re-execs itself, so it must be started by absolute path.
        self.assertIn("AGENTBOX_SSHD_BIN=${AGENTBOX_SSHD_BIN:-/usr/sbin/sshd}", self.entry)
        self.assertIn('"$AGENTBOX_SSHD_BIN" -D -e -f "$AGENTBOX_SSHD_CONFIG" -h "$key"', self.entry)
        self.assertIn('"$AGENTBOX_SSHD_BIN" -T -f "$AGENTBOX_SSHD_CONFIG" -h "$key"', self.entry)

    def test_host_key_is_ed25519_on_the_home_volume_and_private(self):
        self.assertIn("AGENTBOX_SSHD_DIR=${AGENTBOX_SSHD_DIR:-$HOME/.config/agentbox/sshd}", self.entry)
        self.assertIn("ssh-keygen -q -t ed25519 -N ''", self.entry)
        self.assertIn('chmod 600 "$key"', self.entry)
        self.assertIn('umask 077 && mkdir -p "$AGENTBOX_SSHD_DIR"', self.entry)

    def test_every_sshd_log_line_is_prefixed(self):
        body = self.entry.split("# --- 2b. sshd", 1)[1].split("# --- 4. resume", 1)[0]
        msgs = re.findall(r'\blog "([^"]*)"', body)
        self.assertTrue(msgs)
        for m in msgs:
            self.assertTrue(m.startswith("sshd"), m)
        # sshd's own -e log goes through the same prefix.
        self.assertIn("printf 'agentbox: sshd %s\\n'", body)
        self.assertIn("2> >(sshd_log)", body)

    def test_herdr_server_is_a_detached_daemon(self):
        # Saved SSH machines refuse a server that isn't a session leader
        # (detached_server_daemon: false); `machine add` would offer to replace it.
        self.assertIn('setsid herdr server >>"$HOME/.config/herdr/agentbox-server.log" 2>&1 </dev/null &', self.entry)
        self.assertNotIn("setsid -f", self.entry)  # -f forks: $! would no longer be herdr

    def test_supervised_and_stopped_on_shutdown(self):
        self.assertIn("  start_sshd\n", self.entry.split("main() {", 1)[1])
        stop = self.entry.split("shutdown() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('kill -TERM "$sshd_sup_pid"', stop)
        self.assertIn("for p in $sshd_sup_pid ", stop)


class BuildWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(WORKFLOW.read_text())
        self.text = WORKFLOW.read_text()
        self.on = triggers(self.wf)

    def test_dispatch_only(self):
        # GitHub builds on a push to main (GitHubBuildWorkflow); this one is the manual
        # fallback, so the two never race to push one tag.
        self.assertIn("workflow_dispatch", self.on)
        self.assertNotIn("push", self.on)
        self.assertNotIn("pull_request", self.on)

    def test_publishes_the_image_at_v_version(self):
        self.assertIn(f"IMAGE: {IMAGE}", self.text)
        self.assertIn("tr -d '[:space:]' < VERSION", self.text)
        self.assertIn('tag=v$(', self.text)

    def test_builds_the_repo_root_for_amd64_only(self):
        self.assertIn("--local context=. --local dockerfile=.", self.text)
        self.assertIn("--opt platform=linux/amd64", self.text)

    def test_refuses_to_overwrite_a_tag(self):
        self.assertIn("lordmuffin/agentbox/manifests/", self.text)
        self.assertIn("already exists", self.text)

    def test_hands_the_read_token_to_buildctl_as_a_secret(self):
        self.assertIn("${{ secrets.SOURCE_READ_TOKEN }}", self.text)
        self.assertIn(f"--secret id={SECRET_ID},", self.text)
        # Never as a build arg, where it would land in the image's history.
        self.assertNotRegex(self.text, r"build-arg:[A-Z_]*TOKEN")

    def test_secret_names_forgejo_accepts(self):
        # Forgejo refuses Actions secret names starting FORGEJO_, GITEA_ or GITHUB_.
        for name in re.findall(r"secrets\.([A-Za-z0-9_]+)", self.text):
            self.assertNotRegex(name, r"^(FORGEJO|GITEA|GITHUB)_", name)

    def test_one_job_on_a_k3s2_runner_pod(self):
        # buildkitd's NetworkPolicy admits only the forgejo-runner namespace, and
        # runner-01 also offers self-hosted.
        (job,) = self.wf["jobs"].values()
        self.assertIn(job["runs-on"], ("docker", "ubuntu-latest"))

    def test_unit_tests_run_before_the_build(self):
        (job,) = self.wf["jobs"].values()
        names = [s.get("name", "") for s in job["steps"]]
        self.assertLess(names.index("Unit and static tests"), names.index("Build and push"))

    def test_talks_mtls_to_the_cluster_builder(self):
        for needle in (ADDR, "--tlscacert", "--tlscert", "--tlskey",
                       "--tlsservername buildkitd.buildkit.svc.cluster.local"):
            self.assertIn(needle, self.text)

    def test_buildctl_is_a_release_checked_by_sha256(self):
        m = re.search(r"buildkit-(v[\d.]+)\.linux-amd64\.tar\.gz", self.text)
        self.assertTrue(m)
        self.assertRegex(self.text, rf"[0-9a-f]{{64}}  buildkit-{re.escape(m[1])}\.linux-amd64")


class GitHubBuildWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = GH_WORKFLOW.read_text()
        self.wf = yaml.safe_load(self.text)
        self.on = triggers(self.wf)
        (self.job,) = self.wf["jobs"].values()

    def test_builds_on_push_to_main_and_dispatch(self):
        self.assertIn("workflow_dispatch", self.on)
        self.assertEqual(self.on["push"]["branches"], ["main"])
        for p in ("tests/**", "**.md", ".forgejo/**"):
            self.assertIn(p, self.on["push"]["paths-ignore"])
        self.assertNotIn("pull_request", self.on)

    def test_runs_only_on_github(self):
        self.assertEqual(self.job["if"], "github.server_url == 'https://github.com'")

    def test_publishes_the_image_at_v_version_for_amd64_only(self):
        self.assertIn(f"IMAGE: {IMAGE}", self.text)
        self.assertIn("tr -d '[:space:]' < VERSION", self.text)
        self.assertIn("platforms: linux/amd64", self.text)

    def test_refuses_to_overwrite_a_tag(self):
        self.assertIn("lordmuffin/agentbox/manifests/", self.text)
        self.assertIn("already exists", self.text)

    def test_clones_sources_from_github(self):
        self.assertIn("SOURCE_BASE=https://github.com/lordmuffin", self.text)

    def test_read_token_is_a_buildkit_secret_never_a_build_arg(self):
        self.assertIn(f"{SECRET_ID}=${{{{ secrets.SOURCE_READ_TOKEN }}}}", self.text)
        args = self.text.split("build-args:", 1)[1].split("secrets:", 1)[0]
        self.assertNotRegex(args, TOKENISH)

    def test_unit_tests_run_before_the_build(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        self.assertLess(names.index("Unit and static tests"), names.index("Build and push"))
        self.assertLess(names.index("Refuse to overwrite an existing tag"), names.index("Build and push"))


class SourceClone(unittest.TestCase):
    def test_source_host_is_an_arg_defaulting_to_forgejo(self):
        text = DOCKERFILE.read_text()
        self.assertIn("ARG SOURCE_BASE=https://git.apj.dev/lordmuffin", text)
        self.assertIn('clone --quiet "$SOURCE_BASE/tether.git" tether', text)
        self.assertIn('clone --quiet "$SOURCE_BASE/herd.git" herd', text)
        # GitHub's git-over-HTTPS takes the token as basic auth, Forgejo as `token`.
        self.assertIn("x-access-token:%s", text)


class TestWorkflow(unittest.TestCase):
    def test_runs_on_every_push_and_pr_without_secrets(self):
        text = TESTS.read_text()
        on = triggers(yaml.safe_load(text))
        self.assertIn("push", on)
        self.assertIn("pull_request", on)
        self.assertNotIn("${{ secrets.", text)


if __name__ == "__main__":
    unittest.main()

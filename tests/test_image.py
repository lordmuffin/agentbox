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
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
VERSION = ROOT / "VERSION"
ENTRYPOINT = ROOT / "entrypoint.sh"
WORKFLOW = ROOT / ".forgejo/workflows/build.yml"
TESTS = ROOT / ".forgejo/workflows/test.yml"
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
        for entry in ("tests/", ".forgejo/", ".git"):
            self.assertIn(entry, ignore)


class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.text = ENTRYPOINT.read_text()

    def test_proxy_listens_on_the_pod_ip(self):
        # tetherd refuses 0.0.0.0 and names, so --proxy-addr comes from POD_IP
        # (homelab-gitops test_tether_proxy.py checks the StatefulSet sets it).
        self.assertIn("export TETHER_PROXY_ADDR=$POD_IP:7443", self.text)

    def test_proxy_stays_off_without_a_pem_ca(self):
        # A non-empty file is not enough: the listener needs a PEM CA.
        self.assertIn("BEGIN CERTIFICATE", self.text)


class BuildWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(WORKFLOW.read_text())
        self.text = WORKFLOW.read_text()
        self.on = triggers(self.wf)

    def test_builds_on_push_to_main_and_dispatch(self):
        self.assertIn("workflow_dispatch", self.on)
        self.assertEqual(self.on["push"]["branches"], ["main"])
        for p in ("tests/**", "**.md", ".forgejo/**"):
            self.assertIn(p, self.on["push"]["paths-ignore"])
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


class TestWorkflow(unittest.TestCase):
    def test_runs_on_every_push_and_pr_without_secrets(self):
        text = TESTS.read_text()
        on = triggers(yaml.safe_load(text))
        self.assertIn("push", on)
        self.assertIn("pull_request", on)
        self.assertNotIn("${{ secrets.", text)


if __name__ == "__main__":
    unittest.main()

"""Browser testing support: the image carries Chromium + playwright-core, the helper
library builds container-safe launch options, and the entrypoint registers the MCP server."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
ENTRYPOINT = ROOT / "entrypoint.sh"
BROWSER = ROOT / "browser"


class Image(unittest.TestCase):
    def test_final_stage_installs_chromium_and_fonts(self):
        final = DOCKERFILE.split("\nFROM ")[-1]
        apt = re.search(r"apt-get install[^&]*", final).group(0)
        for pkg in ("chromium", "fonts-liberation", "fonts-noto-color-emoji"):
            self.assertRegex(apt, rf"\b{pkg}\b")

    def test_playwright_is_pinned_and_downloads_no_browser(self):
        for arg in ("PLAYWRIGHT_CORE_VERSION", "PLAYWRIGHT_MCP_VERSION"):
            self.assertRegex(DOCKERFILE, rf"ARG {arg}=\d+\.\d+\.\d+\n")
        # playwright-core never downloads browsers; the full `playwright` package would.
        self.assertIn('"playwright-core@${PLAYWRIGHT_CORE_VERSION}"', DOCKERFILE)
        self.assertNotRegex(DOCKERFILE, r'"playwright@')

    def test_helper_is_shipped_and_resolvable(self):
        self.assertIn("COPY --chmod=0755 browser /opt/agentbox/browser", DOCKERFILE)
        self.assertIn("NODE_PATH=/usr/local/lib/node_modules", DOCKERFILE)
        self.assertIn("ln -s /opt/agentbox/browser /usr/local/lib/node_modules/agentbox-browser", DOCKERFILE)
        self.assertIn("ln -s /opt/agentbox/browser/cli.cjs /usr/local/bin/agentbox-browser", DOCKERFILE)
        self.assertIn("&& chromium --version", DOCKERFILE)

    def test_package_main_exists(self):
        pkg = json.loads((BROWSER / "package.json").read_text())
        self.assertTrue((BROWSER / pkg["main"]).exists())
        self.assertTrue((BROWSER / pkg["bin"]["agentbox-browser"]).exists())


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Lib(unittest.TestCase):
    def node(self, code, **env):
        p = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ["PATH"], "HOME": "/tmp", **env}, cwd=BROWSER)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_launch_options_are_container_safe(self):
        o = self.node("console.log(JSON.stringify(require('./lib.cjs').launchOptions({})))")
        self.assertEqual(o["executablePath"], "/usr/bin/chromium")
        self.assertTrue(o["headless"])
        self.assertIn("--no-sandbox", o["args"])
        self.assertIn("--disable-dev-shm-usage", o["args"])

    def test_launch_options_follow_the_environment(self):
        o = self.node("console.log(JSON.stringify(require('./lib.cjs').launchOptions("
                      "{AGENTBOX_CHROMIUM:'/opt/c', AGENTBOX_BROWSER_HEADED:'1'}, {args:['--x']})))")
        self.assertEqual(o["executablePath"], "/opt/c")
        self.assertFalse(o["headless"])
        self.assertEqual(o["args"][-1], "--x")
        self.assertIn("--no-sandbox", o["args"])

    def test_https_errors_are_accepted_only_when_asked(self):
        c = "console.log(JSON.stringify([require('./lib.cjs').contextOptions(%s).ignoreHTTPSErrors]))"
        self.assertEqual(self.node(c % "{}"), [False])
        self.assertEqual(self.node(c % "{AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS:'true'}"), [True])

    def test_watch_collects_what_a_user_would_call_broken(self):
        out = self.node("""
          const { watch } = require('./lib.cjs');
          const h = {}; const page = { on: (e, f) => { h[e] = f; } };
          const log = watch(page);
          h.console({ type: () => 'error', text: () => 'boom' });
          h.console({ type: () => 'log', text: () => 'fine' });
          h.pageerror(new Error('uncaught'));
          h.requestfailed({ method: () => 'GET', url: () => 'http://x/a', failure: () => ({ errorText: 'net::ERR' }) });
          h.response({ status: () => 200, url: () => 'http://x/ok' });
          h.response({ status: () => 404, url: () => 'http://x/missing' });
          console.log(JSON.stringify(log.problems()));
        """)
        self.assertEqual(out, ["console.error: boom", "pageerror: uncaught",
                               "request failed: GET http://x/a net::ERR", "http: 404 http://x/missing"])

    def test_cli_usage_and_errors(self):
        cli = str(BROWSER / "cli.cjs")
        run = lambda *a: subprocess.run(["node", cli, *a], capture_output=True, text=True, timeout=30)
        self.assertEqual(run("--help").returncode, 0)
        self.assertIn("smoke <url>", run("--help").stdout)
        self.assertEqual(run("bogus").returncode, 2)
        self.assertEqual(run("smoke").returncode, 2)
        self.assertEqual(run("smoke", "http://x", "--nope").returncode, 2)


class BrowserMcpShell(unittest.TestCase):
    """Sources entrypoint.sh and runs setup_browser_mcp against fake claude / playwright-mcp."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "claude.log"
        self.fake("claude", 'printf \'%s\\n\' "$*" >>"$CLAUDE_LOG"\n')
        self.fake("playwright-mcp", "exit 0\n")

    def fake(self, name, body):
        f = self.bin / name
        f.write_text("#!/usr/bin/env bash\n" + body)
        f.chmod(0o755)

    def run_setup(self, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp), "CLAUDE_LOG": str(self.log), **env}
        p = subprocess.run(["bash", "-c", 'source "$1" && setup_browser_mcp', "bash", str(ENTRYPOINT)],
                           env=e, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        return p.stdout.splitlines()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_registers_a_headless_no_sandbox_server_replacing_any_old_one(self):
        out = self.run_setup()
        self.assertEqual(self.calls(), [
            "mcp remove --scope user playwright",
            "mcp add --scope user playwright -- playwright-mcp --headless --no-sandbox "
            "--executable-path /usr/bin/chromium"])
        self.assertEqual(out, ["agentbox: browser: playwright MCP registered for claude"])

    def test_private_ca_and_custom_binary_are_passed_through(self):
        self.run_setup(AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS="1", AGENTBOX_CHROMIUM="/opt/c")
        add = self.calls()[-1]
        self.assertIn("--ignore-https-errors", add)
        self.assertIn("--executable-path /opt/c", add)

    def test_opt_out_touches_nothing(self):
        self.assertIn("off", self.run_setup(AGENTBOX_BROWSER_MCP="0")[0])
        self.assertEqual(self.calls(), [])

    def test_missing_tools_are_a_logged_no_op(self):
        (self.bin / "playwright-mcp").unlink()
        self.assertIn("not installed", self.run_setup()[0])
        self.assertEqual(self.calls(), [])

    def test_a_failing_claude_warns_and_does_not_stop_the_start(self):
        self.fake("claude", "exit 1\n")
        self.assertIn("WARNING", self.run_setup()[0])

    def test_runs_at_start_and_never_fatally(self):
        self.assertIn("  setup_browser_mcp || true\n", ENTRYPOINT.read_text())


class TetherBrowserShell(unittest.TestCase):
    """Sources entrypoint.sh and runs start_tetherd against a fake tetherd that records its env:
    the Browser pane (tether docs/host-browser.md) gets the image's Chromium, unsandboxed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir(mode=0o700)
        self.hmac = self.tmp / "hmac"
        self.hmac.write_text("h" * 64 + "\n")
        self.env_out = self.tmp / "tetherd.env"
        tetherd = self.bin / "tetherd"
        tetherd.write_text('#!/usr/bin/env bash\nenv | grep "^TETHER_BROWSER\\|^TETHER_FORGE" | sort >"$ENV_OUT"\n')
        tetherd.chmod(0o755)

    def start(self, **env):
        """Run start_tetherd; return (tetherd's TETHER_BROWSER*/FORGE* env, the sourcing shell's after)."""
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp), "ENV_OUT": str(self.env_out),
             "AGENTBOX_RUNTIME_DIR": str(self.run_dir), "AGENTBOX_HMAC_KEY_FILE": str(self.hmac),
             "AGENTBOX_VAPID_KEY_FILE": str(self.tmp / "none"), **env}
        script = ('source "$1" && start_tetherd >/dev/null && wait "$tetherd_pid" '
                  '&& env | grep "^TETHER_BROWSER\\|^TETHER_FORGE" | sort || true')
        p = subprocess.run(["bash", "-c", script, "bash", str(ENTRYPOINT)],
                           env=e, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        parse = lambda text: dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
        return parse(self.env_out.read_text()), parse(p.stdout)

    def test_tetherd_gets_the_image_chromium_without_the_sandbox(self):
        got, _ = self.start()
        self.assertEqual(got, {"TETHER_BROWSER_BIN": "/usr/bin/chromium",
                               "TETHER_BROWSER_NO_SANDBOX": "true",
                               "TETHER_FORGE_WEB": "git-ssh.apj.dev=https://git.apj.dev"})

    def test_settings_from_the_pod_win(self):
        got, _ = self.start(AGENTBOX_CHROMIUM="/opt/c", TETHER_BROWSER_NO_SANDBOX="false",
                            TETHER_FORGE_WEB="x=https://y")
        self.assertEqual(got["TETHER_BROWSER_BIN"], "/opt/c")
        self.assertEqual(got["TETHER_BROWSER_NO_SANDBOX"], "false")
        self.assertEqual(got["TETHER_FORGE_WEB"], "x=https://y")
        got, _ = self.start(TETHER_BROWSER_BIN="/opt/other", AGENTBOX_CHROMIUM="/opt/c")
        self.assertEqual(got["TETHER_BROWSER_BIN"], "/opt/other")

    def test_not_set_at_the_top_so_herdr_and_agents_dont_inherit_them(self):
        text = ENTRYPOINT.read_text()
        start = text[text.index("start_tetherd() {"):text.index("\n}\n", text.index("start_tetherd() {"))]
        for var in ("TETHER_BROWSER_BIN", "TETHER_BROWSER_NO_SANDBOX", "TETHER_FORGE_WEB"):
            self.assertIn(f"export {var}=", start)
            self.assertEqual(text.count(f"export {var}="), 1, var)


if __name__ == "__main__":
    unittest.main()

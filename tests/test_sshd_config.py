"""Offline tests for agentbox/sshd_config, the sshd the PC's herdr logs in through.

docs/agentbox.md §herdr machine. sshd runs as `agent` on the pod's loopback; the
PC reaches it only by `kubectl port-forward`. What herdr needs over SSH is exec
channels on stdio, so everything else is off: passwords, keyboard-interactive,
root, every kind of forwarding, sftp.

Two layers: the file as written (each directive once, no Match block that could
loosen it, no Subsystem), and, when an sshd binary is installed, `sshd -T`'s
effective values, which also catch a directive this sshd version ignores.

Run with:
    python3 -m unittest discover -s agentbox/tests -t .
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "sshd_config"
SSHD = next((p for p in ("/usr/sbin/sshd", "/usr/bin/sshd") if Path(p).exists()), None)

# Directive -> the only value allowed, as written (lower-cased directive).
REQUIRED = {
    "port": "2222",
    "listenaddress": "127.0.0.1",
    "addressfamily": "inet",
    "allowusers": "agent",
    "permitrootlogin": "no",
    "authenticationmethods": "publickey",
    "pubkeyauthentication": "yes",
    "authorizedkeysfile": "/etc/agentbox/ssh/authorized_keys",
    "passwordauthentication": "no",
    "permitemptypasswords": "no",
    "kbdinteractiveauthentication": "no",
    "hostbasedauthentication": "no",
    "gssapiauthentication": "no",
    "usepam": "no",
    "disableforwarding": "yes",
    "allowagentforwarding": "no",
    "allowtcpforwarding": "no",
    "allowstreamlocalforwarding": "no",
    "gatewayports": "no",
    "x11forwarding": "no",
    "permittunnel": "no",
    "permitlisten": "none",
    "permitopen": "none",
    "permituserenvironment": "no",
    "permituserrc": "no",
    "maxauthtries": "3",
}
# Never in the file: the host key comes from the entrypoint's -h (it lives on the
# home volume), and the rest would loosen something.
FORBIDDEN = {"subsystem", "match", "hostkey", "challengeresponseauthentication",
             "authorizedkeyscommand", "trustedusercakeys", "include", "acceptenv", "setenv"}


def directives() -> list[tuple[str, str]]:
    out = []
    for line in CONFIG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        out.append((key.lower(), value.strip()))
    return out


class AsWritten(unittest.TestCase):
    def setUp(self):
        self.pairs = directives()
        self.conf = dict(self.pairs)

    def test_each_directive_once(self):
        # sshd takes the FIRST value, so a later duplicate would silently do nothing.
        keys = [k for k, _ in self.pairs]
        self.assertEqual(sorted(k for k in set(keys) if keys.count(k) > 1), [])

    def test_hardening(self):
        for key, want in REQUIRED.items():
            self.assertEqual(self.conf.get(key), want, key)

    def test_nothing_that_loosens_it(self):
        self.assertEqual(sorted(FORBIDDEN & set(self.conf)), [])

    def test_loopback_only(self):
        listen = [v for k, v in self.pairs if k == "listenaddress"]
        self.assertEqual(listen, ["127.0.0.1"])

    def test_unprivileged_port(self):
        # sshd runs as uid 1000 with every capability dropped: no port below 1024.
        self.assertGreaterEqual(int(self.conf["port"]), 1024)


@unittest.skipUnless(SSHD and shutil.which("ssh-keygen"), "no sshd/ssh-keygen installed")
class Effective(unittest.TestCase):
    """`sshd -T`: the values this sshd will actually run with."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        key = Path(cls._tmp.name) / "host_key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        cls.key = key

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def sshd(self, *args):
        p = subprocess.run([SSHD, "-f", str(CONFIG), "-h", str(self.key), *args],
                           capture_output=True, text=True)
        # Run as root (the Forgejo job container), sshd -t/-T insist on the privilege
        # separation directory, /run/sshd, which a host with the binary but no running
        # sshd lacks. The pod runs sshd as uid 1000, where the check does not apply, and
        # it says nothing about the config, so skip rather than fail.
        if p.returncode != 0 and "Missing privilege separation directory" in p.stderr:
            self.skipTest(p.stderr.strip())
        return p

    def test_config_checks_clean(self):
        p = self.sshd("-t")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Unsupported option", p.stderr)
        self.assertNotIn("Deprecated option", p.stderr)

    def test_effective_values_for_the_agent_user(self):
        p = self.sshd("-T", "-C", "user=agent,host=pc,addr=127.0.0.1")
        self.assertEqual(p.returncode, 0, p.stderr)
        got = {}
        for line in p.stdout.splitlines():
            k, _, v = line.partition(" ")
            got.setdefault(k.lower(), v)
        self.assertEqual(got["listenaddress"], "127.0.0.1:2222")
        for key in ("passwordauthentication", "kbdinteractiveauthentication", "hostbasedauthentication",
                    "permitrootlogin", "allowagentforwarding", "allowtcpforwarding",
                    "allowstreamlocalforwarding", "x11forwarding", "permittunnel", "gatewayports",
                    "permituserenvironment", "permituserrc"):
            self.assertEqual(got[key], "no", key)
        self.assertEqual(got["disableforwarding"], "yes")
        self.assertEqual(got["authenticationmethods"], "publickey")
        self.assertEqual(got["authorizedkeysfile"], "/etc/agentbox/ssh/authorized_keys")
        self.assertNotIn("subsystem", got)


if __name__ == "__main__":
    unittest.main()

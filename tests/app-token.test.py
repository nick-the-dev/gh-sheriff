#!/usr/bin/env python3
import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("app_token_module", ROOT / "scripts" / "app-token.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class AppTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.key_path = os.path.join(cls.folder, "key.pem")
        cls.pub_path = os.path.join(cls.folder, "pub.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.key_path, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", cls.key_path, "-pubout", "-out", cls.pub_path], check=True, capture_output=True)
        cls.key = pathlib.Path(cls.key_path).read_text()
        cls.key_b64 = base64.b64encode(cls.key.encode()).decode()

    def test_jwt_is_signed_rs256_and_verifies_with_the_public_key(self):
        token = app.app_jwt("123", self.key, now=1_000_000)
        header, payload, signature = token.split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64url_decode(payload)), {"iat": 999_940, "exp": 1_000_540, "iss": "123"})
        sig_path = os.path.join(self.folder, "sig")
        pathlib.Path(sig_path).write_bytes(b64url_decode(signature))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", self.pub_path, "-signature", sig_path],
                                  input=(header + "." + payload).encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_owner_specific_app_wins_over_the_generic_one(self):
        environ = {"AGENT_APP_ID": "1", "AGENT_APP_KEY_B64": self.key_b64,
                   "AGENT_APP_NICK_THE_DEV_ID": "2", "AGENT_APP_NICK_THE_DEV_KEY_B64": self.key_b64}
        self.assertEqual(app.app_credentials("nick-the-dev", environ)[0], "2")
        self.assertEqual(app.app_credentials("acme", environ)[0], "1")

    def test_missing_app_names_the_variables_to_set(self):
        with self.assertRaisesRegex(app.AppTokenError, "AGENT_APP_SOME_ORG_ID"):
            app.app_credentials("some-org", {})

    def test_installation_token_is_scoped_to_the_one_repository(self):
        calls = []

        def fake_api(method, path, bearer, body=None):
            calls.append((method, path, body))
            return {"id": 42} if method == "GET" else {"token": "ghs_test"}

        environ = {"AGENT_APP_ACME_ID": "7", "AGENT_APP_ACME_KEY_B64": self.key_b64}
        with mock.patch.object(app, "api", side_effect=fake_api):
            self.assertEqual(app.installation_token("acme/widgets", environ), "ghs_test")
        self.assertEqual(calls, [("GET", "/repos/acme/widgets/installation", None),
                                 ("POST", "/app/installations/42/access_tokens", {"repositories": ["widgets"]})])

    def test_requested_permissions_narrow_the_token(self):
        bodies = []
        environ = {"AGENT_APP_ID": "7", "AGENT_APP_KEY_B64": self.key_b64}
        with mock.patch.object(app, "api", side_effect=lambda m, p, b, body=None: bodies.append(body) or ({"id": 1} if m == "GET" else {"token": "t"})):
            app.installation_token("o/r", environ, {"statuses": "write"})
        self.assertEqual(bodies[-1], {"repositories": ["r"], "permissions": {"statuses": "write"}})

    def test_a_key_wrapped_across_lines_is_accepted(self):
        wrapped = "\n".join(self.key_b64[i:i + 76] for i in range(0, len(self.key_b64), 76))
        self.assertIn("PRIVATE KEY", app.app_credentials("o", {"AGENT_APP_ID": "1", "AGENT_APP_KEY_B64": wrapped})[1])

    def test_the_command_never_receives_the_app_keys(self):
        seen = {}
        with mock.patch.object(app, "installation_token", return_value="ghs"), \
             mock.patch.object(app, "app_login", return_value="bot[bot]"), \
             mock.patch.dict(os.environ, {"AGENT_APP_ID": "1", "AGENT_APP_X_KEY_B64": "secret", "GITHUB_ACTIONS": ""}), \
             mock.patch.object(app.subprocess, "run", side_effect=lambda cmd, env, check: seen.update(env) or mock.Mock(returncode=0)):
            self.assertEqual(app.main(["--repo", "o/r", "--permission", "statuses=write", "--", "true"]), 0)
        self.assertEqual([name for name in seen if name.startswith("AGENT_APP_")], [])
        self.assertEqual((seen["GH_TOKEN"], seen["GH_ACTOR_LOGIN"]), ("ghs", "bot[bot]"))

    def test_bad_repository_is_refused(self):
        with self.assertRaises(app.AppTokenError):
            app.installation_token("not a repo", {})

    def test_command_runs_with_the_token_and_its_exit_code_is_returned(self):
        out = os.path.join(self.folder, "seen")
        with mock.patch.object(app, "installation_token", return_value="ghs_seen"), \
             mock.patch.object(app, "app_login", return_value="agents[bot]"):
            code = app.main(["--repo", "o/r", "--", sys.executable, "-c",
                             "import os,sys; open(sys.argv[1],'w').write(os.environ['GH_TOKEN'] + ' ' + os.environ['GH_ACTOR_LOGIN']); sys.exit(3)", out])
        self.assertEqual(code, 3)
        self.assertEqual(pathlib.Path(out).read_text(), "ghs_seen agents[bot]")

    def test_app_login_is_the_slug_as_a_bot(self):
        with mock.patch.object(app, "app_credentials", return_value=("1", "key")), \
             mock.patch.object(app, "app_jwt", return_value="jwt"), \
             mock.patch.object(app, "api", return_value={"slug": "acme-agents"}) as called:
            self.assertEqual(app.app_login("acme/widgets", {}), "acme-agents[bot]")
        self.assertEqual(called.call_args.args[:3], ("GET", "/app", "jwt"))

    def test_failure_to_mint_never_runs_the_command(self):
        with mock.patch.object(app, "installation_token", side_effect=app.AppTokenError("no app")), \
             mock.patch.object(app.subprocess, "run", side_effect=AssertionError("must not run")), \
             mock.patch("sys.stderr"):
            self.assertEqual(app.main(["--repo", "o/r", "--", "true"]), 2)


if __name__ == "__main__":
    unittest.main()

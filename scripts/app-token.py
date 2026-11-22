#!/usr/bin/env python3
"""Run a command as the GitHub App installed on a repository's owner, with a one-hour token scoped to that repository."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API_VERSION = "2022-11-28"
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class AppTokenError(Exception):
    pass


def owner_key(owner):
    return re.sub(r"[^A-Z0-9]", "_", owner.upper())


def app_credentials(owner, environ):
    generic = (environ.get("AGENT_APP_ID"), environ.get("AGENT_APP_KEY_B64"))
    specific = tuple(environ.get("AGENT_APP_" + owner_key(owner) + suffix) for suffix in ("_ID", "_KEY_B64"))
    app_id, key_b64 = specific if all(specific) else generic
    if not (app_id and key_b64):
        raise AppTokenError("no GitHub App for owner " + owner + ": set AGENT_APP_" + owner_key(owner) + "_ID and _KEY_B64")
    try:
        key = base64.b64decode("".join(str(key_b64).split()), validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppTokenError("the App key for " + owner + " is not valid base64") from exc
    return str(app_id).strip(), key


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign(message, key):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "app.pem")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(key)
        result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=message, capture_output=True, check=False)
    if result.returncode:
        raise AppTokenError("openssl could not sign the App JWT: " + result.stderr.decode("utf-8", "replace").strip()[:200])
    return result.stdout


def app_jwt(app_id, key, now=None):
    issued = int(now if now is not None else time.time()) - 60
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"iat": issued, "exp": issued + 600, "iss": app_id}, separators=(",", ":")).encode())
    signing_input = (header + "." + payload).encode("ascii")
    return header + "." + payload + "." + b64url(sign(signing_input, key))


def api(method, path, bearer, body=None):
    base = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + bearer,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "agent-app-token",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AppTokenError("GitHub answered " + str(exc.code) + " to " + method + " " + path) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AppTokenError("cannot reach GitHub for " + method + " " + path + ": " + str(exc)) from exc


def installation_token(repo, environ=None, permissions=None):
    environ = os.environ if environ is None else environ
    if not REPO_RE.match(repo or ""):
        raise AppTokenError("repository must be owner/name, got " + repr(repo))
    owner, name = repo.split("/", 1)
    app_id, key = app_credentials(owner, environ)
    jwt = app_jwt(app_id, key)
    installation = api("GET", "/repos/" + repo + "/installation", jwt)
    body = {"repositories": [name]}
    if permissions:
        body["permissions"] = dict(permissions)
    token = api("POST", "/app/installations/" + str(installation.get("id")) + "/access_tokens", jwt, body)
    if not isinstance(token.get("token"), str) or not token["token"]:
        raise AppTokenError("GitHub returned no installation token for " + repo)
    return token["token"]


def app_login(repo, environ=None):
    environ = os.environ if environ is None else environ
    app_id, key = app_credentials(repo.split("/", 1)[0], environ)
    slug = api("GET", "/app", app_jwt(app_id, key)).get("slug")
    if not isinstance(slug, str) or not slug:
        raise AppTokenError("GitHub returned no App slug")
    return slug + "[bot]"


def repo_from_git():
    result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    match = re.search(r"github\.com[:/]([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$", result.stdout.strip())
    return match.group(1) if result.returncode == 0 and match else ""


def resolve_repo(explicit):
    return explicit or os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY") or repo_from_git()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[arguments.index("--") + 1:] if "--" in arguments else []
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    parser = argparse.ArgumentParser(prog="app-token.py", usage="app-token.py [--repo owner/name] -- command ...")
    parser.add_argument("--repo", default="")
    parser.add_argument("--permission", action="append", default=[], metavar="NAME=LEVEL",
                        help="narrow the token to these App permissions, for example statuses=write")
    args = parser.parse_args(options)
    permissions = dict(item.split("=", 1) for item in args.permission if "=" in item)
    if len(permissions) != len(args.permission):
        parser.error("--permission takes NAME=LEVEL")
    if not command:
        parser.error("give the command to run after --")
    repo = resolve_repo(args.repo)
    try:
        token = installation_token(repo, permissions=permissions)
        login = app_login(repo)
    except AppTokenError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
        return 2
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + token, flush=True)
    child = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_APP_")}
    return subprocess.run(command, env={**child, "GH_TOKEN": token, "GH_ACTOR_LOGIN": login}, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())

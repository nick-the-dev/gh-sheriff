#!/usr/bin/env python3
"""Sheriff reviewer providers: run one read-only review with a chosen provider and model, and report its usage.

A provider spec is `provider:model`, for example `claude:claude-opus-5-5` or
`opencode:opencode-go/deepseek-v4.1-flash`. A bare model name means the Claude provider.
"""
import json
import os
import pathlib
import subprocess
import tempfile

CLAUDE_PACKAGE = "@anthropic-ai/claude-code@2.1.330"
OPENCODE_PACKAGE = "opencode-ai@1.19.5"
MAX_TURNS = 80
SECRETS = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "opencode": "OPENCODE_API_KEY"}
DENIED_READS = ("//proc/**", "//sys/**", "//etc/**", "~/.claude/**", "~/.config/**", "~/.npm/**",
                "//home/runner/work/_temp/**", "./pr/.git/**", "./base/.git/**")


class ReviewerError(Exception):
    pass


def parse_spec(spec):
    text = (spec or "").strip()
    if not text:
        return None
    provider, _, model = text.partition(":") if ":" in text else ("claude", "", text)
    provider, model = provider.strip().lower(), model.strip()
    if provider not in SECRETS or not model or any(c.isspace() for c in model):
        raise ReviewerError("unsupported reviewer '" + text + "': use claude:<model> or opencode:<provider/model>")
    return provider, model


def empty_usage():
    return {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0, "cost_usd": 0.0}


def claude_command(model, prompt):
    command = ["npm", "exec", "--yes", "--package=" + CLAUDE_PACKAGE, "--", "claude", "-p", prompt, "--model", model,
               "--output-format", "json", "--max-turns", str(MAX_TURNS), "--allowedTools", "Read(./**)", "Grep", "Glob",
               "--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"]
    return command + ["Read(" + path + ")" for path in DENIED_READS]


def claude_result(raw):
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReviewerError("Claude Code produced no readable output") from exc
    if not isinstance(data, dict) or data.get("is_error") or not str(data.get("result") or "").strip():
        raise ReviewerError("Claude Code ended without a result")
    usage = empty_usage()
    for numbers in (data.get("modelUsage") or {}).values():
        usage["input"] += int(numbers.get("inputTokens") or 0)
        usage["cache_read"] += int(numbers.get("cacheReadInputTokens") or 0)
        usage["cache_write"] += int(numbers.get("cacheCreationInputTokens") or 0)
        usage["output"] += int(numbers.get("outputTokens") or 0)
        usage["reasoning"] += int(numbers.get("thinkingTokens") or 0)
    usage["cost_usd"] = float(data.get("total_cost_usd") or 0)
    return str(data["result"]), usage


def opencode_config():
    read_rules = {"*": "allow", "pr/.git/**": "deny", "base/.git/**": "deny"}
    denied = ("edit", "bash", "webfetch", "websearch", "task", "skill", "lsp", "question", "todowrite", "external_directory")
    return {"autoupdate": False, "share": "disabled",
            "agent": {"sheriff-review": {"description": "Sheriff read-only reviewer", "mode": "primary", "steps": MAX_TURNS,
                                         "permission": {**{name: "deny" for name in denied}, "read": read_rules,
                                                        "glob": "allow", "grep": "allow", "list": "allow"}}}}


def opencode_command(model, prompt):
    return ["npm", "exec", "--yes", "--package=" + OPENCODE_PACKAGE, "--", "opencode", "run", "--model", model,
            "--agent", "sheriff-review", "--format", "json", prompt]


def opencode_result(raw):
    usage, steps, current = empty_usage(), [], []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        kind = event.get("type")
        if kind == "step_start":
            current = []
        elif kind == "text" and not part.get("synthetic") and isinstance(part.get("text"), str):
            current.append(part["text"])
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            usage["input"] += int(tokens.get("input") or 0)
            usage["output"] += int(tokens.get("output") or 0)
            usage["reasoning"] += int(tokens.get("reasoning") or 0)
            usage["cache_read"] += int(cache.get("read") or 0)
            usage["cache_write"] += int(cache.get("write") or 0)
            usage["cost_usd"] += float(part.get("cost") or 0)
            if current:
                steps.append("".join(current))
    if not steps:
        raise ReviewerError("OpenCode ended without a result")
    return steps[-1], usage


def run(spec, prompt, workdir, environ=None):
    provider, model = spec
    environ = dict(os.environ if environ is None else environ)
    if not (environ.get(SECRETS[provider]) or "").strip():
        raise ReviewerError("the sheriff environment needs the secret " + SECRETS[provider] + " for the " + provider + " reviewer")
    keep = {name: value for name, value in environ.items()
            if name in ("PATH", "HOME", "LANG", "TMPDIR", "RUNNER_TEMP", SECRETS[provider]) or name.startswith("npm_config_")}
    if provider == "claude":
        result = subprocess.run(claude_command(model, prompt), cwd=workdir, env=keep, capture_output=True, text=True, check=False)
        text, usage = claude_result(result.stdout)
    else:
        home = pathlib.Path(tempfile.mkdtemp(prefix="sheriff-opencode-"))
        config = home / "opencode.json"
        config.write_text(json.dumps(opencode_config()))
        keep.update({"OPENCODE_CONFIG": str(config), "XDG_CONFIG_HOME": str(home / "config"),
                     "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state"),
                     "XDG_CACHE_HOME": str(home / "cache"), "OPENCODE_DISABLE_AUTOUPDATE": "1"})
        result = subprocess.run(opencode_command(model, prompt), cwd=workdir, env=keep, capture_output=True, text=True, check=False)
        text, usage = opencode_result(result.stdout)
    return {"provider": provider, "model": model, "text": text, "usage": usage}

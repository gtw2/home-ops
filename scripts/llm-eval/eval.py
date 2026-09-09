#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "pyyaml>=6.0"]
# ///
"""Score a chat model on real failure modes from this cluster.

Every task is something that actually broke here, so a passing score means the
model would have been useful on the day, not that it does well on a benchmark.
Run it against any model litellm serves - local or hosted - and compare.

  ./eval.py --model llama-strix-chat
  ./eval.py --model llama-strix-chat --model gpt-5.6 --repeat 3
  ./eval.py --compare results/*.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).parent
REPO = HERE.parent.parent
DEFAULT_TASKS = HERE / "tasks.yaml"
DEFAULT_OUT = HERE / "results"

# Reasoning models can spend a long time before the first content token, and a
# local 35B on one GPU is slower again when another request holds a slot.
DEFAULT_TIMEOUT = 900.0

# Reasoning models bill thinking against max_tokens and emit content only after
# they stop, so this budget has to clear the SERVED model's reasoning budget
# with room for an answer on top - it is not a "long enough answer" number.
# Measured here twice: at 2048 a competent model scored 0% on every task
# (content="", finish_reason="length"), and at 8192 - still under the
# InferenceService's reasoningBudget of 16384 - the two hardest tasks scored 0%
# for the same reason, one of which scores 100% at this value. If a model is
# deployed with a larger reasoning budget, raise this to match or the eval
# measures truncation instead of quality.
DEFAULT_MAX_TOKENS = 20480

# Models split reasoning from the answer two ways: a sibling `reasoning_content`
# field, or inline <think> tags. Normalise both so grading sees only the answer
# and comparisons across models stay fair.
THINK_TAGS = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)

# A task may target a hosted model, so repo content in a prompt leaves the
# cluster. SOPS files are excluded from every glob unconditionally - they are
# ciphertext and useless as context, and naming a specific file is the only way
# to send one deliberately.
NEVER_GLOB = ("*.sops.yaml", "*.sops.yml", "*.agekey", "*.key")


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


@contextlib.contextmanager
def litellm_connection(base_url: str | None, api_key: str | None):
    """Yield (base_url, api_key), port-forwarding to the in-cluster proxy if needed.

    Explicit --base-url wins. Otherwise we forward a random local port to
    svc/litellm and read the master key out of the cluster, so the common case
    is zero-argument.
    """
    if base_url:
        yield base_url.rstrip("/"), api_key or os.environ.get("LITELLM_API_KEY", "")
        return

    key = api_key or os.environ.get("LITELLM_API_KEY") or _master_key()
    proc = subprocess.Popen(
        ["kubectl", "--namespace", "llm", "port-forward", "svc/litellm", ":4000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        port = _await_forward(proc, what="svc/litellm")
        yield f"http://127.0.0.1:{port}/v1", key
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def _master_key() -> str:
    out = subprocess.run(
        ["kubectl", "--namespace", "llm", "get", "secret", "litellm",
         "--output", "jsonpath={.data.LITELLM_MASTER_KEY}"],
        capture_output=True, text=True, check=True,
    ).stdout
    if not out:
        die("no LITELLM_MASTER_KEY in the llm/litellm secret; pass --api-key")
    import base64
    return base64.b64decode(out).decode().strip()


def _await_forward(proc: subprocess.Popen, timeout: float = 30.0,
                   what: str = "svc/litellm") -> int:
    """Read kubectl's 'Forwarding from 127.0.0.1:NNNNN' line to learn the port."""
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if match := re.search(r"127\.0\.0\.1:(\d+)", line):
            return int(match.group(1))
    proc.terminate()
    die(f"kubectl port-forward to {what} did not come up")


# --------------------------------------------------------------------------
# MCP (toolhive gateway)
# --------------------------------------------------------------------------


class MCPSession:
    """A streamable-HTTP MCP client, just enough of the protocol to run tools.

    The gateway is a toolhive VirtualMCPServer with the optimizer on, so
    tools/list returns exactly two meta-tools - find_tool and call_tool -
    regardless of how many backends are in the group. The model reaches a real
    tool by describing it, then invoking it through call_tool. That indirection
    is why `invoked` unwraps call_tool's tool_name: without it every task would
    look like it called the same one tool.
    """

    def __init__(self, client: httpx.Client, url: str):
        self.client, self.url = client, url
        self.session_id: str | None = None
        self.tools: list[dict] = []
        self.invoked: list[str] = []

    @staticmethod
    def _parse(reply: httpx.Response) -> dict:
        """Responses come back as plain JSON or as a one-event SSE stream."""
        body = reply.text
        # A JSON-RPC notification carries no id, so the server answers 202 with
        # an empty body. That is success, not a parse failure.
        if not body.strip():
            return {}
        if "data: " in body:
            for line in body.splitlines():
                if line.startswith("data: "):
                    body = line[6:]
                    break
        try:
            return json.loads(body)
        except ValueError as exc:
            raise RuntimeError(f"unparseable MCP reply: {exc}: {body[:200]}") from exc

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        reply = self.client.post(self.url, headers=headers, json=payload)
        if reply.status_code >= 400:
            raise RuntimeError(f"MCP HTTP {reply.status_code}: {reply.text[:200]}")
        if sid := reply.headers.get("mcp-session-id"):
            self.session_id = sid
        return self._parse(reply)

    def open(self) -> "MCPSession":
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "llm-eval", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.tools = (listed.get("result") or {}).get("tools") or []
        if not self.tools:
            die(f"MCP gateway at {self.url} advertised no tools")
        return self

    def openai_tools(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t.get("description", ""),
                              "parameters": t.get("inputSchema")
                              or {"type": "object", "properties": {}}}}
                for t in self.tools]

    def call(self, name: str, arguments: dict) -> str:
        """Run one tool. Errors are returned as text, not raised.

        A failed tool call is data the model should see and recover from - the
        same way it would in a real client - not an eval crash.
        """
        if name == "call_tool":
            self.invoked.append(str(arguments.get("tool_name")))
        else:
            self.invoked.append(name)
        try:
            out = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
        except (RuntimeError, httpx.RequestError) as exc:
            return f"tool error: {exc}"
        if err := out.get("error"):
            return f"tool error: {err}"
        result = out.get("result") or {}
        text = "".join(b.get("text", "") for b in result.get("content") or [])
        return text or json.dumps(result)[:4000]


@contextlib.contextmanager
def mcp_connection(url: str | None, enabled: bool):
    """Yield an open MCPSession, or None when --tools was not passed.

    Mirrors litellm_connection: an explicit --mcp-url wins, otherwise forward a
    random local port to the gateway Service so the common case takes no
    arguments.
    """
    if not enabled:
        yield None
        return
    if url:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            yield MCPSession(client, url).open()
        return
    proc = subprocess.Popen(
        ["kubectl", "--namespace", "llm", "port-forward",
         "svc/vmcp-mcp-gateway", ":4483"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        port = _await_forward(proc, what="svc/vmcp-mcp-gateway")
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            yield MCPSession(client, f"http://127.0.0.1:{port}/mcp").open()
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    checks: list[dict[str, Any]]
    system: str | None = None
    context_files: list[str] = field(default_factory=list)
    context_globs: list[str] = field(default_factory=list)
    context: str | None = None
    tools: list[dict] | None = None
    response_format: dict | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    notes: str = ""

    def render(self) -> str:
        """Inline any repo files this task reads, then the question itself.

        Globs are sorted so the prompt is byte-identical between runs; an
        unstable prompt would show up as model variance it is not.
        """
        paths: list[Path] = []
        for rel in self.context_files:
            path = REPO / rel
            if not path.is_file():
                die(f"task {self.id}: context file not found: {rel}")
            paths.append(path)
        for pattern in self.context_globs:
            matched = sorted(REPO.glob(pattern))
            if not matched:
                die(f"task {self.id}: context glob matched nothing: {pattern}")
            paths.extend(p for p in matched
                         if p.is_file() and not _excluded(p))

        blocks = []
        for path in dict.fromkeys(paths):
            blocks.append(f"--- {path.relative_to(REPO)} ---\n{path.read_text()}")
        if self.context:
            blocks.append(self.context.strip())
        if blocks:
            return "\n\n".join(blocks) + "\n\n" + self.prompt.strip()
        return self.prompt.strip()


def _excluded(path: Path) -> bool:
    return any(path.match(pattern) for pattern in NEVER_GLOB)


def load_tasks(path: Path, only: list[str], categories: list[str]) -> list[Task]:
    raw = yaml.safe_load(path.read_text())
    tasks = [Task(**entry) for entry in raw["tasks"]]
    if only:
        tasks = [t for t in tasks if t.id in only]
    if categories:
        tasks = [t for t in tasks if t.category in categories]
    if not tasks:
        die("no tasks matched the given filters")
    return tasks


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------


def strip_fences(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return fenced.group(1).strip() if fenced else text.strip()


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def run_check(check: dict, content: str, tool_calls: list[dict],
              tools_invoked: list[str] | None = None) -> tuple[bool, str]:
    """Return (passed, human-readable detail) for one check."""
    kind = check["kind"]
    tools_invoked = tools_invoked or []

    if kind == "tool_used":
        # Distinct from tool_call, which grades the OpenAI-level call. Through
        # the toolhive gateway that is almost always call_tool, so this grades
        # the backend tool the model actually reached - the thing that says
        # whether it went to the cluster or answered from memory.
        wanted = check["names"]
        hit = [n for n in wanted if n in tools_invoked]
        if check.get("any", True):
            return bool(hit), (f"invoked {hit}" if hit
                               else f"invoked {tools_invoked or 'nothing'}, wanted any of {wanted}")
        missing = [n for n in wanted if n not in tools_invoked]
        return not missing, f"missing {missing}" if missing else f"invoked {wanted}"

    if kind == "used_any_tool":
        return bool(tools_invoked), (f"invoked {tools_invoked}" if tools_invoked
                                     else "no backend tool reached")

    if kind == "regex_all":
        missing = [p for p in check["patterns"]
                   if not re.search(p, content, re.I | re.S)]
        return not missing, f"missing {missing}" if missing else "all present"

    if kind == "regex_any":
        hit = next((p for p in check["patterns"]
                    if re.search(p, content, re.I | re.S)), None)
        return bool(hit), f"matched {hit!r}" if hit else f"none of {check['patterns']}"

    if kind == "regex_none":
        # A wrong-but-confident answer. Catching these is the point: a model
        # that names the real cause AND three invented ones has not diagnosed
        # anything.
        hit = next((p for p in check["patterns"]
                    if re.search(p, content, re.I | re.S)), None)
        return not hit, f"hit forbidden {hit!r}" if hit else "clean"

    if kind == "json_valid":
        try:
            json.loads(strip_fences(content))
            return True, "parsed"
        except (ValueError, TypeError) as exc:
            return False, f"unparseable: {exc}"

    if kind == "json_field":
        try:
            doc = json.loads(strip_fences(content))
        except (ValueError, TypeError) as exc:
            return False, f"unparseable: {exc}"
        actual = dig(doc, check["path"])
        if "equals" in check:
            ok = actual == check["equals"]
            return ok, f"{check['path']}={actual!r} (want {check['equals']!r})"
        return actual is not None, f"{check['path']}={actual!r}"

    if kind == "tool_call":
        if not tool_calls:
            return False, "no tool call emitted"
        wanted = check["name"]
        call = next((c for c in tool_calls
                     if c.get("function", {}).get("name") == wanted), None)
        if not call:
            got = [c.get("function", {}).get("name") for c in tool_calls]
            return False, f"called {got}, wanted {wanted!r}"
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except ValueError as exc:
            return False, f"arguments not valid JSON: {exc}"
        for key, want in (check.get("arguments") or {}).items():
            if args.get(key) != want:
                return False, f"{key}={args.get(key)!r} (want {want!r})"
        return True, f"{wanted}({args})"

    die(f"unknown check kind: {kind}")


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def ask(client: httpx.Client, base_url: str, key: str, model: str,
        task: Task, temperature: float, no_cache: bool = True,
        mcp: "MCPSession | None" = None, max_tool_turns: int = 6) -> dict:
    """One task. Returns timing, token counts and the graded surfaces.

    With `mcp` set this becomes a tool-calling loop: the gateway's tools are
    offered alongside any the task declares, and every tool call the model makes
    is executed and fed back until it answers or hits max_tool_turns. Timing and
    token counts are summed across turns, so tok/s stays comparable with a
    no-tools run while latency honestly includes the round trips.

    With `mcp` None the behaviour is byte-identical to before: one request, tool
    calls recorded but never executed. That is what keeps the committed
    no-tools baselines reproducible.
    """
    messages: list[dict[str, Any]] = []
    if task.system:
        messages.append({"role": "system", "content": task.system})
    messages.append({"role": "user", "content": task.render()})

    tools = list(task.tools or [])
    if mcp is not None:
        # The session is reused across tasks so the gateway keeps one warm
        # optimizer index; the trace is per task, so clear it here.
        mcp.invoked.clear()
        tools += mcp.openai_tools()

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": task.max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if task.response_format:
        payload["response_format"] = task.response_format
    if no_cache:
        # The proxy caches responses in redis (ttl 300). Without this, a re-run
        # inside five minutes replays the stored answer: identical scores, ~0.1s
        # latency, absurd tok/s - and a model swap that looks like it changed
        # nothing. Correctness and speed both have to be measured cold.
        payload["cache"] = {"no-cache": True}

    turns = 0
    elapsed = 0.0
    prompt_tokens = completion_tokens = 0
    all_calls: list[dict] = []
    content = reasoning = ""
    finish_reason = None

    while True:
        payload["messages"] = messages
        started = time.monotonic()
        try:
            reply = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        except httpx.RequestError as exc:
            return {"error": f"{type(exc).__name__}: {exc}",
                    "latency": elapsed + (time.monotonic() - started)}
        elapsed += time.monotonic() - started
        turns += 1

        if reply.status_code != 200:
            return {"error": f"HTTP {reply.status_code}: {reply.text[:300]}",
                    "latency": elapsed}

        answer = reply.json()
        choice = (answer.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = answer.get("usage") or {}
        prompt_tokens += usage.get("prompt_tokens") or 0
        completion_tokens += usage.get("completion_tokens") or 0
        finish_reason = choice.get("finish_reason")

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        if inline := THINK_TAGS.findall(content):
            reasoning += "".join(str(m) for m in inline)
            content = THINK_TAGS.sub("", content).strip()

        calls = message.get("tool_calls") or []
        all_calls.extend(calls)

        # Without a live MCP session a tool call is recorded and left alone, so
        # the tool_call check kind still grades intent. Executing needs somewhere
        # to execute.
        if not calls or mcp is None or turns > max_tool_turns:
            break

        messages.append({"role": "assistant",
                         "content": message.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", ""),
                             "content": mcp.call(fn.get("name", ""), arguments)})

    # Some backends put chain-of-thought in a sibling field. Grade only what a
    # caller would actually receive as the answer, but record the split so a
    # model that buries its answer in reasoning is visible rather than just low.
    return {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": all_calls,
        "tools_invoked": list(mcp.invoked) if mcp is not None else [],
        "turns": turns,
        "finish_reason": finish_reason,
        "latency": elapsed,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_per_second": completion_tokens / elapsed if elapsed > 0 else 0.0,
    }


def grade(task: Task, response: dict) -> dict:
    if response.get("error"):
        return {"score": 0.0, "checks": [], "error": response["error"]}

    content, tool_calls = response["content"], response["tool_calls"]
    invoked = response.get("tools_invoked") or []
    results = []
    for check in task.checks:
        passed, detail = run_check(check, content, tool_calls, invoked)
        results.append({"kind": check["kind"],
                        "label": check.get("label", check["kind"]),
                        "passed": passed, "detail": detail})

    score = sum(r["passed"] for r in results) / len(results) if results else 0.0
    warnings = []
    truncated = response.get("finish_reason") == "length"
    if not content and not tool_calls:
        # Distinguish "thought until it ran out of budget" from "said nothing":
        # the first is a real and comparable weakness, the second is a bug here.
        warnings.append(
            "spent the whole token budget reasoning and never answered"
            if truncated and response.get("reasoning") else
            "empty response")
    elif truncated:
        warnings.append("answer truncated at max_tokens")
    return {"score": score, "checks": results, "warnings": warnings}


def evaluate(client, base_url, key, model, tasks, repeat, temperature,
             verbose, no_cache=True, mcp=None, max_tool_turns=6) -> dict:
    print(f"\n\033[1m{model}\033[0m")
    print("-" * 78)
    records = []

    for task in tasks:
        runs = []
        for attempt in range(repeat):
            response = ask(client, base_url, key, model, task, temperature,
                           no_cache, mcp=mcp, max_tool_turns=max_tool_turns)
            result = grade(task, response)
            runs.append({**result,
                         "reasoning_chars": len(response.get("reasoning", "")),
                         "latency": response.get("latency", 0.0),
                         "tokens_per_second": response.get("tokens_per_second", 0.0),
                         "completion_tokens": response.get("completion_tokens", 0),
                         "prompt_tokens": response.get("prompt_tokens", 0),
                         "tools_invoked": response.get("tools_invoked", []),
                         "turns": response.get("turns", 1),
                         "content": response.get("content", "")})
            mark = "." if result["score"] == 1.0 else ("!" if result["score"] else "x")
            print(f"  {task.id:26s} [{attempt + 1}/{repeat}] {mark}", end="\r")

        score = statistics.mean(r["score"] for r in runs)
        latency = statistics.mean(r["latency"] for r in runs)
        tps = statistics.mean(r["tokens_per_second"] for r in runs)
        colour = "\033[32m" if score == 1.0 else ("\033[33m" if score else "\033[31m")
        used = runs[0].get("tools_invoked") or []
        turns = runs[0].get("turns", 1)
        trace = f"  \033[36m{turns}t {','.join(used[:3])}\033[0m" if used else ""
        print(f"  {task.id:26s} {colour}{score:5.0%}\033[0m  "
              f"{latency:6.1f}s  {tps:5.1f} tok/s  {task.category}{trace}")

        failures = [c for c in runs[0]["checks"] if not c["passed"]]
        if failures and (verbose or score < 1.0):
            for check in failures:
                print(f"      \033[31mmiss\033[0m {check['label']}: {check['detail']}")
        for run in runs:
            for warning in run.get("warnings", []):
                print(f"      \033[33mwarn\033[0m {warning}")
            if error := run.get("error"):
                print(f"      \033[31mfail\033[0m {error}")
        if verbose:
            print(f"      \033[90m{runs[0]['content'][:400]}\033[0m")

        records.append({"id": task.id, "category": task.category, "score": score,
                        "latency": latency, "tokens_per_second": tps,
                        "tools_invoked": used, "turns": turns, "runs": runs})

    return summarise(model, records, repeat, temperature, mcp is not None)


def summarise(model: str, records: list[dict], repeat: int, temperature: float,
              tools: bool = False) -> dict:
    overall = statistics.mean(r["score"] for r in records) if records else 0.0
    by_category: dict[str, list[float]] = {}
    for record in records:
        by_category.setdefault(record["category"], []).append(record["score"])

    print("-" * 78)
    for category, scores in sorted(by_category.items()):
        print(f"  {category:26s} {statistics.mean(scores):5.0%}")
    print(f"  \033[1m{'overall':26s} {overall:5.0%}\033[0m   "
          f"median {statistics.median([r['latency'] for r in records]):.1f}s, "
          f"{statistics.mean([r['tokens_per_second'] for r in records]):.1f} tok/s avg")
    if tools:
        reached = [r for r in records if r.get("tools_invoked")]
        calls = sum(len(r.get("tools_invoked") or []) for r in records)
        print(f"  {'tools':26s} {len(reached)}/{len(records)} tasks used a tool, "
              f"{calls} backend calls")

    return {
        "model": model,
        "overall": overall,
        "by_category": {k: statistics.mean(v) for k, v in by_category.items()},
        "median_latency": statistics.median([r["latency"] for r in records]),
        "mean_tokens_per_second": statistics.mean(
            [r["tokens_per_second"] for r in records]),
        "repeat": repeat,
        "temperature": temperature,
        # Stamped so a tools run is never compared against a no-tools baseline
        # by accident - they are different experiments on the same tasks.
        "tools": tools,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tasks": records,
    }


def dry_run(tasks: list[Task], verbose: bool) -> None:
    """Render every prompt and compile every pattern, calling nothing.

    Catches the two authoring mistakes that otherwise look like a model failing:
    a context path that no longer exists, and a regex that cannot match.
    """
    print(f"\n{'task':30s}{'category':18s}{'chars':>8s}{'~tokens':>9s}  checks")
    print("-" * 78)
    problems = 0
    for task in tasks:
        rendered = task.render()
        for check in task.checks:
            for pattern in check.get("patterns", []):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    print(f"  \033[31mbad regex\033[0m {task.id}: {pattern!r}: {exc}")
                    problems += 1
        print(f"{task.id:30s}{task.category:18s}{len(rendered):8d}"
              f"{len(rendered) // 4:9d}  {len(task.checks)}")
        if verbose:
            print(f"\033[90m{rendered[:600]}\033[0m\n")
    print("-" * 78)
    print(f"{len(tasks)} tasks, "
          f"{sum(len(t.checks) for t in tasks)} checks, "
          f"{problems} problems")
    if problems:
        raise SystemExit(1)


def compare(paths: list[Path]) -> None:
    reports = [json.loads(p.read_text()) for p in paths]
    ids = [t["id"] for t in reports[0]["tasks"]]
    width = max(len(i) for i in ids) + 2

    print(f"\n{'task':{width}s}" + "".join(f"{r['model'][:18]:>20s}" for r in reports))
    print("-" * (width + 20 * len(reports)))
    for task_id in ids:
        row = f"{task_id:{width}s}"
        for report in reports:
            entry = next((t for t in report["tasks"] if t["id"] == task_id), None)
            cell = f"{entry['score']:.0%}" if entry else "-"
            row += f"{cell:>20s}"
        print(row)
    print("-" * (width + 20 * len(reports)))
    print(f"{'overall':{width}s}" + "".join(f"{r['overall']:>19.0%} " for r in reports))
    print(f"{'tok/s':{width}s}"
          + "".join(f"{r['mean_tokens_per_second']:>19.1f} " for r in reports))


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a model on real failures from this cluster.")
    parser.add_argument("--model", action="append", default=[],
                        help="model name as litellm serves it (repeatable)")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint "
                        "(default: port-forward to svc/litellm)")
    parser.add_argument("--api-key", help="default: the llm/litellm master key")
    parser.add_argument("--tasks-file", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--task", action="append", default=[],
                        help="run only this task id (repeatable)")
    parser.add_argument("--category", action="append", default=[],
                        help="run only this category (repeatable)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per task; >1 averages out sampling noise")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--allow-cache", action="store_true",
                        help="let the proxy serve cached responses; off by "
                             "default because it fakes latency and hides "
                             "model changes")
    parser.add_argument("--max-tokens", type=int,
                        help="override every task's token budget; raise it if "
                             "runs warn about reasoning exhausting the budget")
    parser.add_argument("--tools", action="store_true",
                        help="offer the toolhive MCP gateway's tools and execute "
                             "the calls the model makes. Off by default so the "
                             "committed no-tools baselines stay reproducible.")
    parser.add_argument("--mcp-url", help="MCP endpoint (default: port-forward "
                                          "to llm/vmcp-mcp-gateway)")
    parser.add_argument("--max-tool-turns", type=int, default=6,
                        help="cap on tool round trips per task (default: 6)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="directory for JSON reports")
    parser.add_argument("--verbose", action="store_true",
                        help="show the first response of every task")
    parser.add_argument("--compare", nargs="+", type=Path, metavar="REPORT",
                        help="print a table from existing JSON reports and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="render prompts and compile checks without calling "
                             "a model; use this when authoring tasks")
    args = parser.parse_args()

    if args.compare:
        compare(args.compare)
        return
    if not args.model:
        die("give at least one --model (or --compare some reports)")

    tasks = load_tasks(args.tasks_file, args.task, args.category)
    if args.max_tokens:
        for task in tasks:
            task.max_tokens = args.max_tokens

    if args.dry_run:
        dry_run(tasks, args.verbose)
        return

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    with mcp_connection(args.mcp_url, args.tools) as mcp, \
            litellm_connection(args.base_url, args.api_key) as (base_url, key):
        if mcp is not None:
            names = ", ".join(t["name"] for t in mcp.tools)
            print(f"tools: {len(mcp.tools)} from the gateway ({names})")
        with httpx.Client(timeout=args.timeout) as client:
            reports = [
                evaluate(client, base_url, key, model, tasks, args.repeat,
                         args.temperature, args.verbose,
                         no_cache=not args.allow_cache, mcp=mcp,
                         max_tool_turns=args.max_tool_turns)
                for model in args.model
            ]

    written = []
    for report in reports:
        safe = re.sub(r"[^\w.-]", "_", report["model"])
        path = args.out / f"{stamp}-{safe}.json"
        path.write_text(json.dumps(report, indent=2))
        written.append(path)

    print()
    for path in written:
        print(f"wrote {path.relative_to(REPO)}")
    if len(reports) > 1:
        compare(written)


if __name__ == "__main__":
    main()

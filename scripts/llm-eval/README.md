# llm-eval

A quality baseline for the models this cluster serves, so a model swap can be
judged on evidence instead of impressions.

Every task is a real failure from this cluster, and every answer key is the fix
that actually worked. That is the whole design: a model scoring well here would
have been useful on the day, which is not the same thing as scoring well on a
public benchmark.

```sh
task llm:eval MODEL=llama-strix-chat          # score one model
task llm:eval:compare                          # table of the last reports
uv run scripts/llm-eval/eval.py --help         # everything else
```

No arguments are needed to reach the cluster: the script port-forwards to
`svc/litellm` on a random local port and reads the master key out of the
`llm/litellm` secret. Point it elsewhere with `--base-url` / `--api-key`, which
is how you score a hosted model against the same tasks.

## Tools

`--tools` offers the toolhive MCP gateway's tools alongside anything a task
declares, and executes every call the model makes, feeding results back until it
answers or hits `--max-tool-turns` (default 6). Without the flag nothing changes:
one request per task, tool calls recorded but never executed. That is deliberate
- the committed baselines are no-tools runs and have to stay reproducible.

```sh
task llm:eval -- --tools                 # port-forwards to llm/vmcp-mcp-gateway
task llm:eval -- --tools --mcp-url URL   # or point somewhere explicitly
```

Reports are stamped `"tools": true|false`. A tools run and a no-tools run are
different experiments on the same tasks, so `--compare` across that boundary is
measuring two things at once.

### Grading tool use

The gateway runs toolhive's optimizer, so it advertises exactly two tools -
`find_tool` and `call_tool` - no matter how many MCP servers are in the group.
The model reaches a real tool by describing it, then invoking it through
`call_tool`. That indirection means the OpenAI-level tool name is almost always
`call_tool`, which is why there are two check kinds:

- `tool_call` grades the OpenAI-level call (name plus arguments). Unchanged, and
  it still works with tools off - it grades intent, not execution.
- `tool_used` grades the *backend* tool the model actually reached, unwrapped
  from `call_tool`'s `tool_name`. `names:` plus optional `any: false` to require
  all of them. `used_any_tool` is the loose version: did it go to the cluster at
  all, or answer from memory?


## What it measures

| Category | Tasks | Why it is here |
|---|---|---|
| `diagnosis` | 2 | Read symptoms, name the cause. The core homelab job. |
| `reasoning` | 2 | Follow a control-flow trap to its conclusion. |
| `k8s-depth` | 2 | Knowledge you cannot infer from the manifest in front of you. |
| `hardware` | 1 | Strix Halo memory behaviour, which decides what we can run. |
| `tool-use` | 1 | Emits a correct tool call. Fail this and no agent loop works. |
| `structured-output` | 1 | Honours `response_format`. A caller parses this. |
| `long-context` | 1 | ~66k tokens of real manifests, needles ~163k chars apart. |

Scores are the fraction of checks passed, averaged. Per-task partial credit is
deliberate: "diagnosed the cause but proposed a poor fix" is a different result
from "wrong", and collapsing them to pass/fail throws away the signal you want
when comparing two models.

## Reading the output

Alongside the score, watch for:

- **`spent the whole token budget reasoning and never answered`** — a real
  weakness for agent use, not a harness problem, *provided* `--max-tokens` is
  generous. See the budget note below.
- **`answer truncated at max_tokens`** — raise `--max-tokens` and re-run; the
  score is not trustworthy.
- **tok/s far above ~100 with sub-second latency** — you are reading a cached
  response. The proxy caches in redis with a 300s TTL, so the script sends
  `cache: {"no-cache": true}` on every request. `--allow-cache` turns that off,
  and should stay off for anything you intend to compare.

## Two traps this harness had to be taught

Both produced confidently wrong results before they were fixed, and both would
recur in any home-grown eval:

**Reasoning tokens are billed against `max_tokens`.** At the original budget of
2048, `llama-strix-chat` scored **0% on every task** — each response came back
with `content: ""` and `finish_reason: "length"`, having spent its entire
allowance thinking. The model was fine; the harness was measuring whether it
could finish reasoning in 2048 tokens. The default is now 8192, and an empty
answer is reported as a distinct warning rather than silently scored zero.

**A check must test the insight, not the wording.** The CSI task originally
required the phrase "4 of 5" to prove the model had read the DaemonSet count.
A correct diagnosis that instead said the plugin "cannot schedule onto
`framework`" scored a miss. When a check fails, read the response before
believing it — if the answer is right, the check is wrong.

## Adding a task

```sh
uv run scripts/llm-eval/eval.py --model x --dry-run --verbose
```

`--dry-run` renders every prompt and compiles every pattern without calling a
model, which catches the two authoring mistakes that otherwise look like model
failures: a `context_files` path that has since moved, and a regex that cannot
match anything.

Task fields: `id`, `category`, `prompt`, `checks`, and optionally `system`,
`context_files`, `context_globs`, `context`, `tools`, `response_format`,
`max_tokens`, `notes`.

Check kinds: `regex_all`, `regex_any`, `regex_none`, `json_valid`, `json_field`
(`path` plus optional `equals`), and `tool_call` (`name` plus optional
`arguments`).

Guidelines that keep the scores meaningful:

- Use an incident you personally debugged. If the answer key is a guess, the
  task measures nothing.
- Give `regex_any` several phrasings of the same idea. You are scoring
  understanding, not vocabulary.
- Reserve `regex_none` for answers that are actively wrong, never for answers
  that are merely differently worded.
- Prefer several small checks over one big one, so partial credit is informative.

`context_globs` never picks up `*.sops.yaml` or key material — a task may target
a hosted model, so repo content in a prompt leaves the network.

## What leaves the network

Against a local model nothing leaves the cluster. With `--base-url` pointed at a
hosted provider, the prompts go to that provider - and the `long-context` task
inlines **234 manifests, ~263KB** of this repo.

Audited contents of that payload: no credentials, keys, tokens, JWTs or
ciphertext of any kind. `context_globs` never matches `*.sops.yaml` or key
material, and that exclusion is enforced in the loader rather than per task.

It does contain **12 RFC1918 addresses** (the `10.10.40.0/24` node subnet) and
the cluster's internal service names. Meaningless outside the network, but it is
internal topology, so decide deliberately before scoring a hosted model:

```sh
# everything except the repo dump
uv run scripts/llm-eval/eval.py --model <hosted> \
  --category diagnosis --category reasoning --category k8s-depth \
  --category tool-use --category structured-output --category hardware
```

Reports under `results/` and `baseline/` store only responses, never prompts, so
the manifest dump is not written to disk.

## Caveats

- **10 tasks is a smoke test, not a benchmark.** It reliably separates "this
  model is usable here" from "this one is not". It will not resolve two good
  models a few points apart — for that, raise `--repeat` and widen the set.
- **Single-grader risk.** Regex checks reward stating the right thing and cannot
  tell a lucky keyword from real understanding. Skim responses with `--verbose`
  the first time you score a new model.
- **The tasks leak into the fix.** They describe incidents whose write-ups live
  in this repo, so a model trained on it would have an edge. Not a concern
  today; worth remembering if these are ever published.

## Baselines

`baseline/` holds one committed report per set of weights. They are named after
the model, not the service: litellm serves whatever is live as
`llama-strix-chat`, so the report's own filename and its `weights` field are the
only record of what was actually measured.

| weights | overall | tok/s |
|---|---|---|
| Qwen3.6-35B-A3B UD-Q4_K_XL (ROCm) | 77% | 57.2 |
| Qwen3.8-Flash-Next UD-Q3_K_XL (Vulkan, no spec-decode) | 89% | 22.2 |

Flash-Next wins 12 points and regresses on nothing, at ~2.6x slower generation.
The gains are concentrated where a bigger model should help - `waitforidle-deadlock`
33% -> 100%, `long-context` 67% -> 100%, `csi-node-tolerations` 75% -> 100% - which
is a more convincing signal than the headline number, since a uniform lift would
more likely mean the harness moved.

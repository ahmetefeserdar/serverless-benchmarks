# Agent trace format (v0.1)

A **trace** records one agent run: the sequence of LLM turns and tool calls the agent made.
The replayer (`replay.py`) executes a trace without a live LLM: LLM turns become waits of
the recorded latency, tool calls become real invocations of our serverless functions.

All branches (browser, code execution, pipeline) produce and consume traces in this format.

## File

One JSON file per run, e.g. `traces/<tool-or-task>/<trace_id>.json`.

```json
{
  "version": "0.1",
  "trace_id": "compare-serverless-cloud-001",
  "task": "Compare serverless computing and cloud computing.",
  "source": {
    "kind": "recorded",
    "model": "muse-spark-1.3-contributor",
    "agent": "minimal-loop",
    "recorded_at": "2026-11-13"
  },
  "steps": [
    {"id": 1, "type": "llm", "latency_s": 1.2, "input_tokens": 850, "output_tokens": 40},
    {"id": 2, "type": "tool", "tool": "web-fetch",
     "args": {"url": "https://en.wikipedia.org/wiki/Serverless_computing", "max_length": 5000}},
    {"id": 3, "type": "llm", "latency_s": 0.9, "input_tokens": 2100, "output_tokens": 35},
    {"id": 4, "type": "tool", "tool": "web-fetch", "group": 1,
     "args": {"url": "https://en.wikipedia.org/wiki/Cloud_computing", "max_length": 5000}},
    {"id": 5, "type": "tool", "tool": "code-exec", "group": 1,
     "args": {"repo": "small", "command": "pytest -q"}},
    {"id": 6, "type": "llm", "latency_s": 2.5, "input_tokens": 4300, "output_tokens": 410}
  ]
}
```

## Fields

**Top level**

| field | required | meaning |
|---|---|---|
| `version` | yes | format version, currently `"0.1"` |
| `trace_id` | yes | unique name of this run |
| `task` | yes | the prompt the agent was given |
| `source` | yes | `kind`: `"recorded"` (real LLM) or `"synthetic"` (hand-written); `model`, `agent`, `recorded_at` when recorded |
| `steps` | yes | ordered list of steps (below) |

**`llm` step**: the model thinking. Replayed as a wait.

| field | required | meaning |
|---|---|---|
| `latency_s` | yes | recorded wall-clock time of the LLM call, in seconds |
| `input_tokens`, `output_tokens` | no | token counts; used to model context growth and cost |

**`tool` step**: a real call to one of our functions.

| field | required | meaning |
|---|---|---|
| `tool` | yes | benchmark name without the number, e.g. `web-fetch` for `710.web-fetch` |
| `args` | yes | the exact input event passed to the function's `handler(event)` |
| `group` | no | consecutive tool steps with the same `group` run **in parallel** (agents often issue several tool calls at once) |

`id` is optional on every step and only helps referencing steps in results.

## Rules

1. **Reproducible inputs.** `args` must point to fixed inputs (stored page snapshots, pinned repos in storage), not live websites or `git clone` from GitHub. Live URLs are only allowed in `synthetic` traces during prototyping.
2. **No LLM output is needed for replay.** We store latency and token counts, not the model's text. A recorder may keep the raw transcript next to the trace for debugging.
3. **Variable LLM delay is a replay option, not part of the trace.** The replayer can scale or jitter `latency_s` (e.g. to simulate a slower model) without editing traces.
4. **Different runs of the same task are different traces.** Five recordings of one task that call tools differently are five files; that variation is wanted.

## Open questions (to settle as a team)

- Do tool steps need a recorded `expected` outcome for validation, or is the benchmark's own `validate_output` enough?
- How do pipeline steps (function A's output feeding function B) reference earlier results?

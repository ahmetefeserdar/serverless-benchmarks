# Agentic benchmarks for SeBS — overview

Entry point for teammates and for coding agents working in this directory.
State as of 2026-11-19 (progress talk). Slides: [`talks/2026-11-19-progress.pdf`](talks/2026-11-19-progress.pdf).

## Goal

Benchmarks that reproduce how AI agents use tools, deployed as serverless functions, to measure how
agent workloads behave on serverless platforms (latency, cold starts, package size, state, cost).
Mentor: Marcin Copik. Requirements from his 11-03 emails:

- several agentic branches (browser, code execution, workflows), single functions and pipelines;
- **no live LLM** during a benchmark: agent runs are recorded once and replayed;
- reproducible inputs, configurable sizes, correctness checks;
- selection justified by existing agentic applications and traces (AgentSysBench, TrEnv-X, tool-usage studies).

## How it works

```
RECORD  record.py: agent loop -> LLM (OpenAI-compatible API) -> real tool calls   -> traces/<id>.json
REPLAY  replay.py: trace -> sleep recorded LLM latency / invoke tool functions   -> results/<id>.json
```

A **trace** is a list of `llm` steps (latency, tokens) and `tool` steps (tool name + args).
Format, fields and decisions: [`TRACE_FORMAT.md`](TRACE_FORMAT.md).

## Layout

| path | what | owner |
|---|---|---|
| `benchmarks/700.agentic/710.web-fetch/` | MCP fetch-server clone: page from storage snapshot -> main content -> Markdown -> truncate | Ivan |
| `benchmarks/700.agentic/720.code-exec/` | coding agent on a pinned repo: install, run tests, grep, read, patch, lint; one-call or per-step sessions | Ahmet |
| `benchmarks/700.agentic/730.browser/` | headless Chromium (Playwright) on stored sites: form, pinned Wikipedia page, synthetic 2,000-item shop | Jan |
| `benchmarks/700.agentic/740.pipeline/` | produce/consume steps handing results over through storage or inline | Elif |
| `agentic/record.py` | records traces from any OpenAI-compatible model (`--mock` for a scripted fake) | Ivan |
| `agentic/replay.py` | the shared replayer for all four benchmarks | Ivan |
| `agentic/snapshot.py` | pins web pages (Wikipedia by revision id) for `710.web-fetch` | Ivan |
| `agentic/pipeline.py` | Elif's first pipeline runner (superseded by `replay.py`, kept for reference) | Elif |
| `agentic/traces/` | recorded and synthetic traces (`*.transcript.json` are local only, ignored) | all |
| `agentic/results/` | replay outputs (ignored by git; regenerate) | — |
| `agentic/PLAN.md` | roles and milestones | — |

Category number 700 because 600 is taken by SeBS workflows. Each benchmark: `config.json`,
`input.py` (`generate_input`, `validate_output`, sizes test/small/large), `python/function.py`.

## Run it locally

Requires Docker (usable without sudo), Python 3.10+, libcurl headers. From the repository root:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/sebs storage start all configs/storage.json --output-json out_storage.json

# web-fetch needs page snapshots in benchmarks-data (not committed, see "Known issues")
python3 agentic/snapshot.py pin Serverless_computing Cloud_computing United_States

# deploy each benchmark locally; every command writes the function address + default input
.venv/bin/sebs local start 710.web-fetch test  out_benchmark.json        --config configs/example.json --storage-configuration out_storage.json --deployments 1 --architecture x64
.venv/bin/sebs local start 720.code-exec large out_720.code-exec.json    --config configs/example.json --storage-configuration out_storage.json --deployments 1 --architecture x64
.venv/bin/sebs local start 730.browser   test  out_730.browser.json      --config configs/example.json --storage-configuration out_storage.json --deployments 1 --architecture x64
.venv/bin/sebs local start 740.pipeline  test  out_740.pipeline.json     --config configs/example.json --storage-configuration out_storage.json --deployments 1 --architecture x64

# replay the demo trace that uses all four benchmarks
python3 agentic/replay.py agentic/traces/demo-all-tools-001.json \
  out_benchmark.json out_720.code-exec.json out_730.browser.json out_740.pipeline.json \
  --repeat 3 --out agentic/results/demo.json
```

Replay options: `--repeat N`, `--latency-scale X` (scale recorded LLM latencies),
`--session-mode one-call|per-step`, `--out file`.

Record new traces with a local model (no API key; Ollama in Docker, qwen2.5:3b):

```bash
docker run -d --name ollama -v ollama:/root/.ollama -p 127.0.0.1:11434:11434 ollama/ollama
docker exec ollama ollama pull qwen2.5:3b
export LLM_BASE_URL=http://localhost:11434/v1 LLM_API_KEY=ollama LLM_MODEL=qwen2.5:3b
python3 agentic/record.py "<task>" out_benchmark.json agentic/traces/<id>.json
```

Any OpenAI-compatible endpoint works with the same three variables. Never commit API keys or
`out_storage.json` (contains storage credentials).

## Results so far (local, laptop, SeBS local backend)

| finding | numbers | source |
|---|---|---|
| web-fetch is CPU-bound | 3 MB page: 2.4 s parsing vs 10 ms fetch from storage | `sebs benchmark invoke 710.web-fetch large` |
| snapshots make replay stable | 2.00 s ± 0.03 s vs 25 s in one run against live Wikipedia | `traces/mock-compare-001.json` |
| browser is heavy | ~475 MB package (2× AWS Lambda zip limit), 8 processes, 350–450 MB; warm run 0.74 s, of which 0.35 s closing Chromium | `730.browser/README.md`, demo replay |
| sessions: one call vs per step | billed 5.06 s vs 3.91 s (−23%), end-to-end 6.22 s vs 6.67 s (+7%); per step on fresh containers 11.7 s (Ahmet) | `trace_code_fix_add.json`, 3 runs each mode |
| pipeline hand-off | for 1 MB, hand-off plus invocation overhead exceeds the work | `READMe_pipeline.md` |
| agent behaviour varies | 28 traces from qwen2.5:3b: same task → 1 page or 2 parallel fetches; re-reads; tool errors | `traces/batch-*.json`, `traces/ollama-*.json` |
| all four together | demo task: 8.7 s per run, stable over 3 runs, tools 53% (synthetic ~1 s LLM turns) | `traces/demo-all-tools-001.json` |

Caveat: with a CPU-only local model, LLM time is >96% of every recorded trace. Realistic
latencies need a hosted model (Muse credits requested from Copik) or `--latency-scale`.

## Known issues

- The SeBS local server (`dockerfiles/local/python/server.py`) sets `begin`/`end` around the
  function import only, not the handler. `replay.py` reports it as `import_s` and takes handler
  time from each benchmark's own measurements.
- Page snapshots live in the `benchmarks-data` submodule (Copik's data repository) and are not
  committed here; recreate them with `snapshot.py pin`.
- Locally one container serves a parallel group sequentially; on a cloud platform the second call
  starts a new container. Compare there.
- `replay.py` calls local containers over HTTP directly; cloud runs need SeBS triggers.
- Docker: `docker-credential-desktop not installed` → remove `"credsStore": "desktop"` from
  `~/.docker/config.json`. Windows needs WSL + Docker (Ahmet has a guide).

## Next

- Cloud runs (AWS/GCP free tier) through SeBS triggers; session modes with real cold starts.
- Realistic traces (Muse credits); more tasks per benchmark, including browser and code tasks.
- Tool selection justified against AgentSysBench and tool-usage studies.
- SeBS linting (`tools/linting.py`), one PR per benchmark to `spcl/serverless-benchmarks`.

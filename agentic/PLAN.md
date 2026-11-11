# DPHPC — agentic benchmarks for SeBS

Mentor: Marcin Copik. Goal (from his 11-03 emails): a collection of serverless benchmarks that
represent AI agent operations, both single functions and multi-step pipelines, runnable on any
serverless platform. **No live LLM**: we record agent runs once and replay them (see
[TRACE_FORMAT.md](TRACE_FORMAT.md)). We measure how agent workloads behave on serverless,
not how good the agents are. Every choice must be justified by existing agentic applications,
traces or published characterizations (AgentSysBench, TrEnv-X, Copilot study, TraceLab).

## Repo workflow

- Team fork: `ahmetefeserdar/serverless-benchmarks` = `origin`; `spcl/serverless-benchmarks` = `upstream`.
- One branch per benchmark (e.g. `agentic/web-fetch`); later one PR per benchmark to upstream.
- Benchmarks live in `benchmarks/700.agentic/` (600 is taken by SeBS workflows).
- Template: `benchmarks/700.agentic/710.web-fetch` (`config.json`, `input.py`, `python/function.py`, `python/requirements.txt`).
- Setup: install SeBS, `sebs storage start all configs/storage.json --output-json out_storage.json`, then `sebs benchmark invoke ... --deployment local --storage-configuration out_storage.json`. Windows needs WSL + Docker (Ahmet has a guide + script).
- Never commit `out_storage.json` (contains storage keys) or API keys.

## Roles

| # | branch | owner | what |
|---|---|---|---|
| 1 | Traces & replay | Ivan | trace format, recording with Muse credits, replay player, integration, measurement scripts, tool-usage justification |
| 2 | Browser agent | Jan | headless Chromium in a function (open, click, extract); inputs = stored page snapshots |
| 3 | Code / repo execution | Ahmet | clone → install → test/lint; inputs = pinned repos in storage, not GitHub at runtime |
| 4 | Agentic workflow / pipeline | Elif (tbc) | several functions chained by a trace; measure communication vs. work |

Each benchmark needs test/small/large inputs, a `validate_output` correctness check, and measurements
of cold start, memory, package size and runtime.

## Milestones

| date | what |
|---|---|
| 11-13 | trace format agreed; each tool runs once in SeBS locally |
| 11-17 | one trace replayed end-to-end + progress slides |
| before 11-20 | progress talk with Copik (date being arranged) |
| December | cloud runs (AWS/GCP free tier), one PR per benchmark |
| 12-14..18 | final talk |
| 01-08 | report (8 pages) |

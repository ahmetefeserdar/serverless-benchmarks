# 740.pipeline: passing results between agent steps

An agent task is several tool calls in a row, where later steps use earlier
results. Serverless functions don't share memory, so each result has to be
handed off: inside the request/response (`inline`) or through object storage
(`storage`). This benchmark measures how much of a multi-step task is real
work and how much is that hand-off.

The function has two steps (chosen with `"step"` in the input):

- `produce`: makes a text result of `size_bytes` (stands in for a fetched page,
  test log, ...) and hands it off in `mode` `storage` or `inline`.
- `consume`: gets the earlier results in `inputs`, loads them from storage if
  needed, checks their SHA-256 and counts words.

Later, `produce` can be replaced by the real tools (web-fetch, browser,
code-exec).

Input sizes: test = 10 KB, small = 1 MB, large = 10 MB.

## Run a single step with SeBS

```bash
sebs storage start all configs/storage.json --output-json out_storage.json
sebs local start 740.pipeline test out_pipeline.json \
  --config configs/example.json --storage-configuration out_storage.json \
  --deployments 1 --remove-containers
```

## Run a whole pipeline from a trace

```bash
python agentic/pipeline.py agentic/traces/pipeline/two-step-001.json out_pipeline.json
python agentic/pipeline.py agentic/traces/pipeline/two-step-001.json out_pipeline.json --mode inline --size 51200
python agentic/pipeline.py agentic/traces/pipeline/fan-in-001.json  out_pipeline.json --output results.json
```

Options: `--mode storage|inline`, `--size BYTES`, `--llm-scale 0` (skip LLM
waits), `--output FILE` (save per-step numbers for plots).

For each tool call the runner prints `work` (function's own job), `storage`
(upload/download) and `overhead` (rest of the round trip: HTTP call, JSON,
inline data). The last line gives the hand-off share of tool time.

## Trace extension: `inputs_from`

A tool step can list earlier step ids in `"inputs_from": [2, 3]`. The runner
passes those steps' results to the function as `args["inputs"]`. This is the
proposed answer to the open question in `agentic/TRACE_FORMAT.md`.

## Known limits / notes

- The SeBS local function server (bottle) rejects request bodies over ~100 KB
  with HTTP 413, so `inline` only works for small results locally. Clouds have
  their own limits (e.g. AWS Lambda synchronous payload 6 MB).
- Locally both steps run in the same container, so only the first call is a
  cold start, and parallel calls queue up in one server process (this shows as
  `overhead`). Deploying the steps as separate functions comes with the cloud
  runs.

"""Record an agent run as a trace (see TRACE_FORMAT.md).

A minimal tool-calling agent loop: the LLM decides, our deployed functions run
the tools for real, and every LLM turn and tool call is written to the trace.

LLM: any OpenAI-compatible chat completions API, configured with
LLM_BASE_URL, LLM_API_KEY and LLM_MODEL. With --mock, a scripted fake LLM is
used instead, so the loop can be tested without an API key.

Pages are fetched live while recording (the LLM must see real content), then
stored as snapshots and the trace is rewritten to reference them, so replays
are reproducible. Restart the function (`sebs local start`) afterwards so the
new snapshots get uploaded to storage.

Usage:
  python3 record.py "<task>" <endpoints.json> <out_trace.json> [--mock]
"""
import concurrent.futures
import datetime
import json
import os
import sys
import time
import urllib.request

import snapshot
from replay import load_deployment, make_event

MAX_TURNS = 10

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web-fetch",
            "description": "Fetch a URL and return its main content as Markdown, truncated to max_length "
                           "characters. Use start_index to read further into a truncated page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_length": {"type": "integer", "default": 5000},
                    "start_index": {"type": "integer", "default": 0},
                },
                "required": ["url"],
            },
        },
    },
]

SYSTEM_PROMPT = ("You are a research assistant. Never answer from memory: always read the relevant "
                 "Wikipedia pages with the web-fetch tool first (URLs like https://en.wikipedia.org/wiki/Title), "
                 "read further with start_index if a page is truncated and you need more, then answer concisely.")


def post_json(url, payload, headers=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class OpenAICompatibleLLM:
    def __init__(self):
        self.base_url = os.environ["LLM_BASE_URL"].rstrip("/")
        self.api_key = os.environ["LLM_API_KEY"]
        self.model = os.environ["LLM_MODEL"]

    def chat(self, messages):
        return post_json(f"{self.base_url}/chat/completions",
                         {"model": self.model, "messages": messages, "tools": TOOLS},
                         headers={"Authorization": f"Bearer {self.api_key}"})


class MockLLM:
    """Scripted responses in the OpenAI format: two parallel fetches, one follow-up, an answer."""
    model = "mock"

    def __init__(self):
        self.turn = 0

    def chat(self, messages):
        time.sleep(0.3)
        script = [
            [("web-fetch", {"url": "https://en.wikipedia.org/wiki/Serverless_computing"}),
             ("web-fetch", {"url": "https://en.wikipedia.org/wiki/Cloud_computing"})],
            [("web-fetch", {"url": "https://en.wikipedia.org/wiki/Cloud_computing", "start_index": 5000})],
        ]
        calls = script[self.turn] if self.turn < len(script) else []
        self.turn += 1
        message = {"role": "assistant", "content": None if calls else "Serverless is a subset of cloud computing..."}
        if calls:
            message["tool_calls"] = [
                {"id": f"call_{self.turn}_{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(calls)
            ]
        return {"choices": [{"message": message}],
                "usage": {"prompt_tokens": 500 * self.turn, "completion_tokens": 40}}


def run_tool(tools, name, args):
    # A failing tool returns an error to the model instead of stopping the run,
    # as agent frameworks do (e.g. MCP's isError results).
    url, defaults = tools[name]
    try:
        out = post_json(url, make_event(defaults, args))
        return out["result"]["result"]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def freeze_pages(steps):
    """Snapshot every page fetched live and point the trace at the snapshots."""
    for step in steps:
        if step["type"] == "tool" and step["tool"] == "web-fetch" and "url" in step["args"] and not step.get("error"):
            args = dict(step["args"])
            args["page"] = snapshot.save(args.pop("url"))
            step["args"] = args


def record(task, endpoints_path, out_path, mock=False):
    llm = MockLLM() if mock else OpenAICompatibleLLM()
    tools = load_deployment(endpoints_path)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    steps = []
    group = 0

    for _ in range(MAX_TURNS):
        t0 = time.perf_counter()
        response = llm.chat(messages)
        latency = time.perf_counter() - t0
        usage = response.get("usage", {})
        steps.append({"id": len(steps) + 1, "type": "llm", "latency_s": round(latency, 3),
                      "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})

        message = response["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            break

        # Several tool calls in one LLM turn are issued in parallel, as agents do.
        group += 1
        parsed = [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(lambda c: run_tool(tools, c[1], c[2]), parsed))
        for (call_id, name, args), result in zip(parsed, results):
            step = {"id": len(steps) + 1, "type": "tool", "tool": name, "args": args}
            if "error" in result:
                step["error"] = result["error"]
                print(f"  tool error (returned to model): {name} {args} -> {result['error']}")
            if len(parsed) > 1:
                step["group"] = group
            steps.append(step)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})

    freeze_pages(steps)
    trace = {
        "version": "0.1",
        "trace_id": os.path.splitext(os.path.basename(out_path))[0],
        "task": task,
        "source": {"kind": "recorded" if not mock else "synthetic", "model": llm.model,
                   "agent": "minimal-loop", "recorded_at": datetime.date.today().isoformat()},
        "steps": steps,
    }
    json.dump(trace, open(out_path, "w"), indent=2)
    # Raw conversation for debugging only; not needed for replay.
    json.dump(messages, open(out_path.replace(".json", ".transcript.json"), "w"), indent=2)
    n_llm = sum(s["type"] == "llm" for s in steps)
    print(f"wrote {out_path}: {len(steps)} steps ({n_llm} llm, {len(steps) - n_llm} tool)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--mock"]
    record(args[0], args[1], args[2], mock="--mock" in sys.argv)

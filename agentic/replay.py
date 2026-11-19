"""Replay an agent trace against SeBS functions (see TRACE_FORMAT.md).

llm steps     -> sleep for the recorded latency (stands in for the model)
tool steps    -> HTTP call to the deployed function, timed; consecutive tool
                 steps with the same group run in parallel
inputs_from   -> results of the listed earlier steps are passed to the
                 function as args["inputs"] (pipelines)
session steps -> all steps of one session, including the LLM turns between
                 them, run inside a single invocation, so the workspace
                 survives between tool calls (stateful tools)

The event sent to a function is the deployment's default input (bucket names
etc., from `sebs local start` output) overridden by the step's args.

Usage:
  python3 replay.py <trace.json> <endpoints.json> [<endpoints.json> ...]
                    [--repeat N] [--latency-scale X] [--out results.json]
"""
import argparse
import concurrent.futures
import json
import time
import urllib.request


def load_deployment(paths):
    # tool name -> (function URL, default input event), from `sebs local start` outputs
    if isinstance(paths, str):
        paths = [paths]
    tools = {}
    for path in paths:
        deployed = json.load(open(path))
        for function, inputs in zip(deployed["functions"], deployed["inputs"]):
            name = function["benchmark"].split(".", 1)[1]
            tools[name] = ("http://" + function["url"], inputs)
    return tools


def call(url, event):
    req = urllib.request.Request(url, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def make_event(defaults, args):
    event = dict(defaults)
    if "url" in args:
        # Live URL (prototype traces): do not let the default snapshot win.
        event.pop("page", None)
    event.update(args)
    return event


def handler_seconds(measurement):
    """Time spent in the handler, from the benchmark's own measurements.

    The local SeBS server's begin/end only wrap the import of the function
    (dockerfiles/local/python/server.py), so they cannot be used. Benchmarks
    report microseconds as *_time or milliseconds as *_ms; replayed LLM
    waits inside a session are not handler work. A benchmark that reports
    phases together with their total (730.browser) is taken by its total.
    """
    if isinstance(measurement.get("total_time"), (int, float)):
        return measurement["total_time"] / 1e6
    total = 0.0
    for key, value in measurement.items():
        if not isinstance(value, (int, float)) or key.startswith("llm_wait"):
            continue
        if key.endswith("_time"):
            total += value / 1e6
        elif key.endswith("_ms"):
            total += value / 1e3
    # Per-tool totals of a session (720.code-exec)
    for tool in (measurement.get("tools") or {}).values():
        total += tool.get("wall_ms", 0) / 1e3
    return total


def session_span(steps, i):
    """Indices of all steps belonging to the session that starts at step i."""
    session = steps[i]["session"]
    last = max(j for j, s in enumerate(steps) if s.get("session") == session)
    return list(range(i, last + 1))


def inner_step(s):
    if s["type"] == "llm":
        return {"type": "llm", "latency_s": s["latency_s"]}
    args = {k: v for k, v in s["args"].items() if k not in ("op", "repo")}
    return {"type": "tool", "tool": s["args"]["op"], "args": args, "expect": s.get("expect", {})}


def session_event(defaults, span_steps, latency_scale):
    # Translate trace steps into the in-function trace of a session-based tool
    # (720.code-exec): "op" selects the tool inside the function.
    inner = [inner_step(s) for s in span_steps]
    event = dict(defaults)
    event.update({"trace": inner, "llm_scale": latency_scale})
    return event


def tool_row(rep, step, out, wall, group_size):
    m = out["result"].get("measurement", {})
    return {"rep": rep, "step": step.get("id"), "type": "tool", "tool": step["tool"],
            "group": step.get("group") if group_size > 1 else None, "wall_s": wall,
            "is_cold": out.get("is_cold"), "import_s": float(out["end"]) - float(out["begin"]),
            "function_s": handler_seconds(m),
            "measurement": {k: v for k, v in m.items() if isinstance(v, (int, float))}}


def replay_session_per_step(rows, rep, span_steps, tools, latency_scale, log):
    """One invocation per tool step; the function saves its workspace to storage
    and the next step loads it (720.code-exec per-step mode). LLM waits happen
    here, on the client, so the function is not billed while the model thinks."""
    first = next(s for s in span_steps if s["type"] == "tool")
    url, defaults = tools[first["tool"]]
    key = None
    for s in span_steps:
        sid = s.get("id")
        t0 = time.perf_counter()
        if s["type"] == "llm":
            time.sleep(s["latency_s"] * latency_scale)
            dt = time.perf_counter() - t0
            rows.append({"rep": rep, "step": sid, "type": "llm", "wall_s": dt})
            log(f"{sid:2}. {dt:6.2f}s  LLM  think")
            continue
        event = dict(defaults)
        event.update({"trace": [inner_step(s)], "llm_scale": 0.0,
                      "session": {"id": first["session"], "load": key, "save": "changed"}})
        out = call(url, event)
        dt = time.perf_counter() - t0
        key = out["result"]["result"].get("session", {}).get("key", key)
        row = tool_row(rep, s, out, dt, 1)
        inner = out["result"]["result"].get("steps", [])
        row.update({"type": "tool", "session": first["session"],
                    "diverged": any(x.get("matches_recording") is False for x in inner if x.get("type") == "tool")})
        rows.append(row)
        log(f"{sid:2}. {dt:6.2f}s  TOOL {s['tool']:11} op={s['args']['op']:12} cold={str(out.get('is_cold')):5} "
            f"function={row['function_s']:.2f}s diverged={row['diverged']}")


def replay_once(trace, tools, latency_scale, rep, verbose, session_mode="one-call"):
    rows = []
    results = {}
    steps = trace["steps"]
    log = print if verbose else (lambda *a, **k: None)
    start = time.perf_counter()
    i = 0
    while i < len(steps):
        step = steps[i]
        sid = step.get("id", i + 1)
        t0 = time.perf_counter()

        if step["type"] == "llm":
            time.sleep(step["latency_s"] * latency_scale)
            dt = time.perf_counter() - t0
            rows.append({"rep": rep, "step": sid, "type": "llm", "wall_s": dt})
            log(f"{sid:2}. {dt:6.2f}s  LLM  think")
            i += 1
            continue

        if step.get("error"):
            # The recorded call failed (e.g. a non-existent page); it has no
            # stored input, so it is skipped and only reported.
            rows.append({"rep": rep, "step": sid, "type": "tool-error", "wall_s": 0.0})
            log(f"{sid:2}.   skip  TOOL {step['tool']:11} (failed while recording)")
            i += 1
            continue

        if "session" in step and session_mode == "per-step":
            span = session_span(steps, i)
            replay_session_per_step(rows, rep, [steps[j] for j in span], tools, latency_scale, log)
            i = span[-1] + 1
            continue

        if "session" in step:
            span = session_span(steps, i)
            span_steps = [steps[j] for j in span]
            url, defaults = tools[step["tool"]]
            out = call(url, session_event(defaults, span_steps, latency_scale))
            dt = time.perf_counter() - t0
            row = tool_row(rep, step, out, dt, 1)
            inner = out["result"]["result"].get("steps", [])
            row.update({"type": "session", "session": step["session"], "steps": [steps[j].get("id") for j in span],
                        "llm_wait_s": out["result"]["measurement"].get("llm_wait_ms", 0) / 1e3,
                        "diverged": [s.get("tool") for s in inner if s.get("type") == "tool"
                                     and s.get("matches_recording") is False]})
            rows.append(row)
            n_tools = sum(s["type"] == "tool" for s in span_steps)
            log(f"{sid:2}. {dt:6.2f}s  SESSION {step['tool']:8} '{step['session']}': {n_tools} tool calls + "
                f"{len(span_steps) - n_tools} LLM waits in ONE invocation, cold={out.get('is_cold')}, "
                f"llm-wait inside function={row['llm_wait_s']:.2f}s, diverged={row['diverged'] or 'none'}")
            i = span[-1] + 1
            continue

        batch = [step]
        while ("group" in step and i + len(batch) < len(steps)
               and steps[i + len(batch)].get("group") == step["group"] and not steps[i + len(batch)].get("error")):
            batch.append(steps[i + len(batch)])

        def run(s):
            url, defaults = tools[s["tool"]]
            args = dict(s["args"])
            if "inputs_from" in s:
                args["inputs"] = [results[j] for j in s["inputs_from"]]
            return call(url, make_event(defaults, args))

        with concurrent.futures.ThreadPoolExecutor() as pool:
            outs = list(pool.map(run, batch))
        dt = time.perf_counter() - t0
        for k, (s, out) in enumerate(zip(batch, outs)):
            results[s.get("id", i + k + 1)] = out["result"]["result"]
            row = tool_row(rep, s, out, dt, len(batch))
            rows.append(row)
            par = f" [parallel group {s['group']}]" if len(batch) > 1 else ""
            fed = f" [inputs from {s['inputs_from']}]" if "inputs_from" in s else ""
            log(f"{s.get('id', i + k + 1):2}. {dt:6.2f}s  TOOL {s['tool']:11} cold={str(out.get('is_cold')):5} "
                f"function={row['function_s']:.2f}s{par}{fed}")
        i += len(batch)

    total = time.perf_counter() - start
    llm = sum(r["wall_s"] for r in rows if r["type"] == "llm")
    tool = total - llm
    print(f"rep {rep}: total {total:.2f}s | LLM (replayed) {llm:.2f}s | tools {tool:.2f}s ({tool / total:.0%})")
    return rows, {"rep": rep, "total_s": total, "llm_s": llm, "tool_s": tool}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("endpoints", nargs="+")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--latency-scale", type=float, default=1.0)
    parser.add_argument("--session-mode", choices=["one-call", "per-step"], default="one-call",
                        help="one-call: a whole session in one invocation; per-step: one invocation per "
                             "tool step with workspace save/load through storage")
    parser.add_argument("--out")
    args = parser.parse_args()

    trace = json.load(open(args.trace))
    tools = load_deployment(args.endpoints)
    print(f"task: {trace['task']}\n")
    steps, runs = [], []
    for rep in range(args.repeat):
        rows, summary = replay_once(trace, tools, args.latency_scale, rep, verbose=(rep == 0),
                                    session_mode=args.session_mode)
        steps += rows
        runs.append(summary)
    if args.out:
        json.dump({"trace_id": trace["trace_id"], "latency_scale": args.latency_scale,
                   "session_mode": args.session_mode,
                   "runs": runs, "steps": steps}, open(args.out, "w"), indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()

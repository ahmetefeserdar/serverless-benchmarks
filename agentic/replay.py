"""Replay an agent trace against SeBS functions (see TRACE_FORMAT.md).

llm steps  -> sleep for the recorded latency (stands in for the model)
tool steps -> HTTP call to the deployed function, timed; consecutive tool
              steps with the same group run in parallel

The event sent to a function is the deployment's default input (bucket names
etc., from `sebs local start` output) overridden by the step's args.

Usage:
  python3 replay.py <trace.json> <endpoints.json> [--repeat N] [--latency-scale X] [--out results.json]
"""
import argparse
import concurrent.futures
import json
import time
import urllib.request


def load_deployment(path):
    # tool name -> (function URL, default input event), from `sebs local start` output
    deployed = json.load(open(path))
    tools = {}
    for function, inputs in zip(deployed["functions"], deployed["inputs"]):
        name = function["benchmark"].split(".", 1)[1]
        tools[name] = ("http://" + function["url"], inputs)
    return tools


def call(url, event):
    req = urllib.request.Request(url, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def make_event(defaults, args):
    event = dict(defaults)
    if "url" in args:
        # Live URL (prototype traces): do not let the default snapshot win.
        event.pop("page", None)
    event.update(args)
    return event


def replay_once(trace, tools, latency_scale, rep, verbose):
    rows = []
    steps = trace["steps"]
    start = time.perf_counter()
    i = 0
    while i < len(steps):
        step = steps[i]
        t0 = time.perf_counter()
        if step["type"] == "llm":
            time.sleep(step["latency_s"] * latency_scale)
            dt = time.perf_counter() - t0
            rows.append({"rep": rep, "step": step.get("id", i + 1), "type": "llm", "wall_s": dt})
            if verbose:
                print(f"{step.get('id', i + 1):2}. {dt:5.2f}s  LLM  think")
            i += 1
            continue

        if step.get("error"):
            # The recorded call failed (e.g. a non-existent page); it has no
            # stored input, so it is skipped and only reported.
            if verbose:
                print(f"{step.get('id', i + 1):2}.  skip  TOOL {step['tool']:9} (failed while recording)")
            rows.append({"rep": rep, "step": step.get("id", i + 1), "type": "tool-error", "wall_s": 0.0})
            i += 1
            continue

        batch = [step]
        while ("group" in step and i + len(batch) < len(steps)
               and steps[i + len(batch)].get("group") == step["group"] and not steps[i + len(batch)].get("error")):
            batch.append(steps[i + len(batch)])

        def run(s):
            url, defaults = tools[s["tool"]]
            return call(url, make_event(defaults, s["args"]))

        with concurrent.futures.ThreadPoolExecutor() as pool:
            outs = list(pool.map(run, batch))
        dt = time.perf_counter() - t0
        for k, (s, out) in enumerate(zip(batch, outs)):
            m = out["result"].get("measurement", {})
            # The local SeBS server's begin/end only wrap the import of the function
            # (dockerfiles/local/python/server.py), so it is reported as import time;
            # handler time comes from the benchmark's own measurements.
            import_s = float(out["end"]) - float(out["begin"])
            function_s = sum(v for key, v in m.items() if key.endswith("_time")) / 1e6
            rows.append({"rep": rep, "step": s.get("id", i + k + 1), "type": "tool", "tool": s["tool"],
                         "group": s.get("group"), "wall_s": dt, "is_cold": out.get("is_cold"),
                         "import_s": import_s, "function_s": function_s, **m})
            if verbose:
                par = f" [parallel group {s['group']}]" if len(batch) > 1 else ""
                print(f"{s.get('id', i + k + 1):2}. {dt:5.2f}s  TOOL {s['tool']:9} cold={str(out.get('is_cold')):5} "
                      f"function={function_s:.2f}s import={import_s:.2f}s{par}")
        i += len(batch)

    total = time.perf_counter() - start
    llm = sum(r["wall_s"] for r in rows if r["type"] == "llm")
    tool = total - llm
    print(f"rep {rep}: total {total:.2f}s | LLM (replayed) {llm:.2f}s | tools {tool:.2f}s ({tool / total:.0%})")
    return rows, {"rep": rep, "total_s": total, "llm_s": llm, "tool_s": tool}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("endpoints")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--latency-scale", type=float, default=1.0)
    parser.add_argument("--out")
    args = parser.parse_args()

    trace = json.load(open(args.trace))
    tools = load_deployment(args.endpoints)
    print(f"task: {trace['task']}\n")
    steps, runs = [], []
    for rep in range(args.repeat):
        rows, summary = replay_once(trace, tools, args.latency_scale, rep, verbose=(rep == 0))
        steps += rows
        runs.append(summary)
    if args.out:
        json.dump({"trace_id": trace["trace_id"], "latency_scale": args.latency_scale,
                   "runs": runs, "steps": steps}, open(args.out, "w"), indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()

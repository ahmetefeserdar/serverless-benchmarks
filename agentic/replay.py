"""Replay an agent trace against SeBS functions (prototype).

llm steps  -> sleep for the recorded latency (stands in for the model)
tool steps -> HTTP call to the deployed function, timed
"""
import concurrent.futures
import json
import sys
import time
import urllib.request


def load_endpoints(path):
    # tool name -> function URL, taken from `sebs local start` output
    deployed = json.load(open(path))
    return {f["benchmark"].split(".", 1)[1]: "http://" + f["url"] for f in deployed["functions"]}


def call(url, args):
    req = urllib.request.Request(url, data=json.dumps(args).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def replay(trace_path, endpoints_path):
    trace = json.load(open(trace_path))
    endpoints = load_endpoints(endpoints_path)
    print(f"task: {trace['task']}\n")
    timeline = []
    start = time.perf_counter()
    steps = trace["steps"]
    i = 0
    while i < len(steps):
        step = steps[i]
        t0 = time.perf_counter()
        if step["type"] == "llm":
            time.sleep(step["latency_s"])
            dt = time.perf_counter() - t0
            timeline.append(("llm", dt))
            print(f"{step.get('id', i + 1):2}. {dt:5.2f}s  LLM  think")
            i += 1
            continue
        # Consecutive tool steps with the same group run in parallel.
        batch = [step]
        while "group" in step and i + len(batch) < len(steps) and steps[i + len(batch)].get("group") == step["group"]:
            batch.append(steps[i + len(batch)])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            outs = list(pool.map(lambda s: call(endpoints[s["tool"]], s["args"]), batch))
        dt = time.perf_counter() - t0
        timeline.append(("tool", dt))
        for s, out in zip(batch, outs):
            m = out["result"]["measurement"]
            par = f" [parallel group {s['group']}]" if len(batch) > 1 else ""
            print(f"{s.get('id', i + 1):2}. {dt:5.2f}s  TOOL {s['tool']:9} cold={str(out['is_cold']):5} "
                  f"fetch={m['fetch_time']/1e6:.2f}s convert={m['compute_time']/1e6:.2f}s{par}")
        i += len(batch)
    total = time.perf_counter() - start
    llm = sum(d for t, d in timeline if t == "llm")
    print(f"\ntotal {total:.2f}s | LLM (replayed) {llm:.2f}s | tools {total - llm:.2f}s ({(total - llm) / total:.0%})")


if __name__ == "__main__":
    replay(sys.argv[1], sys.argv[2])

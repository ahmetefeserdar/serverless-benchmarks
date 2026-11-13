"""Run a multi-step agent pipeline from a trace and split its time.

Like replay.py, but results flow between steps:
  - a tool step with "inputs_from": [ids] receives the results of those
    earlier steps as args["inputs"]
  - steps of the same "group" run in parallel

For every tool call the time is split into
  work      time the function spent on its actual job
  storage   time spent uploading/downloading results to object storage
  overhead  everything else in the round trip: HTTP call, JSON encoding,
            and (in inline mode) carrying the data inside the request/response

Usage:
  python agentic/pipeline.py TRACE OUT_BENCHMARK.json [MORE_OUT.json ...]
         [--mode storage|inline] [--size BYTES] [--llm-scale 1.0] [--output results.json]
"""
import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

STORAGE_FIELDS = ("upload_time", "download_time")


def load_deployments(paths):
    # tool name -> function URL; also remember the storage bucket SeBS prepared
    endpoints, bucket = {}, None
    for path in paths:
        deployed = json.load(open(path))
        for f in deployed["functions"]:
            endpoints[f["benchmark"].split(".", 1)[1]] = "http://" + f["url"]
        for inp in deployed.get("inputs", []):
            if isinstance(inp, dict) and "bucket" in inp:
                bucket = bucket or inp["bucket"]
    return endpoints, bucket


def call(url, args):
    body = json.dumps(args).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        hint = " (request too big for the platform: use --mode storage or a smaller --size)" if e.code == 413 else ""
        raise SystemExit(f"\nTool call failed: HTTP {e.code} {e.reason}, request was {len(body)} bytes{hint}")
    return out, time.perf_counter() - t0, len(body)


def split_time(out, round_trip):
    m = out["result"].get("measurement", {})
    storage = sum(m.get(k, 0) for k in STORAGE_FIELDS) / 1e6
    work = sum(v for k, v in m.items() if k.endswith("_time") and k not in STORAGE_FIELDS) / 1e6
    return work, storage, max(round_trip - work - storage, 0.0)


def prepare_args(step, results, bucket, opts):
    args = dict(step.get("args", {}))
    if step["tool"] == "pipeline":
        if bucket and "bucket" not in args:
            args["bucket"] = bucket
        if args.get("step", "produce") == "produce":
            if opts.mode:
                args["mode"] = opts.mode
            if opts.size:
                args["size_bytes"] = opts.size
    if "inputs_from" in step:
        args["inputs"] = [results[i] for i in step["inputs_from"]]
    return args


def run_tool(step, endpoints, results, bucket, opts):
    args = prepare_args(step, results, bucket, opts)
    out, round_trip, request_bytes = call(endpoints[step["tool"]], args)
    work, storage, overhead = split_time(out, round_trip)
    return {
        "id": step.get("id"), "type": "tool", "tool": step["tool"],
        "step": args.get("step"), "is_cold": out.get("is_cold"),
        "total_s": round_trip, "work_s": work, "storage_s": storage, "overhead_s": overhead,
        "request_bytes": request_bytes,
        "result": out["result"]["result"],
    }


def group_steps(steps):
    # consecutive tool steps with the same "group" form one parallel batch
    batches = []
    for step in steps:
        g = step.get("group")
        if g is not None and batches and batches[-1][0].get("group") == g and step["type"] == "tool":
            batches[-1].append(step)
        else:
            batches.append([step])
    return batches


def run(opts):
    trace = json.load(open(opts.trace))
    endpoints, bucket = load_deployments(opts.deployments)
    results, timeline = {}, []
    print(f"task: {trace['task']}  (mode={opts.mode or 'from trace'})\n")

    start = time.perf_counter()
    for batch in group_steps(trace["steps"]):
        t0 = time.perf_counter()
        if batch[0]["type"] == "llm":
            wait = batch[0]["latency_s"] * opts.llm_scale
            time.sleep(wait)
            rows = [{"id": batch[0].get("id"), "type": "llm", "total_s": wait}]
        else:
            with ThreadPoolExecutor(len(batch)) as pool:
                rows = list(pool.map(lambda s: run_tool(s, endpoints, results, bucket, opts), batch))
        for row in rows:
            if row["id"] is not None and row["type"] == "tool":
                results[row["id"]] = row["result"]
            timeline.append(row)
            if row["type"] == "llm":
                print(f"{row['id']}. LLM  wait {row['total_s']:6.3f}s")
            else:
                name = f"{row['tool']}/{row['step']}" if row["step"] else row["tool"]
                print(f"{row['id']}. TOOL {name:18} cold={str(row['is_cold']):5} total={row['total_s']:.3f}s "
                      f"work={row['work_s']:.3f}s storage={row['storage_s']:.3f}s overhead={row['overhead_s']:.3f}s")
        if len(batch) > 1:
            print(f"   (parallel group took {time.perf_counter() - t0:.3f}s)")
    total = time.perf_counter() - start

    tools = [r for r in timeline if r["type"] == "tool"]
    summary = {
        "total_s": total,
        "llm_s": sum(r["total_s"] for r in timeline if r["type"] == "llm"),
        "work_s": sum(r["work_s"] for r in tools),
        "storage_s": sum(r["storage_s"] for r in tools),
        "overhead_s": sum(r["overhead_s"] for r in tools),
    }
    tool_total = summary["work_s"] + summary["storage_s"] + summary["overhead_s"]
    print(f"\ntotal {total:.3f}s | LLM {summary['llm_s']:.3f}s | tools {tool_total:.3f}s")
    if tool_total > 0:
        handoff = summary["storage_s"] + summary["overhead_s"]
        print(f"inside tools: work {summary['work_s']:.3f}s, storage {summary['storage_s']:.3f}s, "
              f"overhead {summary['overhead_s']:.3f}s -> hand-off is {handoff / tool_total:.0%} of tool time")

    if opts.output:
        for r in tools:
            r.pop("result", None)
        json.dump({"trace_id": trace["trace_id"], "mode": opts.mode, "size": opts.size,
                   "summary": summary, "steps": timeline}, open(opts.output, "w"), indent=2)
        print(f"saved {opts.output}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("deployments", nargs="+", help="out_benchmark.json file(s) from `sebs local start`")
    p.add_argument("--mode", choices=["storage", "inline"], help="override hand-off mode of produce steps")
    p.add_argument("--size", type=int, help="override size_bytes of produce steps")
    p.add_argument("--llm-scale", type=float, default=1.0, help="multiply LLM waits (0 = skip them)")
    p.add_argument("--output", help="write per-step results as JSON")
    run(p.parse_args())

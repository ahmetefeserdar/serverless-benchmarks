"""
720.code-exec (PROTOTYPE): replay a recorded coding-agent trace.

- "llm" steps: no model is called. We sleep latency_ms * llm_scale to mimic waiting for
  the LLM API, and use the recorded response.
- "tool" steps: executed for real on a repository downloaded from object storage.
  Each result is compared with what happened during the recording.
"""
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time

from . import storage

client = storage.storage.get_instance()


# ---------- tools the agent can call ----------

def tool_run_tests(repo, args):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-q"],
        cwd=repo, capture_output=True, text=True, timeout=100,
    )
    last = (proc.stderr.strip().splitlines() or [""])[-1]
    return {"exit_code": proc.returncode}, f"exit={proc.returncode} {last}"


def tool_grep(repo, args):
    pattern = re.compile(args["pattern"])
    files = []
    for name in sorted(os.listdir(repo)):
        if name.endswith(".py"):
            with open(os.path.join(repo, name)) as f:
                if pattern.search(f.read()):
                    files.append(name)
    return {"files": files}, f"{len(files)} file(s): {files[:5]}"


def tool_read_file(repo, args):
    with open(os.path.join(repo, args["path"])) as f:
        content = f.read()
    return {"content": content}, f"{len(content)} bytes"


def tool_apply_patch(repo, args):
    path = os.path.join(repo, args["path"])
    with open(path) as f:
        content = f.read()
    applied = args["old"] in content
    if applied:
        with open(path, "w") as f:
            f.write(content.replace(args["old"], args["new"], 1))
    return {"applied": applied}, f"applied={applied}"


TOOLS = {
    "run_tests": tool_run_tests,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "apply_patch": tool_apply_patch,
}


def matches(expect, got):
    if "contains" in expect:
        return expect["contains"] in got.get("content", "")
    return all(got.get(k) == v for k, v in expect.items())


# ---------- handler ----------

def handler(event):
    bucket = event["bucket"]["bucket"]
    key = os.path.join(event["bucket"]["input"], event["repo"])
    scale = float(event.get("llm_scale", 0.0))

    t0 = time.perf_counter()
    archive = client.download_stream(bucket, key)
    workdir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(workdir)
    repo = os.path.join(workdir, "repo")
    setup_ms = (time.perf_counter() - t0) * 1000

    steps, llm_wait_ms, tool_ms = [], 0.0, {}
    tests_passed = False
    for rec in event["trace"]:
        s = time.perf_counter()
        if rec["type"] == "llm":
            time.sleep(rec["latency_ms"] * scale / 1000)
            ms = (time.perf_counter() - s) * 1000
            llm_wait_ms += ms
            steps.append({"type": "llm", "ms": round(ms, 2)})
        else:
            got, summary = TOOLS[rec["tool"]](repo, rec["args"])
            ms = (time.perf_counter() - s) * 1000
            tool_ms[rec["tool"]] = tool_ms.get(rec["tool"], 0.0) + ms
            ok = matches(rec.get("expect", {}), got)
            if rec["tool"] == "run_tests":
                tests_passed = got["exit_code"] == 0
            steps.append({"type": "tool", "tool": rec["tool"], "ms": round(ms, 2),
                          "matches_recording": ok, "summary": summary})

    return {
        "result": {"steps": steps, "final_tests_passed": tests_passed},
        "measurement": {
            "setup_ms": round(setup_ms, 2),
            "archive_bytes": len(archive),
            "llm_wait_ms": round(llm_wait_ms, 2),
            "tool_ms": {k: round(v, 2) for k, v in tool_ms.items()},
        },
    }

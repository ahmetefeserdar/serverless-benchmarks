"""
720.code-exec (PROTOTYPE): replay a recorded coding-agent trace.

- "llm" steps: no model is called. We sleep latency_s * llm_scale to mimic waiting for
  the LLM API.
- "tool" steps: executed for real on a workspace (pinned repo + pinned tool wheels)
  downloaded from object storage, and compared with what happened during the recording.

Per tool step we record wall time, CPU time of the tool's processes, and peak memory;
at the end, disk used by the workspace and the installed dependencies.

Memory: getrusage(RUSAGE_CHILDREN).ru_maxrss is NOT used for tools, because on Linux a
child's counter starts at the parent's RSS (it is inherited across fork/exec), so any tool
smaller than the function process reports the function's size. Instead we sample the tool
process's own high-water mark (VmHWM in /proc/<pid>/status), which is reset at exec.
"""
import io
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


def _vm_hwm_kb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None

from . import storage

client = storage.storage.get_instance()


def _du(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class Workspace:
    def __init__(self, root, src):
        self.root = root
        self.repo = os.path.join(root, "repo")
        self.deps = os.path.join(root, "deps")
        self.src = os.path.join(self.repo, src)
        self.last_peak_kb = None  # peak RSS of the most recent tool process

    def run(self, cmd, timeout=300, sample_s=0.005):
        env = dict(os.environ, PYTHONPATH=f"{self.deps}:{self.src}", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen(cmd, cwd=self.repo, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        peak = [None]

        def sample():
            while proc.poll() is None:
                v = _vm_hwm_kb(proc.pid)
                if v is not None:
                    peak[0] = v if peak[0] is None else max(peak[0], v)
                time.sleep(sample_s)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        sampler.join()
        self.last_peak_kb = peak[0]
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------- tools the agent can call ----------

def tool_install_deps(ws, args):
    wheels = sorted(os.path.join(ws.root, "wheels", w) for w in os.listdir(os.path.join(ws.root, "wheels")))
    proc = ws.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-index", "--no-deps",
                   "--disable-pip-version-check", "--target", ws.deps, *wheels])
    return {"exit_code": proc.returncode}, f"exit={proc.returncode} {len(wheels)} wheels"


def tool_run_tests(ws, args):
    proc = ws.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|error)", last)}
    got = {"exit_code": proc.returncode, "passed": counts.get("passed", 0),
           "failed": counts.get("failed", 0) + counts.get("error", 0)}
    return got, last


def tool_grep(ws, args):
    pattern = re.compile(args["pattern"])
    files = []
    for root, _, names in os.walk(ws.repo):
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, errors="replace") as f:
                    if pattern.search(f.read()):
                        files.append(os.path.relpath(path, ws.repo))
    files.sort()
    return {"files": files}, f"{len(files)} file(s): {files[:3]}"


def tool_read_file(ws, args):
    with open(os.path.join(ws.repo, args["path"])) as f:
        content = f.read()
    return {"content": content}, f"{len(content)} bytes"


def tool_apply_patch(ws, args):
    path = os.path.join(ws.repo, args["path"])
    with open(path) as f:
        content = f.read()
    applied = content.count(args["old"]) == 1
    if applied:
        with open(path, "w") as f:
            f.write(content.replace(args["old"], args["new"]))
    return {"applied": applied}, f"applied={applied}"


def tool_lint(ws, args):
    proc = ws.run([sys.executable, "-m", "ruff", "check", ".", "--no-cache", "--exit-zero",
                   "--output-format", "json"])
    try:
        issues = len(json.loads(proc.stdout or "[]"))
    except json.JSONDecodeError:
        issues = -1
    return {"issues": issues, "exit_code": proc.returncode}, f"{issues} issue(s)"


TOOLS = {
    "install_deps": tool_install_deps,
    "run_tests": tool_run_tests,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "apply_patch": tool_apply_patch,
    "lint": tool_lint,
}


def matches(expect, got):
    for k, v in expect.items():
        if k == "contains":
            if v not in got.get("content", ""):
                return False
        elif k == "contains_file":
            if v not in got.get("files", []):
                return False
        elif got.get(k) != v:
            return False
    return True


def _cpu_ms(before, after):
    return round(((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)) * 1000, 2)


# ---------- handler ----------

def handler(event):
    bucket = event["bucket"]["bucket"]
    key = os.path.join(event["bucket"]["input"], event["workspace"])
    scale = float(event.get("llm_scale", 0.0))

    t0 = time.perf_counter()
    archive = client.download_stream(bucket, key)
    download_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    tmp = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(tmp)
    ws = Workspace(os.path.join(tmp, "workspace"), event.get("src", "."))
    extract_ms = (time.perf_counter() - t1) * 1000
    workspace_bytes = _du(ws.repo)

    steps, llm_wait_ms, tools = [], 0.0, {}
    tests_passed = False
    try:
        for rec in event["trace"]:
            s = time.perf_counter()
            if rec["type"] == "llm":
                time.sleep(rec["latency_s"] * scale)
                ms = (time.perf_counter() - s) * 1000
                llm_wait_ms += ms
                steps.append({"type": "llm", "ms": round(ms, 2)})
                continue

            self_before = resource.getrusage(resource.RUSAGE_SELF)
            child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            ws.last_peak_kb = None
            got, summary = TOOLS[rec["tool"]](ws, rec["args"])
            peak_kb = ws.last_peak_kb  # None for in-process tools (grep, read, patch)
            ms = (time.perf_counter() - s) * 1000
            self_after = resource.getrusage(resource.RUSAGE_SELF)
            child_after = resource.getrusage(resource.RUSAGE_CHILDREN)

            ok = matches(rec.get("expect", {}), got)
            if rec["tool"] == "run_tests":
                tests_passed = got["exit_code"] == 0
            cpu_ms = _cpu_ms(self_before, self_after) + _cpu_ms(child_before, child_after)
            agg = tools.setdefault(rec["tool"], {"calls": 0, "wall_ms": 0.0, "cpu_ms": 0.0,
                                                 "peak_rss_kb": None})
            agg["calls"] += 1
            agg["wall_ms"] = round(agg["wall_ms"] + ms, 2)
            agg["cpu_ms"] = round(agg["cpu_ms"] + cpu_ms, 2)
            if peak_kb is not None:
                agg["peak_rss_kb"] = max(agg["peak_rss_kb"] or 0, peak_kb)
            steps.append({"type": "tool", "tool": rec["tool"], "ms": round(ms, 2),
                          "cpu_ms": cpu_ms, "peak_rss_kb": peak_kb,
                          "matches_recording": ok, "summary": summary})

        measurement = {
            "download_ms": round(download_ms, 2),
            "extract_ms": round(extract_ms, 2),
            "archive_bytes": len(archive),
            "repo_bytes": workspace_bytes,
            "deps_bytes": _du(ws.deps),
            "llm_wait_ms": round(llm_wait_ms, 2),
            "tools": tools,
            # KiB. Function process: high-water mark over its lifetime (includes earlier
            # warm invocations). Tools: largest sampled VmHWM of any tool process this call.
            "peak_rss_function_kb": _vm_hwm_kb("self"),
            "peak_rss_tools_kb": max((t["peak_rss_kb"] or 0 for t in tools.values()), default=0),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "result": {"repo": event.get("repo"), "steps": steps, "final_tests_passed": tests_passed},
        "measurement": measurement,
    }

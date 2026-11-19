"""
720.code-exec (PROTOTYPE): replay a recorded coding-agent trace.

- "llm" steps: no model is called. We sleep latency_s * llm_scale to mimic waiting for
  the LLM API.
- "tool" steps: executed for real on a workspace (pinned repo + pinned tool wheels)
  downloaded from object storage, and compared with what happened during the recording.

Per tool step we record wall time, CPU time of the tool's processes, and peak memory;
at the end, disk used by the workspace and the installed dependencies.

Two ways to replay a session:
- one invocation (default): the whole trace, LLM waits included, runs in one call on one
  workspace. Simple, but the function is billed while it waits for the LLM.
- per step: the client sends one tool step per call with "session": {"load": key, "save": mode}.
  The function starts from the saved workspace `load` (or the base workspace if absent), runs
  the step, and saves the workspace back to storage; the new key is returned in
  result.session.key for the next call. LLM waits happen on the client and are not billed,
  but every call pays for loading and saving the workspace.
  save = "always" (after every step), "changed" (only after tools that modify the
  workspace), or "never".
  state = "repo" (default: save only the repo; installed deps stay in a per-container cache in
  /tmp and are reinstalled from the base workspace's wheels on a cache miss) or "full" (save
  the installed deps too). "drop_deps_cache": true forces a miss, i.e. a new container.

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

def _pip_install(ws, wheels_dir, target):
    wheels = sorted(os.path.join(wheels_dir, w) for w in os.listdir(wheels_dir))
    return ws.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-index", "--no-deps",
                   "--disable-pip-version-check", "--target", target, *wheels])


def tool_install_deps(ws, args):
    wheels_dir = os.path.join(ws.root, "wheels")
    if not os.path.isdir(wheels_dir):
        # Loaded from a saved session: deps were installed earlier and the wheels dropped.
        return {"exit_code": 0}, "already installed (saved session)"
    proc = _pip_install(ws, wheels_dir, ws.deps)
    return {"exit_code": proc.returncode}, f"exit={proc.returncode} {len(os.listdir(wheels_dir))} wheels"


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

# Tools that change the workspace; with save="changed" only these trigger a save.
# (pytest runs with no cache provider and no bytecode files, ruff with --no-cache.)
MODIFIES_WORKSPACE = {"install_deps", "apply_patch"}


DEPS_CACHE_ROOT = "/tmp/code-exec-deps"


def _deps_installed(ws):
    return os.path.isdir(ws.deps) and bool(os.listdir(ws.deps))


def _pack(ws, state):
    """Workspace -> tar.gz bytes.

    state="full": repo + installed deps (wheels dropped once installed).
    state="repo": only the repo (and the wheels if deps are not installed yet); the next call
    gets the deps from the container's cache or reinstalls them (see _restore_deps).
    """
    buf = io.BytesIO()
    installed = _deps_installed(ws)
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        for name in sorted(os.listdir(ws.root)):
            if name == "wheels" and installed:
                continue
            if name == "deps" and state == "repo":
                continue
            tar.add(os.path.join(ws.root, name), arcname=os.path.join("workspace", name))
        meta = json.dumps({"deps_installed": installed, "deps_in_state": state == "full"}).encode()
        info = tarfile.TarInfo("workspace/state.json")
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
    buf.seek(0)
    return buf


def _restore_deps(ws, bucket, base_key, cache, drop_cache):
    """Point ws.deps at installed deps for a repo-only state. Returns (cache status, ms)."""
    if drop_cache:
        shutil.rmtree(cache, ignore_errors=True)
    if os.path.isdir(cache) and os.listdir(cache):
        ws.deps = cache
        return "hit", 0.0
    # Miss (new container): reinstall from the wheels of the base workspace.
    t = time.perf_counter()
    base = tempfile.mkdtemp()
    try:
        data = client.download_stream(bucket, base_key)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(base, members=[m for m in tar.getmembers()
                                          if m.name.startswith("workspace/wheels")])
        staging = cache + ".partial"
        shutil.rmtree(staging, ignore_errors=True)
        proc = _pip_install(ws, os.path.join(base, "workspace", "wheels"), staging)
        if proc.returncode != 0:
            raise RuntimeError(f"restoring deps failed: {proc.stderr[-500:]}")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        os.rename(staging, cache)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    ws.deps = cache
    return "miss", (time.perf_counter() - t) * 1000


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
    session = event.get("session")  # None = whole trace in this one invocation
    save_mode = (session or {}).get("save", "always") if session else "never"
    if save_mode not in ("always", "changed", "never"):
        raise ValueError(f"session.save must be always/changed/never, got {save_mode!r}")
    state_kind = (session or {}).get("state", "repo")
    if state_kind not in ("repo", "full"):
        raise ValueError(f"session.state must be repo/full, got {state_kind!r}")
    base_key = os.path.join(event["bucket"]["input"], event["workspace"])
    loaded = bool(session and session.get("load"))
    key = session["load"] if loaded else base_key  # full key, as returned by the previous call
    deps_cache = os.path.join(DEPS_CACHE_ROOT, event["workspace"].replace(".tar.gz", ""))
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
    tests_passed = None  # stays None if this call ran no tests (per-step mode)
    changed = False
    cache_status, deps_restore_ms = None, 0.0
    try:
        state_file = os.path.join(ws.root, "state.json")
        if loaded and os.path.exists(state_file):
            with open(state_file) as f:
                saved = json.load(f)
            os.remove(state_file)
            if saved.get("deps_installed") and not saved.get("deps_in_state"):
                cache_status, deps_restore_ms = _restore_deps(
                    ws, bucket, base_key, deps_cache, bool(session.get("drop_deps_cache")))

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
            changed = changed or rec["tool"] in MODIFIES_WORKSPACE
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

        # Save the workspace for the next per-step call.
        session_out, save_ms, upload_ms, state_bytes = None, 0.0, 0.0, 0
        if session is not None and state_kind == "repo" and _deps_installed(ws) \
                and ws.deps != deps_cache:
            # Deps were installed in this call: keep them in the container for later calls.
            if session.get("drop_deps_cache"):
                shutil.rmtree(deps_cache, ignore_errors=True)
            if not os.path.isdir(deps_cache):
                os.makedirs(DEPS_CACHE_ROOT, exist_ok=True)
                os.rename(ws.deps, deps_cache)  # same filesystem (/tmp): instant
                ws.deps = deps_cache
                cache_status = cache_status or "stored"
        if session is not None:
            if save_mode == "always" or (save_mode == "changed" and changed):
                t2 = time.perf_counter()
                packed = _pack(ws, state_kind)
                save_ms = (time.perf_counter() - t2) * 1000
                state_bytes = packed.getbuffer().nbytes
                t3 = time.perf_counter()
                name = os.path.join(event["bucket"]["output"], f"session-{session.get('id', 'ws')}.tar.gz")
                new_key = client.upload_stream(bucket, name, packed)
                upload_ms = (time.perf_counter() - t3) * 1000
                session_out = {"key": new_key, "saved": True}
            else:
                session_out = {"key": key if session.get("load") else None, "saved": False}

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
        if session is not None:
            # download_ms/extract_ms above are the cost of loading the session state.
            measurement.update({"save_ms": round(save_ms, 2), "upload_ms": round(upload_ms, 2),
                                "state_bytes": state_bytes, "state": state_kind,
                                "deps_restore_ms": round(deps_restore_ms, 2),
                                "deps_cache": cache_status})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = {"repo": event.get("repo"), "steps": steps, "final_tests_passed": tests_passed}
    if session_out is not None:
        result["session"] = session_out
    return {"result": result, "measurement": measurement}

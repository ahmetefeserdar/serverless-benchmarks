"""
720.code-exec: replay a recorded coding-agent session (PROTOTYPE).

An agent is asked "the tests fail, find the bug and fix it". Its session is a *trace*:
LLM turns (recorded latency) interleaved with tool calls. No LLM is called during the
benchmark: LLM turns become waits of latency_s * llm_scale, and tool calls run for real
inside the function on a workspace fetched from object storage.

Reproducibility:
- Real repositories are pinned to a release archive and verified by SHA-256.
- Test/lint tools (pytest, ruff) are pinned wheels, verified by SHA-256 and shipped inside
  the workspace archive, so the function installs them offline (no PyPI at runtime).
- A known one-line bug is injected so the agent's "find and fix" story is deterministic.
Downloads happen once, at input generation, and are cached in ~/.cache/sebs-code-exec.

The traces are hand-written (source: synthetic) until we record real ones.
"""
import hashlib
import io
import os
import tarfile
import tempfile
import urllib.request

CACHE = os.path.expanduser("~/.cache/sebs-code-exec")

WHEELS = [
    ("pytest-8.3.3-py3-none-any.whl",
     "a6853c7375b2663155079443d2e45de913a911a11d669df02a50814944db57b2",
     "https://files.pythonhosted.org/packages/6b/77/7440a06a8ead44c7757a64362dd22df5760f9b12dc5f11b6188cd2fc27a0/pytest-8.3.3-py3-none-any.whl"),
    ("iniconfig-2.0.0-py3-none-any.whl",
     "b6a85871a79d2e3b22d2d1b94ac2824226a63c6b741c88f7ae975f18b6778374",
     "https://files.pythonhosted.org/packages/ef/a6/62565a6e1cf69e10f5727360368e451d4b7f58beeac6173dc9db836a5b46/iniconfig-2.0.0-py3-none-any.whl"),
    ("packaging-24.1-py3-none-any.whl",
     "5b8f2217dbdbd2f7f384c41c628544e6d52f2d0f53c6d0c3ea61aa5d1d7ff124",
     "https://files.pythonhosted.org/packages/08/aa/cc0199a5f0ad350994d660967a8efb233fe0416e4639146c089643407ce6/packaging-24.1-py3-none-any.whl"),
    ("pluggy-1.5.0-py3-none-any.whl",
     "44e1ad92c8ca002de6377e165f3e0f1be63266ab4d554740532335b9d75ea669",
     "https://files.pythonhosted.org/packages/88/5f/e351af9a41f866ac3f1fac4ca0613908d9a41741cfcf2228f4ad853b697d/pluggy-1.5.0-py3-none-any.whl"),
    ("ruff-0.6.9-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
     "a67267654edc23c97335586774790cde402fb6bbdb3c2314f1fc087dee320bfa",
     "https://files.pythonhosted.org/packages/a7/86/96f4252f41840e325b3fa6c48297e661abb9f564bd7dcc0572398c8daa42/ruff-0.6.9-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"),
]

# The bug is injected into the archive; the agent's patch reverts it (old/new swapped).
# SeBS sizes are fixed to test/small/large; ordered by repository size.
REPOS = {
    "test": {
        "name": "tomli-2.0.1",
        "url": "https://github.com/hukkin/tomli/archive/refs/tags/2.0.1.tar.gz",
        "sha256": "ad22dbc128623e0c156ffaff019f29f456eba8a5d5a05164dd34f63e560449df",
        "src": "src",
        "bug": {"path": "src/tomli/_parser.py",
                "good": "return pos + 4, True", "bad": "return pos + 4, False"},
        "grep": "return pos \\+ 4,", "grep_hit": "src/tomli/_parser.py",
    },
    "small": {
        "name": "more-itertools-10.5.0",
        "url": "https://github.com/more-itertools/more-itertools/archive/refs/tags/v10.5.0.tar.gz",
        "sha256": "1072e3a5e25f19a6c5759227b3ce69fb2314592cbecec5916e69464e3cb811d3",
        "src": ".",
        "bug": {"path": "more_itertools/recipes.py",
                "good": "return list(islice(iterable, n))",
                "bad": "return list(islice(iterable, n + 1))"},
        "grep": "def take\\(", "grep_hit": "more_itertools/recipes.py",
    },
    "large": {
        "name": "click-8.1.7",
        "url": "https://github.com/pallets/click/archive/refs/tags/8.1.7.tar.gz",
        "sha256": "89251974dba8552b4e22990ca34adfb93a47ba7deb27fe7358a6661a09ca8793",
        "src": "src",
        "bug": {"path": "src/click/types.py",
                "good": '{"1", "true", "t", "yes", "y", "on"}',
                "bad": '{"1", "true", "t", "y", "on"}'},
        "grep": "class BoolParamType", "grep_hit": "src/click/types.py",
    },
}

# size -> scale applied to recorded LLM latency (0 = measure tool cost only)
LLM_SCALE = {"test": 0.0, "small": 0.5, "large": 1.0}

# Expected results from the reference run (SeBS python 3.11 image). Lint counts use ruff's
# default rules on the repo as shipped.
EXPECT = {
    "test":  {"failed_before": 1, "passed_after": 13, "lint_issues": 0},
    "small": {"failed_before": 29, "passed_after": 663, "lint_issues": 76},
    "large": {"failed_before": 1, "passed_after": 589, "lint_issues": 0},
}


def buckets_count():
    return (1, 1)


def _fetch(url, sha256, name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        with urllib.request.urlopen(url, timeout=120) as r, open(path + ".part", "wb") as f:
            f.write(r.read())
        os.replace(path + ".part", path)
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != sha256:
        os.remove(path)
        raise RuntimeError(f"Checksum mismatch for {name}: got {digest}, pinned {sha256}")
    return path


def _replace_once(path, old, new):
    content = open(path).read()
    if content.count(old) != 1:
        raise RuntimeError(f"Expected exactly one '{old}' in {path}, found {content.count(old)}")
    with open(path, "w") as f:
        f.write(content.replace(old, new))


def _build_workspace(size, tmp):
    ws = os.path.join(tmp, "workspace")
    repo = os.path.join(ws, "repo")
    os.makedirs(os.path.join(ws, "wheels"))
    spec = REPOS[size]
    archive = _fetch(spec["url"], spec["sha256"], spec["name"] + ".tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        top = tar.getmembers()[0].name.split("/")[0]
        tar.extractall(tmp)
    os.rename(os.path.join(tmp, top), repo)
    _replace_once(os.path.join(repo, spec["bug"]["path"]), spec["bug"]["good"], spec["bug"]["bad"])
    for name, sha, url in WHEELS:
        src = _fetch(url, sha, name)
        with open(src, "rb") as fi, open(os.path.join(ws, "wheels", name), "wb") as fo:
            fo.write(fi.read())
    return ws, spec


def _trace(spec, expect):
    """The agent's session. Hand-written for now; latencies are typical LLM API times."""
    bug = spec["bug"]
    return [
        {"type": "llm", "latency_s": 0.82, "input_tokens": 420, "output_tokens": 30},
        {"type": "tool", "tool": "install_deps", "args": {}, "expect": {"exit_code": 0}},
        {"type": "tool", "tool": "run_tests", "args": {},
         "expect": {"exit_code": 1, "failed": expect["failed_before"]}},
        {"type": "llm", "latency_s": 0.64, "input_tokens": 1900, "output_tokens": 25},
        {"type": "tool", "tool": "grep", "args": {"pattern": spec["grep"]},
         "expect": {"contains_file": spec["grep_hit"]}},
        {"type": "tool", "tool": "read_file", "args": {"path": bug["path"]},
         "expect": {"contains": bug["bad"]}},
        {"type": "llm", "latency_s": 0.91, "input_tokens": 4200, "output_tokens": 60},
        {"type": "tool", "tool": "apply_patch",
         "args": {"path": bug["path"], "old": bug["bad"], "new": bug["good"]},
         "expect": {"applied": True}},
        {"type": "tool", "tool": "run_tests", "args": {},
         "expect": {"exit_code": 0, "failed": 0, "passed": expect["passed_after"]}},
        {"type": "tool", "tool": "lint", "args": {},
         "expect": {"issues": expect["lint_issues"]}},
        {"type": "llm", "latency_s": 0.30, "input_tokens": 4600, "output_tokens": 15},
    ]


def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):
    with tempfile.TemporaryDirectory() as tmp:
        ws, spec = _build_workspace(size, tmp)
        archive = os.path.join(tmp, f"workspace-{size}.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(ws, arcname="workspace")
        upload_func(0, f"workspace-{size}.tar.gz", archive)

    return {
        "bucket": {"bucket": benchmarks_bucket, "input": input_paths[0], "output": output_paths[0]},
        "workspace": f"workspace-{size}.tar.gz",
        "repo": spec["name"],
        "src": spec["src"],
        "trace": _trace(spec, EXPECT[size]),
        "llm_scale": LLM_SCALE[size],
        "size": size,
    }


def validate_output(data_dir, input_config: dict, output: dict, language: str, storage=None):
    result = output.get("result", {})
    steps = result.get("steps", [])
    trace = input_config["trace"]

    if len(steps) != len(trace):
        return f"Replayed {len(steps)} steps, trace has {len(trace)}"
    for i, (step, rec) in enumerate(zip(steps, trace)):
        if step.get("type") != rec["type"]:
            return f"Step {i}: type {step.get('type')} != recorded {rec['type']}"
        if rec["type"] == "tool" and not step.get("matches_recording"):
            return f"Step {i} ({rec['tool']}) diverged from the recording: {step.get('summary')}"
    if not result.get("final_tests_passed"):
        return "Tests still fail after replaying the agent's fix"
    return None

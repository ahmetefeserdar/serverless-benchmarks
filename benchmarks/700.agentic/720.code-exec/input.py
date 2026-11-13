"""
720.code-exec: replay a recorded coding-agent session (PROTOTYPE).

An agent was asked "the tests fail, fix the bug". Its session is stored as a *trace*:
LLM turns (what the model said + how long the API took) interleaved with tool calls.
The benchmark does NOT call an LLM. LLM turns become a sleep of latency_ms * llm_scale;
tool calls really execute inside the function, on a repository fetched from storage.

The trace below is hand-written for the prototype. Real traces would be recorded once
with an actual agent (e.g. using Marcin's Muse Spark credits) and stored in benchmarks-data.
"""
import os
import tarfile
import tempfile

BUGGY_CALC = '''\
def add(a, b):
    return a - b   # BUG: should be a + b


def mul(a, b):
    return a * b
'''

TEST_CALC = '''\
import unittest
from calc import add, mul


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mul(self):
        self.assertEqual(mul(2, 3), 6)
'''

# Extra modules make the repo (and therefore grep and the test suite) bigger for larger sizes.
EXTRA_MODULE = '''\
def f_{i}(x):
    return sum(k * k for k in range(x)) + {i}
'''
EXTRA_TEST = '''\
import unittest
from mod_{i} import f_{i}


class TestMod{i}(unittest.TestCase):
    def test_f(self):
        self.assertEqual(f_{i}(2000), sum(k * k for k in range(2000)) + {i})
'''

# size -> (extra modules in the repo, scale applied to recorded LLM latency)
SIZES = {
    "test": (0, 0.0),     # correctness only, no LLM waiting
    "small": (50, 0.25),  # some waiting, small repo
    "large": (400, 1.0),  # full recorded LLM latency, big repo
}

TRACE = [
    {"type": "llm", "latency_ms": 820,
     "response": "The tests are failing. I'll run them first to see which one."},
    {"type": "tool", "tool": "run_tests", "args": {}, "expect": {"exit_code": 1}},
    {"type": "llm", "latency_ms": 640,
     "response": "test_add fails: add(2, 3) != 5. Let me find where add is defined."},
    {"type": "tool", "tool": "grep", "args": {"pattern": "def add"},
     "expect": {"files": ["calc.py"]}},
    {"type": "tool", "tool": "read_file", "args": {"path": "calc.py"},
     "expect": {"contains": "a - b"}},
    {"type": "llm", "latency_ms": 910,
     "response": "add() subtracts instead of adding. I'll patch it."},
    {"type": "tool", "tool": "apply_patch",
     "args": {"path": "calc.py", "old": "return a - b", "new": "return a + b"},
     "expect": {"applied": True}},
    {"type": "tool", "tool": "run_tests", "args": {}, "expect": {"exit_code": 0}},
    {"type": "llm", "latency_ms": 300, "response": "All tests pass. The bug is fixed."},
]


def buckets_count():
    return (1, 1)


def _build_repo(path: str, extra_modules: int):
    with open(os.path.join(path, "calc.py"), "w") as f:
        f.write(BUGGY_CALC)
    with open(os.path.join(path, "test_calc.py"), "w") as f:
        f.write(TEST_CALC)
    for i in range(extra_modules):
        with open(os.path.join(path, f"mod_{i}.py"), "w") as f:
            f.write(EXTRA_MODULE.format(i=i))
        with open(os.path.join(path, f"test_mod_{i}.py"), "w") as f:
            f.write(EXTRA_TEST.format(i=i))


def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):
    extra_modules, llm_scale = SIZES[size]
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        _build_repo(repo, extra_modules)
        archive = os.path.join(tmp, f"repo-{size}.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(repo, arcname="repo")
        upload_func(0, f"repo-{size}.tar.gz", archive)

    return {
        "bucket": {"bucket": benchmarks_bucket, "input": input_paths[0], "output": output_paths[0]},
        "repo": f"repo-{size}.tar.gz",
        "trace": TRACE,
        "llm_scale": llm_scale,
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

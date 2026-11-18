We simulate an AI agent whose tools run as serverless functions. One tool produces a result (e.g. a fetched website), and the next tool consumes it. Functions can’t share memory, so the result goes through a mailbox (storage). We measure how much of the tool time is real work vs. moving data between functions.

## Trace extension: `inputs_from`
 
A tool step can list earlier step ids in `"inputs_from": [2, 3]`. The runner
passes those steps' results to the function as `args["inputs"]`. This is the
proposed answer to the open question in `agentic/TRACE_FORMAT.md` (to be
agreed with the trace format owner).
 
```json
{"id": 4, "type": "tool", "tool": "pipeline", "inputs_from": [2],
 "args": {"step": "consume"}}
```
 
The field is optional, so traces without it still work. It covers chains
(2 → 4) and fan-in (2, 3, 4 → 6).
 
### How it works
 
1. **Remember results** (`agentic/pipeline.py`, `run`). After each tool step
   the runner stores its result by step id:
```python
   results[row["id"]] = row["result"]
```
   In storage mode the result is only a small label, not the data:
   `{"mode": "storage", "bucket": "...", "key": "produce.a1b2.txt", "size_bytes": 1048576, "sha256": "..."}`.
 
2. **Look them up** (`agentic/pipeline.py`, `prepare_args`). Before calling a
   step, the runner collects the listed results:
```python
   if "inputs_from" in step:
       args["inputs"] = [results[i] for i in step["inputs_from"]]
```
 
3. **Use them** (`python/function.py`, `consume`). The function loops over
   `inputs` and fetches each one according to how it was handed off:
```python
   if item["mode"] == "storage":
       data = client.download_stream(item["bucket"], item["key"])
   else:
       data = item["data"].encode()
```
   It then checks the SHA-256 against the one `produce` reported, so a
   corrupted or wrong object is detected.
 
```
step 2 produce ──► returns {"key": "produce.a1b2.txt", ...}
                        │
runner: results[2] = that
                        │
step 4 has inputs_from [2] → args["inputs"] = [results[2]]
                        │
step 4 consume ──► reads inputs[0]["key"] → downloads from storage
```
 
So only small labels travel through the runner; the data itself moves
through storage (or inline, when the mode is `inline`).


## First result
 
2-step trace, 1 MB result, storage mode, local (macOS):
work 0.093 s, storage 0.149 s, overhead 0.168 s
→ **77% of tool time is hand-off, 23% is real work.**
 
## Roadmap
 
- [x] **11-13** 2-step pipeline running locally (produce → storage → consume)
- [ ] **11-17** inline vs. storage comparison and fan-in trace on slides;
      agree on `inputs_from` with the trace format owner
- [ ] **Idea 1: which hand-off is fastest.** Add a third mode, `nosql`
      (ScyllaDB). Run all modes over sizes 10 KB → 10 MB with repetitions and
      plot hand-off time vs. size. Note the size limits of each mode (local
      ~100 KB inline, AWS Lambda 6 MB synchronous payload).
- [ ] **Idea 2: the waiting orchestrator.** A `boss` function that runs the
      whole trace itself: waits for the LLM steps and calls the tools. Measure
      how much of its billed time is idle waiting and convert it to cost
      (memory × seconds × price).
- [ ] **Real tools.** Replace `produce` with 710.web-fetch, the browser agent
      and code execution once they are ready.
- [ ] **December** cloud runs (AWS/GCP free tier) with the steps deployed as
      separate functions; compare with the cloud workflow engines
      (SeBS workflows branch: AWS Step Functions, GCP Workflows); open the PR.

 

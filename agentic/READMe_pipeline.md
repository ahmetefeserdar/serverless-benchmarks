We simulate an AI agent whose tools run as serverless functions. One tool produces a result (e.g. a fetched website), and the next tool consumes it. Functions can’t share memory, so the result goes through a mailbox (storage). We measure how much of the tool time is real work vs. moving data between functions.


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

 

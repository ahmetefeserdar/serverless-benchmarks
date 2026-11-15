# 730.browser - Browser Agent Tool

**Type:** Agentic
**Languages:** Python
**Architecture:** arm64 (tested), x64 (should work, not tested yet)

## Description

When an AI agent needs a website that a plain HTTP download can't handle, like a shop that builds its page with JavaScript or a form it has to fill in, it reaches for a real browser. Tools like [Playwright MCP](https://github.com/microsoft/playwright-mcp) and [browser-use](https://github.com/browser-use/browser-use) give the model a headless Chromium. It navigates, clicks, types, and then reads the page back as text to decide what to do next.

This benchmark is that tool, packaged as a serverless function. Each invocation opens a stored website, performs a list of actions in order, and returns what the agent would read next. It also times every phase along the way: downloading the site, starting the browser, each action, and shutting down.

There is no LLM inside the function. The action list comes from the input. Right now that's a hand-written task per size; later, it will come from traces recorded with a real model and replayed (see `agentic/TRACE_FORMAT.md`). The function doesn't care where the actions come from.

### Why this one is interesting for serverless

It's heavy in every way a function shouldn't be:

- **Big package.** About 475 MB: Chromium alone is 283 MB, and Playwright's Python package ships its own Node.js (157 MB). That's almost twice the 250 MB limit for AWS Lambda zip packages.
- **Slow start.** Before the first click, Playwright has to start its driver and launch Chromium. Locally this costs several seconds, often more than the browsing itself.
- **Many processes.** One browser session runs about 8 processes (Python, Node.js, and several Chromium processes), using roughly 350-450 MB together.
- **Stateful by nature.** An agent keeps one tab open across many steps, but functions forget everything between calls. See `reuse_browser` below.

## Inputs

There are three sizes, each a small agent task on a stored website:

| Size | Website | What the agent does | Actions |
|---|---|---|---|
| `test` | A one-field contact form | Types a name, clicks Send, reads the reply | 5 |
| `small` | The Wikipedia article "Serverless computing" | Reads it, jumps to "Challenges", extracts the text, takes a screenshot | 5 |
| `large` | "OneStop Shop", a store with 2,000 products | Searches for headphones, sorts by price, opens the cheapest, adds 2 to the cart, checks out | 19 |

A few notes on the websites:

- **They are saved copies, never live sites.** `generate_input` packs the folder from `sites/` into a `.tar.gz` and uploads it to object storage. The function downloads it, unpacks it, and serves it on `127.0.0.1`. Chromium is started with every other hostname unresolvable, so even if a page references something on the internet, the request simply fails (and shows up in `requests_failed`). Runs never depend on the live web.
- **The Wikipedia page is pinned** to revision 1375122930 (16 September 2026), together with its stylesheets and images. Wikipedia text is CC BY-SA.
- **The shop is synthetic.** It's modeled on the shopping site in [WebArena](https://webarena.dev/), because buying things is one of the most common web-agent tasks, but it's not a real store. The catalog is generated in `input.py` with a fixed random seed, so it's identical on every run. Replacing it with pages from WebArena or Mind2Web is on the to-do list.

## The input event

This is what the function receives, and what a trace's tool step would put in `args`:

```json
{
  "bucket": {"bucket": "<benchmarks bucket>", "input": "<input prefix>"},
  "site": "form.tar.gz",
  "actions": [
    {"op": "navigate", "path": "index.html"},
    {"op": "type", "selector": "#name", "text": "SeBS"},
    {"op": "click", "selector": "#greet"},
    {"op": "wait_for", "text": "Hello, SeBS!"},
    {"op": "snapshot"}
  ],
  "max_length": 5000,
  "reuse_browser": false
}
```

- `site` is the archive in storage. `actions` run in order, and the function stops at the first one that fails, the way an agent would get an error back.
- `max_length` caps the text returned to the agent (5000 characters, the default of Playwright MCP and the MCP fetch tool).
- `reuse_browser` keeps Chromium running between warm invocations instead of starting a new one every time. Comparing both settings tells you how much an agent pays for functions being stateless.

### Actions

The names follow Playwright MCP's tools, so traces recorded from real agents map onto them directly. Selectors are normal CSS selectors (`#name` means "the element with id `name`").

| Action | Fields | What happens |
|---|---|---|
| `navigate` | `path` | Opens a page of the site |
| `click` | `selector` | Clicks an element |
| `type` | `selector`, `text`, optional `submit` | Fills a field; with `submit: true` also presses Enter |
| `select` | `selector`, `value` | Picks an option in a dropdown |
| `press` | `key` | Presses a key, e.g. `Enter` |
| `wait_for` | `text` | Waits until the text appears on the page |
| `snapshot` | | Returns the page's accessibility tree as text. This is what Playwright MCP gives the model, e.g. `- button "Send"` |
| `extract_text` | optional `selector` | Returns the visible text of the page or of one element |
| `screenshot` | optional `full_page` | Takes a PNG; only its size and SHA-256 are returned, not the image |

Each action waits up to 15 seconds for its element before failing.

## Output

The `result` part is what the agent would see:

- `content`: the text from the last `snapshot` or `extract_text`, truncated to `max_length`
- `steps`: one entry per action with `ok`, its duration (`time`, in microseconds), and an `error` if it failed
- `screenshots`, `title`, and `url` of the final page

The `measurement` part is for us. All times are in microseconds, like in the other benchmarks:

| Field | What it covers |
|---|---|
| `download_time`, `unpack_time` | Getting the site from storage and starting the local web server |
| `driver_start_time` | Starting Playwright's Node.js driver |
| `browser_launch_time` | Launching Chromium (0 if a warm browser was reused) |
| `context_time` | Opening a fresh tab with its own cookies and storage |
| `actions_time` | All actions together |
| `close_time` | Closing the tab, and the browser unless it's reused |
| `total_time` | Everything above |
| `fetch_time`, `compute_time` | Input vs. work, with the same meaning as in `710.web-fetch`, so `agentic/replay.py` can show both tools the same way |
| `peak_memory_kb` | Highest memory of the whole process tree (Python, Node.js, Chromium), sampled after each action. It uses PSS, so memory shared between Chromium's processes isn't counted twice |
| `peak_processes` | Number of processes in that tree |
| `requests_served`, `bytes_served`, `requests_failed` | What the browser loaded from the local server, and what it couldn't load (e.g. external URLs) |
| `browser_launched`, `chromium_version` | Whether this call started a new browser, and which version |

## Correctness check

`validate_output` checks that every action succeeded, that the agent's final text contains what it should (e.g. "Hello, SeBS!", or for the shop the right product, quantity, and total), that the text isn't longer than `max_length`, and that each requested screenshot isn't empty. For the shop, the expected answer is computed from the generated catalog, so it stays correct if the catalog changes.

## Packaging

Chromium needs about twenty system libraries (`libnss3`, `libgbm`, X11 libraries, and so on) that neither the SeBS images nor AWS Lambda have. We can't install them at runtime, and the SeBS build container doesn't run as root, so `python/package.sh` does the following during the build:

1. Downloads the Chromium **headless shell** that matches the pinned Playwright version. It's the headless-only build, smaller than full Chromium.
2. Downloads the Debian packages Chromium depends on with `apt-get download` (no root needed) and unpacks them.
3. Copies only the libraries Chromium and Node.js actually load into `browser/lib`. That's 22 MB, compared to 343 MB for everything apt pulls in.
4. Adds the Liberation fonts, so text renders the same everywhere.
5. Checks that nothing is missing, so a broken package fails at build time instead of at the first invocation.

At runtime, `function.py` points `LD_LIBRARY_PATH` at `browser/lib` and launches Chromium with `--no-sandbox` (containers have no user namespaces) and `--disable-dev-shm-usage` (`/dev/shm` is tiny in containers).

The package has to be built on the same architecture it runs on, because Playwright downloads the browser for the machine it's running on.

## Running it locally

```bash
sebs storage start object configs/storage.json --output-json out_storage.json
sebs benchmark invoke 730.browser test --config configs/example.json \
    --deployment local --architecture arm64 \
    --storage-configuration out_storage.json --repetitions 3 --verbose
```

The first build takes a few minutes because it downloads Chromium. After that it's cached.

The benchmark only needs object storage, so starting just `object` saves the memory ScyllaDB would take.

## Known issues

- **Storage on macOS.** On Mac and Windows, SeBS gives function containers the storage address `localhost:<port>`, which doesn't work from inside a container (it works on Linux). Until that's fixed in SeBS, set `address` to the `external_address` value in `out_storage.json`. Then stop old function containers, because they keep the address they were created with.
- **Local timings on a Mac are noisy.** The code is shared into containers through Docker Desktop's file sharing, which slows down loading Chromium, and memory pressure on a laptop makes it worse. Use a Linux machine or the cloud for numbers you want to report.
- **AWS Lambda** won't accept a 475 MB zip, so it will need a container image deployment. The libraries are also built for Debian 13 (glibc 2.41), which is newer than what Lambda's Python runtimes provide.

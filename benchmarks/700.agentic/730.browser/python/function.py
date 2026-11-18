# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Browser tool of an AI agent, following the Playwright MCP server (microsoft/playwright-mcp)
# and browser-use: a headless Chromium opens a page, performs a sequence of actions (click,
# type, select, wait), and returns what the agent reads next (an accessibility snapshot or the
# page text) and optionally a screenshot.
#
# Pages are stored website snapshots, downloaded from object storage and served on localhost;
# all other hosts are unreachable, so a replay never depends on the live web.

import functools
import glob
import hashlib
import http.server
import io
import os
import shutil
import tarfile
import tempfile
import threading
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import storage
client = storage.storage.get_instance()

BROWSER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'browser')
CHROME = (glob.glob(os.path.join(BROWSER_DIR, 'chromium_headless_shell-*', '*', 'chrome-headless-shell')) or [None])[0]

# Chromium and Playwright's Node.js driver load the bundled libraries (see package.sh).
os.environ['LD_LIBRARY_PATH'] = os.path.join(BROWSER_DIR, 'lib') + ':' + os.environ.get('LD_LIBRARY_PATH', '')

LAUNCH_ARGS = [
    # The Chromium sandbox needs user namespaces, which function containers do not offer.
    '--no-sandbox',
    # /dev/shm is tiny in containers; Chromium uses /tmp instead.
    '--disable-dev-shm-usage',
    '--disable-gpu',
    # Only localhost resolves: snapshots cannot load anything from the live web.
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
]
ACTION_TIMEOUT_MS = 15000
VIEWPORT = {'width': 1280, 'height': 800}

# Kept between warm invocations when the caller asks to reuse the browser.
_playwright = None
_browser = None


def fontconfig_file():
    # The font directory is only known at runtime (the package location differs per platform).
    path = os.path.join(tempfile.gettempdir(), 'sebs-browser-fonts.conf')
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{os.path.join(BROWSER_DIR, 'fonts')}</dir>
  <cachedir>{os.path.join(tempfile.gettempdir(), 'fontconfig')}</cachedir>
  <alias><family>sans-serif</family><prefer><family>Liberation Sans</family></prefer></alias>
  <alias><family>serif</family><prefer><family>Liberation Serif</family></prefer></alias>
  <alias><family>monospace</family><prefer><family>Liberation Mono</family></prefer></alias>
</fontconfig>
""")
    return path


class SiteServer:
    """Serves an unpacked website snapshot on a free localhost port, counting requests."""

    def __init__(self, directory):
        self.requests = 0
        self.bytes_sent = 0
        server = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_head(self):
                server.requests += 1
                return super().send_head()

            def copyfile(self, source, outputfile):
                data = source.read()
                server.bytes_sent += len(data)
                outputfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=directory))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def process_tree_memory_kb():
    """Memory of this process and all descendants (Node.js driver, Chromium processes).

    Uses PSS (proportional set size), which splits shared pages between processes, so the
    sum is not inflated by the libraries every Chromium process maps. Linux only.
    """
    parents = {}
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                with open(f'/proc/{entry}/stat') as f:
                    # The command name may contain spaces; fields after ')' are fixed.
                    parents[int(entry)] = int(f.read().rsplit(')', 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                pass
    tree, frontier = {os.getpid()}, [os.getpid()]
    while frontier:
        pid = frontier.pop()
        for child, parent in parents.items():
            if parent == pid and child not in tree:
                tree.add(child)
                frontier.append(child)
    total = 0
    for pid in tree:
        try:
            with open(f'/proc/{pid}/smaps_rollup') as f:
                total += next(int(line.split()[1]) for line in f if line.startswith('Pss:'))
        except (OSError, StopIteration):
            pass
    return total, len(tree)


def get_browser(reuse):
    global _playwright, _browser
    if reuse and _browser is not None and _browser.is_connected():
        return _browser, False, 0.0, 0.0

    close_browser()
    begin = time.perf_counter()
    # Playwright for Python drives the browser through a Node.js process.
    _playwright = sync_playwright().start()
    driver_start = time.perf_counter() - begin

    begin = time.perf_counter()
    env = dict(os.environ, FONTCONFIG_FILE=fontconfig_file(), HOME=tempfile.gettempdir())
    _browser = _playwright.chromium.launch(executable_path=CHROME, args=LAUNCH_ARGS, env=env)
    launch = time.perf_counter() - begin
    return _browser, True, driver_start, launch


def close_browser():
    global _playwright, _browser
    if _browser is not None:
        try:
            _browser.close()
        except PlaywrightError:
            pass
    if _playwright is not None:
        _playwright.stop()
    _playwright, _browser = None, None


def truncate(text, max_length):
    if len(text) <= max_length:
        return text
    return text[:max_length] + f'\n\n<error>Content truncated: {len(text)} characters in total.</error>'


def run_action(page, base_url, action, max_length):
    """Performs one agent action; returns the text the agent reads after it, if any."""
    op = action['op']
    if op == 'navigate':
        page.goto(base_url + action.get('path', ''), wait_until='load')
    elif op == 'click':
        page.click(action['selector'])
    elif op == 'type':
        page.fill(action['selector'], action['text'])
        if action.get('submit'):
            page.press(action['selector'], 'Enter')
    elif op == 'select':
        page.select_option(action['selector'], action['value'])
    elif op == 'press':
        page.keyboard.press(action['key'])
    elif op == 'wait_for':
        page.get_by_text(action['text']).first.wait_for()
    elif op == 'snapshot':
        # What Playwright MCP returns to the model: the page's accessibility tree as text.
        return truncate(page.locator('body').aria_snapshot(), max_length)
    elif op == 'extract_text':
        return truncate(page.inner_text(action.get('selector', 'body')), max_length)
    elif op == 'screenshot':
        image = page.screenshot(full_page=action.get('full_page', False))
        return {'screenshot_bytes': len(image), 'screenshot_sha256': hashlib.sha256(image).hexdigest()}
    else:
        raise ValueError(f'Unknown action: {op}')
    return None


def handler(event):
    bucket = event['bucket']['bucket']
    site_key = os.path.join(event['bucket']['input'], event['site'])
    actions = event['actions']
    max_length = event.get('max_length', 5000)
    reuse = event.get('reuse_browser', False)

    total_begin = time.perf_counter()

    begin = time.perf_counter()
    archive = client.download_stream(bucket, site_key)
    download_time = time.perf_counter() - begin

    begin = time.perf_counter()
    site_dir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        tar.extractall(site_dir)
    server = SiteServer(site_dir)
    unpack_time = time.perf_counter() - begin

    browser, launched, driver_start_time, launch_time = get_browser(reuse)

    begin = time.perf_counter()
    context = browser.new_context(viewport=VIEWPORT)
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    page = context.new_page()
    # Includes requests to other hosts, which cannot resolve.
    failed = []
    page.on('requestfailed', lambda request: failed.append(request.url))
    context_time = time.perf_counter() - begin

    steps, content, screenshots = [], '', []
    peak_memory_kb, peak_processes = 0, 0
    actions_begin = time.perf_counter()
    try:
        for action in actions:
            begin = time.perf_counter()
            step = {'op': action['op']}
            try:
                output = run_action(page, server.url, action, max_length)
                step['ok'] = True
                if isinstance(output, str):
                    content = output
                    step['chars'] = len(output)
                elif isinstance(output, dict):
                    screenshots.append(output)
                    step.update(output)
            except (PlaywrightError, KeyError, ValueError) as e:
                # An agent would see the error and decide what to do; a replay stops here.
                step['ok'] = False
                step['error'] = str(e).splitlines()[0]
            step['time'] = (time.perf_counter() - begin) * 1e6
            memory_kb, processes = process_tree_memory_kb()
            peak_memory_kb = max(peak_memory_kb, memory_kb)
            peak_processes = max(peak_processes, processes)
            steps.append(step)
            if not step['ok']:
                break
        actions_time = time.perf_counter() - actions_begin
        title, final_path = page.title(), page.url.replace(server.url, '/')
    finally:
        begin = time.perf_counter()
        context.close()
        if not reuse:
            close_browser()
        server.close()
        shutil.rmtree(site_dir, ignore_errors=True)
        close_time = time.perf_counter() - begin

    total_time = time.perf_counter() - total_begin
    browser_time = driver_start_time + launch_time + context_time + actions_time + close_time
    return {
        'result': {
            'title': title,
            'url': final_path,
            'content': content,
            'steps': steps,
            'screenshots': screenshots,
        },
        'measurement': {
            # Microseconds, as in the other benchmarks. fetch_time and compute_time follow
            # 710.web-fetch: getting the input versus working on it.
            'fetch_time': (download_time + unpack_time) * 1e6,
            'compute_time': browser_time * 1e6,
            'download_time': download_time * 1e6,
            'unpack_time': unpack_time * 1e6,
            'driver_start_time': driver_start_time * 1e6,
            'browser_launch_time': launch_time * 1e6,
            'context_time': context_time * 1e6,
            'actions_time': actions_time * 1e6,
            'close_time': close_time * 1e6,
            'total_time': total_time * 1e6,
            'browser_launched': launched,
            'peak_memory_kb': peak_memory_kb,
            'peak_processes': peak_processes,
            'site_bytes': len(archive),
            'requests_served': server.requests,
            'bytes_served': server.bytes_sent,
            'requests_failed': len(failed),
            'chromium_version': browser.version,
        }
    }

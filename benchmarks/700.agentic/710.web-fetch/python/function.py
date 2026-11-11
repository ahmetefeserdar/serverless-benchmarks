# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Web fetch tool of an AI agent, following the reference MCP fetch server
# (modelcontextprotocol/servers, src/fetch): download a page, extract the
# main content, convert it to Markdown, and truncate it to the context budget.

import datetime

import httpx
import markdownify
import readabilipy.simple_json

SEBS_USER_AGENT = "SeBS/1.2 (https://github.com/spcl/serverless-benchmarks) SeBS Benchmark Suite/1.2"


def extract_content_from_html(html):
    # Pure-Python extractor; Readability.js would require Node in the function.
    ret = readabilipy.simple_json.simple_json_from_html_string(html, use_readability=False)
    if not ret.get('content'):
        return "<error>Page failed to be simplified from HTML</error>"
    return markdownify.markdownify(ret['content'], heading_style=markdownify.ATX)


def handler(event):

    url = event.get('url')
    max_length = event.get('max_length', 5000)
    start_index = event.get('start_index', 0)

    fetch_begin = datetime.datetime.now()
    response = httpx.get(url, headers={'User-Agent': SEBS_USER_AGENT}, follow_redirects=True, timeout=30)
    response.raise_for_status()
    html = response.text
    fetch_end = datetime.datetime.now()

    process_begin = datetime.datetime.now()
    content = extract_content_from_html(html)
    process_end = datetime.datetime.now()

    original_length = len(content)
    truncated = content[start_index:start_index + max_length]
    remaining = original_length - (start_index + len(truncated))
    if remaining > 0:
        next_start = start_index + len(truncated)
        truncated += f"\n\n<error>Content truncated. Call the fetch tool with a start_index of {next_start} to get more content.</error>"

    fetch_time = (fetch_end - fetch_begin) / datetime.timedelta(microseconds=1)
    process_time = (process_end - process_begin) / datetime.timedelta(microseconds=1)
    return {
        'result': {
            'url': url,
            'content': truncated,
        },
        'measurement': {
            'fetch_time': fetch_time,
            'fetch_size': len(response.content),
            'compute_time': process_time,
            'markdown_length': original_length,
        }
    }

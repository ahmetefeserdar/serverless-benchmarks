# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
import glob
import os

# Pinned Wikipedia snapshots (see agentic/snapshot.py and pages/manifest.json).
# Sizes of the raw HTML: ~185 kB, ~527 kB, ~3 MB.
size_pages = {
    'test': 'Serverless_computing',
    'small': 'Cloud_computing',
    'large': 'United_States',
}

expected_keywords = {
    'Serverless_computing': 'serverless',
    'Cloud_computing': 'cloud',
    'United_States': 'united states',
}

# Default context budget of the MCP fetch tool.
MAX_LENGTH = 5000


def buckets_count():
    return (1, 0)


def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):

    # Upload every snapshot, so that traces can reference any stored page.
    for page in glob.glob(os.path.join(data_dir, 'pages', '*.html')):
        upload_func(0, os.path.basename(page), page)

    return {
        'bucket': {'bucket': benchmarks_bucket, 'input': input_paths[0]},
        'page': size_pages[size],
        'max_length': MAX_LENGTH,
        'start_index': 0,
    }


def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage = None) -> str | None:

    result = output.get('result', {})
    content = result.get('content', '')
    page = input_config.get('page', '')

    if not isinstance(content, str) or len(content) == 0:
        return f"Content is missing or empty (type={type(content).__name__})"

    if 'failed to be simplified' in content:
        return "HTML could not be simplified to Markdown"

    if len(content) > input_config['max_length'] + 200:
        return f"Content not truncated: {len(content)} chars > max_length {input_config['max_length']}"

    keyword = expected_keywords.get(page)
    if keyword and input_config.get('start_index', 0) == 0 and keyword not in content.lower():
        return f"Expected keyword '{keyword}' not found in content"

    return None

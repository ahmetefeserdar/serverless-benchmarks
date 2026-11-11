# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Pages of increasing size, fetched live like 120.uploader.
# TODO: pin revisions or mirror into benchmarks-data for reproducibility.
url_generators = {
    # ~100 kB HTML
    'test': 'https://en.wikipedia.org/wiki/Serverless_computing',
    # ~500 kB HTML
    'small': 'https://en.wikipedia.org/wiki/Cloud_computing',
    # ~1.5 MB HTML
    'large': 'https://en.wikipedia.org/wiki/United_States',
}

expected_keywords = {
    'test': 'serverless',
    'small': 'cloud',
    'large': 'united states',
}

# Default context budget of the MCP fetch tool.
MAX_LENGTH = 5000


def buckets_count():
    return (0, 0)


def generate_input(data_dir, size, benchmarks_bucket, input_buckets, output_buckets, upload_func, nosql_func):
    return {'url': url_generators[size], 'size': size, 'max_length': MAX_LENGTH, 'start_index': 0}


def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage = None) -> str | None:

    result = output.get('result', {})
    content = result.get('content', '')
    size = input_config.get('size', '')

    if not isinstance(content, str) or len(content) == 0:
        return f"Content is missing or empty (type={type(content).__name__})"

    if 'failed to be simplified' in content:
        return "HTML could not be simplified to Markdown"

    if len(content) > input_config['max_length'] + 200:
        return f"Content not truncated: {len(content)} chars > max_length {input_config['max_length']}"

    if expected_keywords[size] not in content.lower():
        return f"Expected keyword '{expected_keywords[size]}' not found in content"

    return None

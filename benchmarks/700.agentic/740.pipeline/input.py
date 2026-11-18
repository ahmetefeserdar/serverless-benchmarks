# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Size of the result handed from one step to the next.
# test ~ a short tool answer, small ~ a fetched web page,
# large ~ a big test log or repository listing.
sizes = {
    'test': 10 * 1024,
    'small': 1024 * 1024,
    'large': 10 * 1024 * 1024,
}


def buckets_count():
    return (0, 1)


def generate_input(data_dir, size, benchmarks_bucket, input_buckets, output_buckets, upload_func, nosql_func):
    # A single SeBS invocation runs the first step (produce, via storage).
    # Full pipelines are driven by agentic/pipeline.py from a trace.
    return {
        'step': 'produce',
        'mode': 'storage',
        'size': size,
        'size_bytes': sizes[size],
        'seed': 42,
        'bucket': {'bucket': benchmarks_bucket, 'output': output_buckets[0]},
    }


def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage = None) -> str | None:

    result = output.get('result', {})
    expected = input_config['size_bytes']

    if result.get('size_bytes') != expected:
        return f"Produced {result.get('size_bytes')} bytes, expected {expected}"

    if input_config.get('mode') == 'storage':
        key = result.get('key')
        if not isinstance(key, str) or not key:
            return "Storage key missing from output"
        if storage is not None:
            data = storage.download_stream(result['bucket'], key)
            if len(data) != expected:
                return f"Object in storage has {len(data)} bytes, expected {expected}"

    return None

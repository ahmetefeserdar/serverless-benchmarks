# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# One step of a multi-step agent task. Serverless functions do not share
# memory, so a result has to travel from one step to the next either inside
# the request/response ("inline") or through object storage ("storage").
#
#   produce: makes a text result of a chosen size, like a fetched page or a
#            test log, and hands it off in the chosen mode.
#   consume: receives one or more earlier results, loads them if they are in
#            storage, and does a small amount of work on them (word count).
#
# Every step reports how long it spent on real work and how long on moving
# data to or from storage, so the runner can split end-to-end time.

import datetime
import hashlib
import io
import random

from . import storage
client = storage.storage.get_instance()

WORDS = ["agent", "tool", "serverless", "cloud", "function", "storage",
         "trace", "latency", "cold", "start", "browser", "code", "test"]


def make_text(size_bytes, seed):
    # Deterministic, so the same size and seed always give the same text.
    rng = random.Random(seed)
    parts = []
    length = 0
    while length < size_bytes:
        word = rng.choice(WORDS)
        parts.append(word)
        length += len(word) + 1
    return " ".join(parts)[:size_bytes]


def micros(begin, end):
    return (end - begin) / datetime.timedelta(microseconds=1)


def produce(event):
    size_bytes = int(event["size_bytes"])
    mode = event.get("mode", "storage")
    seed = event.get("seed", 42)

    work_begin = datetime.datetime.now()
    text = make_text(size_bytes, seed)
    data = text.encode()
    digest = hashlib.sha256(data).hexdigest()
    work_end = datetime.datetime.now()

    result = {"mode": mode, "size_bytes": len(data), "sha256": digest}
    upload_time = 0
    if mode == "storage":
        bucket = event["bucket"]["bucket"]
        prefix = event["bucket"]["output"]
        upload_begin = datetime.datetime.now()
        key = client.upload_stream(bucket, f"{prefix}/produce.txt", io.BytesIO(data))
        upload_end = datetime.datetime.now()
        upload_time = micros(upload_begin, upload_end)
        result.update({"bucket": bucket, "key": key})
    elif mode == "inline":
        result["data"] = text
    else:
        raise ValueError(f"unknown mode {mode}")

    return {
        "result": result,
        "measurement": {
            "compute_time": micros(work_begin, work_end),
            "upload_time": upload_time,
            "download_time": 0,
            "upload_size": len(data) if mode == "storage" else 0,
        },
    }


def consume(event):
    inputs = event["inputs"]

    download_time = 0
    download_size = 0
    texts = []
    for item in inputs:
        if item["mode"] == "storage":
            begin = datetime.datetime.now()
            data = client.download_stream(item["bucket"], item["key"])
            end = datetime.datetime.now()
            download_time += micros(begin, end)
            download_size += len(data)
        else:
            data = item["data"].encode()
        texts.append(data)

    work_begin = datetime.datetime.now()
    checks = []
    word_counts = {}
    for item, data in zip(inputs, texts):
        checks.append(hashlib.sha256(data).hexdigest() == item["sha256"])
        for word in data.decode().split():
            word_counts[word] = word_counts.get(word, 0) + 1
    top = sorted(word_counts.items(), key=lambda kv: -kv[1])[:5]
    work_end = datetime.datetime.now()

    return {
        "result": {
            "inputs": len(inputs),
            "total_bytes": sum(len(t) for t in texts),
            "total_words": sum(word_counts.values()),
            "top_words": top,
            "all_checksums_ok": all(checks),
        },
        "measurement": {
            "compute_time": micros(work_begin, work_end),
            "upload_time": 0,
            "download_time": download_time,
            "download_size": download_size,
        },
    }


def handler(event):
    step = event.get("step", "produce")
    if step == "produce":
        return produce(event)
    if step == "consume":
        return consume(event)
    raise ValueError(f"unknown step {step}")

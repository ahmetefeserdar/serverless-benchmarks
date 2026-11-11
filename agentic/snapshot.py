"""Page snapshots for 710.web-fetch.

Replays must not depend on live websites, so every page a trace refers to is
stored once as an HTML snapshot in benchmarks-data and uploaded to object
storage by the benchmark's input generator.

Usage:
  python3 snapshot.py pin <Wikipedia_Title> [...]   # store the current revision, pinned by oldid
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

USER_AGENT = "SeBS/1.2 (https://github.com/spcl/serverless-benchmarks) SeBS Benchmark Suite/1.2"
PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "benchmarks-data", "700.agentic", "710.web-fetch", "pages")
MANIFEST = os.path.join(PAGES_DIR, "manifest.json")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def slug(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("wikipedia.org") and parsed.path.startswith("/wiki/"):
        return urllib.parse.unquote(parsed.path[len("/wiki/"):])
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", parsed.netloc + parsed.path).strip("_")


def _load_manifest():
    return json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}


def _store(name, html, meta):
    os.makedirs(PAGES_DIR, exist_ok=True)
    with open(os.path.join(PAGES_DIR, name + ".html"), "wb") as f:
        f.write(html)
    manifest = _load_manifest()
    manifest[name] = {**meta, "bytes": len(html), "sha256": hashlib.sha256(html).hexdigest()}
    json.dump(manifest, open(MANIFEST, "w"), indent=2, sort_keys=True)


def save(url):
    """Store a page as fetched right now (used by the recorder); returns its snapshot name."""
    name = slug(url)
    if name not in _load_manifest():
        _store(name, _get(url), {"url": url})
    return name


def pin_wikipedia(title):
    """Store a Wikipedia article at its current revision, referenced by oldid."""
    api = ("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids"
           f"&format=json&titles={urllib.parse.quote(title)}")
    page = next(iter(json.loads(_get(api))["query"]["pages"].values()))
    oldid = page["revisions"][0]["revid"]
    url = f"https://en.wikipedia.org/w/index.php?title={urllib.parse.quote(title)}&oldid={oldid}"
    _store(title, _get(url), {"url": f"https://en.wikipedia.org/wiki/{title}", "oldid": oldid})
    return title, oldid


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "pin":
        sys.exit(__doc__)
    for title in sys.argv[2:]:
        print("pinned %s @ oldid %s" % pin_wikipedia(title))

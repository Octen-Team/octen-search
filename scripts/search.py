#!/usr/bin/env python3
"""
Octen Search — parallel multi-query web search via Octen API.

Supports 1 to 1000 queries. Auto-scales from single lookups to massive
parallel research.

Usage (single query):
    python search.py --query "electric vehicle sales 2026"

Usage (multiple queries from file):
    python search.py --queries queries.json [--output results.json] [--batch-size 20]

queries.json format (simple — array of strings):
    ["query 1", "query 2", "query 3"]

queries.json format (advanced — array of objects with per-query options):
    [
        {"query": "query 1", "count": 5},
        {"query": "query 2", "count": 3, "include_domains": ["arxiv.org"]},
        {"query": "query 3", "count": 5, "full_content": {"enable": true, "max_tokens": 3000}}
    ]

    Flat shorthands are also accepted for convenience:
        enable_full_content / full_content_max_tokens
        enable_highlight / highlight_max_tokens

Results are written to stdout (or --output file) as JSON:
    {
        "total_queries": 10,
        "successful": 9,
        "failed": 1,
        "elapsed_seconds": 2.3,
        "results": [
            {"query": "query 1", "status": "ok", "data": {...}},
            {"query": "query 2", "status": "error", "error": "timeout"}
        ]
    }

API key resolution order:
    1. --api-key CLI argument
    2. OCTEN_API_KEY environment variable
    3. config.json in the skill directory ({"api_key": "your-key"})
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("Installing requests...", file=sys.stderr)
    os.system(f"{sys.executable} -m pip install requests --break-system-packages -q")
    import requests
    from requests.adapters import HTTPAdapter

API_URL = "https://api.octen.ai/search"
DEFAULT_BATCH_SIZE = 20  # queries per wave — balances parallelism vs connection reuse
DEFAULT_TIMEOUT = 30  # seconds per request
CHUNK_SIZE = 1000  # auto-split into chunks of this size for very large query sets

# Default search params applied to every query unless overridden
DEFAULT_PARAMS = {
    "count": 5,  # matches API default; balances depth and variety
}


def resolve_api_key(cli_key: str | None) -> str:
    """
    Resolve API key from (in priority order):
      1. --api-key CLI argument
      2. OCTEN_API_KEY environment variable
      3. config.json in the skill directory
    """
    if cli_key:
        return cli_key

    env_key = os.environ.get("OCTEN_API_KEY")
    if env_key:
        return env_key

    # Look for config.json next to the scripts/ directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    config_path = os.path.join(skill_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            key = config.get("api_key")
            if key:
                return key
        except Exception:
            pass

    print(
        "Error: No API key found. Provide one via:\n"
        "  1. --api-key argument\n"
        "  2. OCTEN_API_KEY environment variable\n"
        '  3. config.json in the skill directory: {"api_key": "your-key"}',
        file=sys.stderr,
    )
    sys.exit(1)


def normalize_queries(raw: list) -> list[dict]:
    """Accept both simple string list and advanced object list."""
    normalized = []
    for item in raw:
        if isinstance(item, str):
            normalized.append({"query": item})
        elif isinstance(item, dict) and "query" in item:
            normalized.append(item)
        else:
            print(f"Warning: skipping invalid query entry: {item}", file=sys.stderr)
    return normalized


def build_api_body(query_obj: dict) -> dict:
    """
    Transform a query object (which may use flat shorthand keys) into
    the nested format expected by the Octen /search API.

    Accepted flat shorthands (mapped to nested API fields):
        enable_full_content      → full_content.enable
        full_content_max_tokens  → full_content.max_tokens
        enable_highlight         → highlight.enable
        highlight_max_tokens     → highlight.max_tokens

    Native nested dicts (highlight, full_content) are also accepted and
    take precedence over flat shorthands.
    """
    obj = {**DEFAULT_PARAMS, **query_obj}

    # --- Build the API body with only valid top-level keys ---
    TOP_LEVEL_KEYS = {
        "query", "count", "include_domains", "exclude_domains",
        "include_text", "exclude_text", "time_basis", "start_time",
        "end_time", "format", "safesearch",
    }
    body: dict = {k: v for k, v in obj.items() if k in TOP_LEVEL_KEYS}

    # --- highlight (nested object) ---
    if "highlight" in obj and isinstance(obj["highlight"], dict):
        body["highlight"] = obj["highlight"]
    else:
        hl: dict = {"enable": True, "max_tokens": 2048}  # default: rich highlights (matches API default)
        if "enable_highlight" in obj:
            hl["enable"] = bool(obj["enable_highlight"])
        if "highlight_max_tokens" in obj:
            hl["max_tokens"] = int(obj["highlight_max_tokens"])
        body["highlight"] = hl

    # --- full_content (nested object) ---
    if "full_content" in obj and isinstance(obj["full_content"], dict):
        body["full_content"] = obj["full_content"]
    else:
        fc: dict = {}
        enable_fc = obj.get("enable_full_content", False)
        if enable_fc:
            fc["enable"] = True
            if "full_content_max_tokens" in obj:
                fc["max_tokens"] = int(obj["full_content_max_tokens"])
        if fc:
            body["full_content"] = fc

    return body


def search_one(query_obj: dict, session: requests.Session, timeout: int) -> dict:
    """Execute a single search request using a shared session. Returns a result dict."""
    query_text = query_obj["query"]
    body = build_api_body(query_obj)

    try:
        resp = session.post(API_URL, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return {"query": query_text, "status": "ok", "data": data}
    except requests.exceptions.Timeout:
        return {"query": query_text, "status": "error", "error": "timeout"}
    except requests.exceptions.HTTPError as e:
        return {
            "query": query_text,
            "status": "error",
            "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
        }
    except Exception as e:
        return {"query": query_text, "status": "error", "error": str(e)[:200]}


def octen_search(
    queries: list[dict],
    api_key: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Run all queries in parallel using batched waves with connection reuse.

    Supports 1 to 1000 queries. For a single query, runs directly without
    threading overhead. For multiple queries, processes in waves of `batch_size`.
    A shared requests.Session with a connection pool recycles TCP connections
    across waves, so only the first wave pays the TLS handshake cost —
    subsequent waves reuse warm connections and complete much faster.
    """
    t0 = time.time()

    # Create a session with a connection pool sized to the batch
    pool_size = min(batch_size, len(queries))
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "x-api-key": api_key,
    })
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)

    results = [None] * len(queries)

    # Fast path: single query — skip threading overhead
    if len(queries) == 1:
        results[0] = search_one(queries[0], session, timeout)
    else:
        # Process in waves — connections are reused between waves
        for wave_start in range(0, len(queries), batch_size):
            wave_end = min(wave_start + batch_size, len(queries))
            wave_indices = list(range(wave_start, wave_end))

            with ThreadPoolExecutor(max_workers=len(wave_indices)) as pool:
                future_to_idx = {
                    pool.submit(search_one, queries[i], session, timeout): i
                    for i in wave_indices
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as e:
                        results[idx] = {
                            "query": queries[idx]["query"],
                            "status": "error",
                            "error": str(e)[:200],
                        }

    session.close()

    elapsed = round(time.time() - t0, 2)
    ok_count = sum(1 for r in results if r and r["status"] == "ok")

    return {
        "total_queries": len(queries),
        "successful": ok_count,
        "failed": len(queries) - ok_count,
        "elapsed_seconds": elapsed,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Octen Search")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--query",
        help="Single search query string (for quick single lookups)",
    )
    group.add_argument(
        "--queries",
        help="Path to JSON file with queries (array of strings or objects, unlimited — auto-chunks at 1000)",
    )
    parser.add_argument("--api-key", help="Octen API key (or set OCTEN_API_KEY)")
    parser.add_argument("--output", help="Output file path (default: stdout)")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Queries per wave (default: {DEFAULT_BATCH_SIZE}). "
             "Connections are reused between waves for speed.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()

    api_key = resolve_api_key(args.api_key)

    # Build query list from either --query or --queries
    if args.query:
        # Single query mode
        queries = [{"query": args.query}]
    else:
        # File mode
        with open(args.queries) as f:
            raw = json.load(f)

        if isinstance(raw, str):
            # Support a bare string in the JSON file
            raw = [raw]
        elif not isinstance(raw, list):
            print("Error: queries file must contain a JSON array or a single string.", file=sys.stderr)
            sys.exit(1)

        queries = normalize_queries(raw)

    if not queries:
        print("Error: no valid queries found.", file=sys.stderr)
        sys.exit(1)

    # Auto-chunk: if queries exceed CHUNK_SIZE, split into sequential chunks
    # and merge results. Each chunk runs its own parallel waves internally.
    if len(queries) <= CHUNK_SIZE:
        chunks = [queries]
    else:
        chunks = [
            queries[i:i + CHUNK_SIZE]
            for i in range(0, len(queries), CHUNK_SIZE)
        ]
        print(
            f"Large query set ({len(queries)} queries) — auto-splitting into "
            f"{len(chunks)} chunk(s) of up to {CHUNK_SIZE} each.",
            file=sys.stderr,
        )

    total_waves = (len(queries) + args.batch_size - 1) // args.batch_size
    print(
        f"Searching {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
        f"in {total_waves} wave(s) (batch_size={args.batch_size})...",
        file=sys.stderr,
    )

    # Run each chunk and merge
    all_results = []
    t0 = time.time()
    for ci, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  Chunk {ci + 1}/{len(chunks)}: {len(chunk)} queries...", file=sys.stderr)
        chunk_result = octen_search(chunk, api_key, args.batch_size, args.timeout)
        all_results.extend(chunk_result["results"])

    elapsed = round(time.time() - t0, 2)
    ok_count = sum(1 for r in all_results if r and r["status"] == "ok")
    result = {
        "total_queries": len(all_results),
        "successful": ok_count,
        "failed": len(all_results) - ok_count,
        "elapsed_seconds": elapsed,
        "results": all_results,
    }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_json)
        print(
            f"Done: {result['successful']}/{result['total_queries']} succeeded "
            f"in {result['elapsed_seconds']}s → {args.output}",
            file=sys.stderr,
        )
    else:
        print(output_json)


if __name__ == "__main__":
    main()

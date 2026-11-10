#!/usr/bin/env python3
"""Sync active quotes from a Notion database into quotes.json.

Runs in GitHub Actions (see .github/workflows/sync-quotes.yml). Uses only the
standard library, so the workflow needs no `pip install` step.

Environment:
  NOTION_TOKEN        Notion internal integration secret (required)
  NOTION_DATABASE_ID  ID of the Quotes database (required)
  NOTION_API_BASE     Override for tests (default: https://api.notion.com/v1)
  QUOTES_OUT          Output path (default: quotes.json)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("NOTION_API_BASE", "https://api.notion.com/v1")
NOTION_VERSION = "2022-06-28"
OUT = os.environ.get("QUOTES_OUT", "quotes.json")


def post(url, token, body, attempts=3):
    """POST JSON with a small retry for rate limits (429) and server errors."""
    data = json.dumps(body).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            retryable = err.code == 429 or err.code >= 500
            if retryable and attempt < attempts:
                time.sleep(float(err.headers.get("Retry-After", 2 * attempt)))
                continue
            raise


def query_active(token, database_id):
    """Yield every page where Active is checked, following pagination."""
    url = f"{API}/databases/{database_id}/query"
    body = {
        "page_size": 100,
        "filter": {"property": "Active", "checkbox": {"equals": True}},
    }
    while True:
        data = post(url, token, body)
        yield from data["results"]
        if not data.get("has_more"):
            return
        body["start_cursor"] = data["next_cursor"]


def plain(rich_text):
    """Notion rich text is a list of fragments; join their plain_text."""
    return "".join(part.get("plain_text", "") for part in rich_text).strip()


def parse(page):
    """Turn one Notion page into {q, a, tags}, or None if it has no quote."""
    props = page["properties"]
    text = plain(props["Quote"]["title"])
    if not text:
        return None
    return {
        "q": text,
        "a": plain(props["Author"]["rich_text"]) or "Unknown",
        "tags": [t["name"] for t in props["Tags"]["multi_select"]],
    }


def build(pages):
    """Parse, dedupe and sort. Sorting keeps the file stable between runs."""
    seen, quotes = set(), []
    for page in pages:
        item = parse(page)
        if item is None:
            continue
        key = item["q"].casefold()
        if key in seen:
            continue
        seen.add(key)
        quotes.append(item)
    quotes.sort(key=lambda x: x["q"].casefold())
    return quotes


def main():
    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not token or not database_id:
        sys.exit("NOTION_TOKEN and NOTION_DATABASE_ID must be set")

    try:
        quotes = build(query_active(token, database_id))
    except urllib.error.HTTPError as err:
        sys.exit(f"Notion API error {err.code}: {err.read().decode()[:300]}")

    # Guard: never overwrite a good file with an empty one (wrong ID, revoked
    # access, every row unchecked...). Failing loudly keeps the old quotes live.
    if not quotes:
        sys.exit("Notion returned 0 active quotes; keeping the existing file")

    # No timestamp on purpose: identical content must give identical bytes,
    # otherwise the workflow would commit on every run.
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(quotes, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"Wrote {len(quotes)} quotes to {OUT}")


if __name__ == "__main__":
    main()

import feedparser
import urllib.request
import json
import os
import time
from datetime import datetime, timezone, timedelta

# --- Sources ---
RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Decoder", "url": "https://the-decoder.com/feed/"},
    {"name": "Hacker News AI", "url": "https://hnrss.org/frontpage?q=AI"},
]

REDDIT_URL = "https://www.reddit.com/r/LocalLLaMA/hot.json?limit=25"

# How far back to look (in hours)
LOOKBACK_HOURS = 24

USER_AGENT = "ai-brief/1.1 (+https://github.com/SelemonAmare/ai-brief)"
REQUEST_TIMEOUT_SECONDS = 15
RETRY_DELAYS_SECONDS = (1, 3)


def fetch_url(url, source_name):
    """Fetch a URL with a small retry/backoff loop."""
    last_error = None
    attempts = len(RETRY_DELAYS_SECONDS) + 1

    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read()
        except Exception as e:
            last_error = e
            if attempt < attempts:
                delay = RETRY_DELAYS_SECONDS[attempt - 1]
                print(f"  {source_name}: fetch failed ({e}); retrying in {delay}s")
                time.sleep(delay)

    print(f"  Error fetching {source_name}: {last_error}")
    return None


def fetch_rss(feed):
    """Fetch items from a single RSS feed, return list of article dicts."""
    try:
        raw = fetch_url(feed["url"], feed["name"])
        if raw is None:
            return []

        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            print(f"  Error parsing {feed['name']}: {parsed.bozo_exception}")
            return []

        items = []
        for entry in parsed.entries:
            # feedparser gives us a time.struct_time in published_parsed (UTC)
            pub = entry.get("published_parsed")
            if pub:
                published_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            else:
                published_dt = None

            items.append({
                "title":     entry.get("title", "No title"),
                "url":       entry.get("link", ""),
                "source":    feed["name"],
                "published": published_dt,
            })
        return items
    except Exception as e:
        print(f"  Error fetching {feed['name']}: {e}")
        return []


def fetch_reddit():
    """Fetch hot posts from r/LocalLLaMA using Reddit's public JSON endpoint."""
    try:
        raw = fetch_url(REDDIT_URL, "r/LocalLLaMA")
        if raw is None:
            return []

        data = json.loads(raw)

        items = []
        for post in data["data"]["children"]:
            p = post["data"]
            # Reddit gives created_utc as a Unix timestamp (seconds since epoch)
            published_dt = datetime.fromtimestamp(p["created_utc"], tz=timezone.utc)
            # Use the external URL if it's a link post, otherwise the Reddit thread URL
            url = p.get("url") or f"https://reddit.com{p.get('permalink', '')}"
            items.append({
                "title":     p.get("title", "No title"),
                "url":       url,
                "source":    "r/LocalLLaMA",
                "published": published_dt,
            })
        return items
    except Exception as e:
        print(f"  Error fetching r/LocalLLaMA: {e}")
        return []


def filter_recent(items, hours=LOOKBACK_HOURS):
    """Keep only items published within the last `hours` hours."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    recent = []
    skipped = 0
    for item in items:
        if item["published"] is None:
            # If we can't tell when it was published, keep it to be safe
            recent.append(item)
        elif item["published"] >= cutoff:
            recent.append(item)
        else:
            skipped += 1
    if skipped:
        print(f"  (filtered out {skipped} items older than {hours}h)")
    return recent


def deduplicate(items):
    """Remove near-duplicate stories.

    Two items are considered duplicates when their titles share enough keywords
    in common (case-insensitive). We keep the first occurrence (earliest source).

    Instead of a fixed word count, we use a ratio: if the overlap covers more
    than half of the shorter title's keywords, it's a duplicate. This handles
    short titles like "HY-World 2.0 released" vs "HY-World 2.0 just dropped"
    that a fixed threshold of 4 would miss.
    """
    STOP = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and",
            "or", "is", "are", "with", "how", "why", "what", "new", "this",
            "just", "now", "its", "can", "from", "has", "was", "but", "not"}

    def keywords(title):
        return {w.lower().strip("\"'.,!?:;") for w in title.split()
                if w.lower() not in STOP and len(w) > 2}

    def is_duplicate(kw, kept):
        overlap = len(kw & kept)
        min_len = min(len(kw), len(kept))
        if min_len == 0:
            return False
        # Duplicate if overlap is at least 2 words AND covers >50% of the shorter title
        return overlap >= 2 and (overlap / min_len) >= 0.5

    seen = []      # list of keyword sets for items we've kept
    unique = []

    for item in items:
        kw = keywords(item["title"])
        if not any(is_duplicate(kw, kept) for kept in seen):
            seen.append(kw)
            unique.append(item)

    removed = len(items) - len(unique)
    if removed:
        print(f"  Deduplicated: removed {removed} near-duplicate(s)")
    return unique


def write_brief(items, date_str):
    """Write the markdown brief to briefs/YYYY-MM-DD.md."""
    os.makedirs("briefs", exist_ok=True)
    path = os.path.join("briefs", f"{date_str}.md")

    lines = [
        f"# AI Brief — {date_str}",
        f"*Generated {datetime.now().strftime('%H:%M')} · {len(items)} stories · last 24 h*",
        "",
    ]

    # Group by source so the brief is easy to scan
    sources_seen = []
    by_source = {}
    for item in items:
        src = item["source"]
        if src not in by_source:
            by_source[src] = []
            sources_seen.append(src)
        by_source[src].append(item)

    for src in sources_seen:
        lines.append(f"## {src}")
        for item in by_source[src]:
            ts = item["published"].strftime("%H:%M UTC") if item["published"] else "?"
            lines.append(f"- [{item['title']}]({item['url']}) `{ts}`")
        lines.append("")

    content = "\n".join(lines)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n=== AI Brief — {date_str} ===\n")

    all_items = []

    # RSS feeds
    for feed in RSS_FEEDS:
        print(f"Fetching {feed['name']}...")
        items = fetch_rss(feed)
        items = filter_recent(items)
        all_items.extend(items)
        print(f"  Kept {len(items)} recent items")

    # Reddit
    print("Fetching r/LocalLLaMA...")
    reddit_items = fetch_reddit()
    reddit_items = filter_recent(reddit_items)
    all_items.extend(reddit_items)
    print(f"  Kept {len(reddit_items)} recent items")

    # Deduplicate across all sources
    print("\nDeduplicating...")
    all_items = deduplicate(all_items)

    print(f"\nTotal: {len(all_items)} stories\n")

    # Write markdown file
    path = write_brief(all_items, date_str)
    print(f"Brief written to: {path}\n")


if __name__ == "__main__":
    main()

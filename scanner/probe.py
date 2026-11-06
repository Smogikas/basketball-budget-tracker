#!/usr/bin/env python3
"""One-off helper: for each URL in config/probe_urls.txt, find RSS feeds,
news sitemaps and article links, and write data/probe.json. Run it from the
"Probe sites" workflow when adding new websites."""

import json
import os
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import USER_AGENT, article_like, bounded_get  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = requests.Session()
S.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,*;q=0.5"})


def get(url):
    try:
        return bounded_get(S, url, budget=15, max_bytes=4_000_000)
    except Exception as e:
        return str(e)[:150]


def feed_info(url):
    r = get(url)
    if isinstance(r, str) or r.status_code != 200 or b"<" not in r.content[:500]:
        return None
    soup = BeautifulSoup(r.content, "xml")
    items = soup.find_all(["item", "entry"])
    urls = soup.find_all("url")
    if items:
        sample = []
        for it in items[:4]:
            link = it.find("link")
            sample.append({"title": (it.find("title").text if it.find("title") else "")[:90],
                           "link": (link.get("href") or link.text).strip() if link else ""})
        dates = [d.text for d in soup.find_all(["pubDate", "published", "updated"])][:1]
        return {"url": r.url, "kind": "rss", "items": len(items), "newest": dates, "sample": sample}
    if urls or soup.find_all("sitemap"):
        return {"url": r.url, "kind": "sitemap", "urls": len(urls),
                "children": [s.find("loc").text for s in soup.find_all("sitemap")[:5]],
                "sample": [u.find("loc").text for u in urls[:4]]}
    return None


def probe(url):
    out = {"input": url}
    r = get(url)
    if isinstance(r, str):
        out["error"] = r
        return out
    out["status"], out["final"], out["bytes"] = r.status_code, r.url, len(r.content)
    p = urlparse(r.url)
    origin = f"{p.scheme}://{p.netloc}"
    feeds, tried = [], set()
    candidates = []
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, "lxml")
        for l in soup.find_all("link", rel="alternate"):
            if "rss" in (l.get("type") or "") or "atom" in (l.get("type") or ""):
                candidates.append(urljoin(r.url, l.get("href")))
        links = []
        for a in soup.find_all("a", href=True):
            u = urljoin(r.url, a["href"]).split("#")[0]
            if article_like(u, r.url) and u not in links:
                links.append(u)
        out["article_links"] = len(links)
        out["article_sample"] = links[:8]
    base = r.url.rstrip("/")
    for path in ("/feed", "/rss", "/rss.xml"):
        candidates += [base + path, origin + path]
    robots = get(origin + "/robots.txt")
    if not isinstance(robots, str) and robots.status_code == 200:
        out["robots_sitemaps"] = re.findall(r"(?im)^sitemap:\s*(\S+)", robots.text)[:10]
        for sm in out["robots_sitemaps"]:
            if "news" in sm.lower():
                candidates.append(sm)
        out["robots_blocks_all"] = bool(re.search(r"(?ims)user-agent:\s*\*\s*(?:\n(?!user-agent).*)*?\ndisallow:\s*/\s*$", robots.text))
    for c in candidates:
        if c in tried:
            continue
        tried.add(c)
        info = feed_info(c)
        if info and info["url"] not in [f["url"] for f in feeds]:
            feeds.append(info)
        if len(feeds) >= 3 or len(tried) >= 10:
            break
    out["feeds"] = feeds
    return out


def main():
    with open(os.path.join(ROOT, "config", "probe_urls.txt"), encoding="utf-8") as f:
        urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    from concurrent.futures import ThreadPoolExecutor, wait
    urls = list(dict.fromkeys(urls))  # drop duplicates, keep order
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {u: pool.submit(probe, u) for u in urls}
    wait(futures.values(), timeout=420)  # 7 minutes for everything
    results = [f.result() if f.done() and not f.exception() else
               {"input": u, "error": "timed out" if not f.done() else str(f.exception())[:150]}
               for u, f in futures.items()]
    for r in results:
        print(r["input"], r.get("status", r.get("error")), len(r.get("feeds", [])), "feeds",
              r.get("article_links", 0), "article links", flush=True)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "probe.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os._exit(0)  # don't wait for threads stuck on a hanging site


if __name__ == "__main__":
    main()

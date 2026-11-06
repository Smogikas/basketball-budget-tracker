#!/usr/bin/env python3
"""One-off helper: for every site in config/sites.json, try common "older
pages" URL formats and keep the ones where page 2 and 3 show different
articles than page 1. Writes data/paging.json. Run from the "Probe sites"
workflow (mode: pages)."""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import USER_AGENT, article_like, bounded_get, parse_date  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = requests.Session()
S.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,*;q=0.5"})


def links_on(url, pattern):
    """(links, newest date, oldest date) found on a feed or HTML page."""
    try:
        r = bounded_get(S, url, budget=15)
    except Exception:
        return [], None, None
    if r.status_code != 200:
        return [], None, None
    head = r.content[:600].lower()
    links, dates = [], []
    if b"<rss" in head or b"<feed" in head or b"<urlset" in head or b"<?xml" in head:
        soup = BeautifulSoup(r.content, "xml")
        for it in soup.find_all(["item", "entry", "url"]):
            link = it.find(["link", "loc"])
            u = (link.get("href") or link.text).strip() if link else ""
            d = it.find(["pubDate", "published", "updated", "publication_date", "lastmod"])
            if u:
                links.append(u)
            if d:
                pd = parse_date(d.text)
                if pd:
                    dates.append(pd)
    else:
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            u = urljoin(r.url, a["href"]).split("#")[0]
            if article_like(u, r.url) and (not pattern or re.search(pattern, u)):
                links.append(u.rstrip("/"))
    links = list(dict.fromkeys(links))
    return links, (max(dates).isoformat()[:10] if dates else None), (min(dates).isoformat()[:10] if dates else None)


def candidates(site):
    out = []
    feed = site["url"]
    if site.get("type") == "rss":
        sep = "&" if "?" in feed else "?"
        out.append(feed + sep + "paged={n}")
    section = site.get("section") or site["url"]
    if " " in section or section.endswith(".xml"):
        return out
    base = section.rstrip("/")
    sep = "&" if "?" in section else "?"
    out += [base + "/page/{n}/", section + sep + "page={n}", base + "/{n}",
            section + sep + "p={n}", section + sep + "pg={n}", section + sep + "strana={n}",
            section + sep + "pagina={n}", base + "/pagina/{n}", base + "/strona/{n}"]
    if base.endswith(".html"):
        out.append(base[:-5] + "/{n}.html")
    return out


def probe(site):
    pattern = site.get("link_pattern", "")
    first = site.get("section") or site["url"]
    p1, _, _ = links_on(site["url"] if site.get("type") != "listing" else first, pattern)
    if site.get("type") != "listing" and first != site["url"] and " " not in first:
        p1 += links_on(first, pattern)[0]
    result = {"site": site["name"], "page1_links": len(p1), "working": []}
    seen = set(u.rstrip("/") for u in p1)
    for tpl in candidates(site):
        p2, new2, old2 = links_on(tpl.format(n=2), pattern)
        fresh2 = [u for u in p2 if u.rstrip("/") not in seen]
        if len(fresh2) < 5:
            continue
        p5, new5, old5 = links_on(tpl.format(n=5), pattern)
        fresh5 = [u for u in p5 if u.rstrip("/") not in seen and u not in p2]
        result["working"].append({
            "template": tpl, "page2_new": len(fresh2), "page5_new": len(fresh5),
            "page2_dates": [new2, old2], "page5_dates": [new5, old5],
            "page5_sample": fresh5[:2] or fresh2[:2],
        })
        if len(fresh5) >= 5:
            break
    return result


def main():
    cfg = json.load(open(os.path.join(ROOT, "config", "sites.json"), encoding="utf-8"))
    sites = [s for s in cfg["sites"] if s.get("enabled", True)]
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {s["name"]: pool.submit(probe, s) for s in sites}
    wait(futures.values(), timeout=480)
    results = [f.result() if f.done() and not f.exception() else
               {"site": n, "error": "timed out" if not f.done() else str(f.exception())[:150]}
               for n, f in futures.items()]
    for r in results:
        print(r["site"], [w["template"] for w in r.get("working", [])], r.get("error", ""), flush=True)
    with open(os.path.join(ROOT, "data", "paging.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os._exit(0)


if __name__ == "__main__":
    main()

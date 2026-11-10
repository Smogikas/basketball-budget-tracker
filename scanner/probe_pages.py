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


def sitemaps_of(site):
    """Sitemaps listed in robots.txt, with how many links and which dates they cover."""
    from urllib.parse import urlparse
    host = urlparse(site.get("section") if site.get("section", "").startswith("http") else site["url"])
    out = []
    try:
        robots = bounded_get(S, f"{host.scheme}://{host.netloc}/robots.txt", budget=10).text
    except Exception:
        return out
    for sm in re.findall(r"(?im)^sitemap:\s*(\S+)", robots)[:6]:
        try:
            r = bounded_get(S, sm, budget=20, max_bytes=8_000_000)
            soup = BeautifulSoup(r.content, "xml")
        except Exception:
            continue
        children = [c.find("loc").text.strip() for c in soup.find_all("sitemap") if c.find("loc")]
        links, newest, oldest = links_on(sm, site.get("link_pattern", ""))
        out.append({"sitemap": sm, "links": len(links), "dates": [newest, oldest],
                    "children": children[:12], "children_total": len(children)})
    return out


def probe(site):
    pattern = site.get("link_pattern", "")
    section = site.get("section") or site["url"]
    feed_links, f_new, f_old = links_on(site["url"], pattern)
    if " " in section or section == site["url"]:
        sec_links = feed_links
    else:
        sec_links = links_on(section, pattern)[0]
    result = {"site": site["name"], "feed_links": len(feed_links), "feed_dates": [f_new, f_old],
              "section_links": len(sec_links), "working": [], "sitemaps": sitemaps_of(site)}
    for tpl in candidates(site):
        base = feed_links if tpl.startswith(site["url"]) else sec_links
        seen = set(u.rstrip("/") for u in base)
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
    only = [x.strip().lower() for x in os.environ.get("PROBE_ONLY", "").split(",") if x.strip()]
    if only:   # e.g. "sporx, walla" = only sites whose name starts with these
        sites = [s for s in sites if any(s["name"].lower().startswith(o) for o in only)]
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {s["name"]: pool.submit(probe, s) for s in sites}
    wait(futures.values(), timeout=500)
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

#!/usr/bin/env python3
"""One-off helper: print the pagination links ("next", page numbers, "load more")
found on each page in config/probe_urls.txt. Run from "Probe sites" (mode: links)."""
import os, re, sys
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import USER_AGENT, bounded_get  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = requests.Session()
S.headers.update({"User-Agent": USER_AGENT})
for url in open(os.path.join(ROOT, "config", "probe_urls.txt")).read().split():
    print("=====", url, flush=True)
    try:
        r = bounded_get(S, url, budget=20)
    except Exception as e:
        print("  error", e); continue
    print("  status", r.status_code, r.url)
    soup = BeautifulSoup(r.text, "lxml")
    for el in soup.select('[class*="pag" i], [id*="pag" i], [class*="more" i], [class*="mas" i], link[rel="next"], a[rel="next"]'):
        print("  BLOCK", el.name, el.get("class"), el.get("id"), str(el)[:400].replace("\n", " "))
    for a in soup.find_all("a", href=True):
        h, t = a["href"], a.get_text(" ", strip=True)[:40]
        if re.search(r"pag|page|p=|/\d{1,3}/?$|siguiente|anterior|mas|more", h + " " + t, re.I):
            print("  A", repr(t), urljoin(r.url, h))
    for m in soup.find_all("meta"):
        if re.search(r"date|time|publish", str(m), re.I):
            print("  META", str(m)[:200])
    for t in soup.find_all("time"):
        print("  TIME", str(t)[:200])
    for m in re.findall(r".{60}(?:datePublished|dateCreated|uploadDate)[^<]{0,80}", r.text)[:5]:
        print("  LD", m)
    for m in re.findall(r"[^<>]{0,60}\b\d{1,2}[/.-]\d{1,2}[/.-]20\d\d\b[^<>]{0,40}|[^<>]{0,40}\b\d{1,2} de [a-z]+ de 20\d\d[^<>]{0,40}", r.text)[:6]:
        print("  DATE", m.strip())
    for m in re.findall(r".{80}(?:page|pagina|offset|loadMore|cargar)[^<]{0,120}", r.text, re.I)[:0]:
        print("  JS", m.replace("\n", " "))

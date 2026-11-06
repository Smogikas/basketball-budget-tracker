#!/usr/bin/env python3
"""Daily keyword scanner for basketball budget and salary articles.

No AI is used. For every website in config/sites.json it finds new articles,
keeps the ones that contain budget or salary words from config/keywords.json,
and compares the team and player names in them with the public CSV export of
the "Basketball" Google Sheet, so the page can show what is already logged.

Output: data/articles.json (read by index.html) and data/seen.json (URLs
already checked, so an article is never listed twice).
"""

import csv
import datetime as dt
import email.utils
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")

SHEET_ID = "1Qt4ijFGDmvoLLHb-gCbcAF9KGts6ZC-RHwWzQw68cZI"
BUDGET_TABS = [
    ("Budgets 26/27", "269873761", "26/27"),
    ("Budgets 25/26", "1153494097", "25/26"),
    ("Historical Budgets", "1365128025", None),
]
PLAYER_TAB = "207447642"

USER_AGENT = (
    "Mozilla/5.0 (compatible; basketball-budget-tracker/1.0; "
    "+https://github.com/Smogikas/basketball-budget-tracker)"
)
REQUEST_DELAY = 1.0      # seconds between requests to the same website
KEEP_ITEMS_DAYS = 45     # how long a found article stays on the page
KEEP_SEEN_DAYS = 120     # how long a checked URL is remembered

# Words removed from team names to get a short alias ("BC Wolves" -> "wolves").
GENERIC_TEAM_WORDS = {
    "bc", "kk", "bk", "kb", "ks", "sc", "cb", "fc", "cd", "ac", "as", "ab", "sk",
    "bkk", "kos", "mks", "club", "basket", "basketball", "basketbol", "baloncesto",
    "pallacanestro", "kosarka", "kosarkaski", "krepsinio", "korvpall", "koripallo",
    "team", "sport", "sports", "the", "de", "del", "la", "le", "les", "and",
    "spor", "kulubu", "athletic", "association", "union", "olympique", "real",
}
# Short aliases that are too common to mean a team on their own.
ALIAS_STOPWORDS = {
    "union", "elite", "stars", "basket", "sporting", "national", "city", "royal",
    "france", "spain", "italy", "germany", "greece", "turkey", "poland", "serbia",
    "lithuania", "latvia", "estonia", "israel", "europe", "academy",
    "lions", "bears", "united", "dragons", "giants", "eagles", "tigers", "sharks",
    "rockets", "kings", "warriors", "heroes", "titans", "energy", "capital",
    "telekom", "basketball", "baskets", "women", "juniors", "youth",
}

SPECIAL_CHARS = str.maketrans({
    "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "ss",
    "ı": "i", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d", "’": "'", "\xa0": " ",
})

AMOUNT_RE = re.compile(
    r"(?:[€$£]\s?\d[\d.,]*(?:\s\d{3})*(?:\s?(?:m|mln|million\w*|millones|milioni|milijon\w*|k|tūkst\.?|thousand|mil)\b)?"
    r"|\d[\d.,]*(?:\s\d{3})*\s?(?:€|eur\b|euro\w*|eurų|\$|usd\b|dollars?\b|£|m€|k€|mln\.?|million\w*|millones|milioni|milijon\w*|tūkst\.?|thousand\b|mil\b))",
    re.IGNORECASE,
)
SEASON_RE = re.compile(r"\b(?:20)?(\d{2})\s?[/–-]\s?(?:20)?(\d{2})\b")


# ----------------------------------------------------------------- helpers

def norm(text):
    """Lowercase, remove accents and punctuation, collapse spaces."""
    if not text:
        return ""
    text = text.translate(SPECIAL_CHARS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text.lower())
    return text.strip()


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def parse_date(value):
    """Parse RSS/ISO dates; returns an aware datetime or None."""
    if not value:
        return None
    value = value.strip()
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
            if not m:
                return None
            d = dt.datetime.fromisoformat(m.group(1))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


class Fetcher:
    """requests session with robots.txt checks and a per-site delay."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en,fr,es,it,de,lt,*;q=0.5",
        })
        self.robots = {}
        self.last_hit = {}

    def allowed(self, url):
        host = urlparse(url).netloc
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(f"{urlparse(url).scheme}://{host}/robots.txt", timeout=15)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host].can_fetch(USER_AGENT, url)

    def get(self, url):
        host = urlparse(url).netloc
        wait = REQUEST_DELAY - (time.time() - self.last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.time()
        r = self.session.get(url, timeout=25)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r


# --------------------------------------------------------- the sheet (CSV)

def fetch_csv(fetcher, gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    r = fetcher.session.get(url, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    if r.text.lstrip().startswith("<"):
        raise RuntimeError("the sheet is not public (got a sign-in page)")
    return list(csv.reader(io.StringIO(r.text)))


def find_col(header, *starts):
    for i, cell in enumerate(header):
        n = norm(cell)
        if any(n.startswith(s) for s in starts):
            return i
    return None


def parse_budgets(rows, tab, default_season):
    """Budget tabs are split into league sections, each with its own header."""
    out, cols, section = [], None, ""
    for row in rows:
        cells = [c.strip() for c in row]
        filled = [c for c in cells if c]
        if not filled:
            continue
        normed = [norm(c) for c in cells]
        if "club" in normed and "nation" in normed:
            cols = {
                "nation": normed.index("nation"),
                "division": find_col(cells, "division"),
                "club": normed.index("club"),
                "budget": find_col(cells, "total budget"),
                "salary": find_col(cells, "gross salary", "salary budget"),
            }
            continue
        if len(filled) == 1 and not re.search(r"\d{3}", filled[0]):
            section = filled[0]
            cols = None  # every league section has its own header row
            continue
        if not cols:
            continue
        club = cells[cols["club"]] if cols["club"] < len(cells) else ""
        if not club or norm(club) in ("club", "total", "average"):
            continue
        m = re.search(r"(\d{2})\s?/\s?(\d{2})", section)
        season = default_season or (f"{m.group(1)}/{m.group(2)}" if m else "")

        def cell(key):
            i = cols.get(key)
            return cells[i] if i is not None and i < len(cells) else ""

        out.append({
            "team": club, "country": cell("nation"), "division": cell("division"),
            "budget": cell("budget"), "salary": cell("salary"),
            "tab": tab, "section": section, "season": season,
        })
    return out


def parse_players(rows):
    out, cols = [], None
    for row in rows:
        cells = [c.strip() for c in row]
        normed = [norm(c) for c in cells]
        if cols is None:
            if "name" in normed and "team" in normed and "season" in normed:
                cols = {
                    "role": find_col(cells, "role"), "name": normed.index("name"),
                    "country": find_col(cells, "country"), "team": normed.index("team"),
                    "season": normed.index("season"),
                    "contract": find_col(cells, "contract value"),
                    "length": find_col(cells, "contract length"),
                }
            continue

        def cell(key):
            i = cols.get(key)
            return cells[i] if i is not None and i < len(cells) else ""

        if cell("name"):
            out.append({k: cell(k) for k in cols})
    return out


class SheetIndex:
    """Lookup of team and player names that appear in the sheet."""

    def __init__(self, budgets, players):
        self.budgets, self.players = budgets, players
        self.team_alias = {}     # alias -> set of canonical team names
        self.team_rows = {}      # canonical -> budget rows
        self.team_country = {}
        self.player_rows = {}    # normalized name -> player rows
        self.player_count = {}   # (team norm, season) -> number of player rows
        for b in budgets:
            canon = b["team"]
            self.team_rows.setdefault(canon, []).append(b)
            if b["country"]:
                self.team_country.setdefault(canon, b["country"])
            for alias in self.aliases(canon):
                self.team_alias.setdefault(alias, set()).add(canon)
        for p in players:
            n = norm(p["name"])
            if len(n.split()) >= 2:
                self.player_rows.setdefault(n, []).append(p)
            key = (norm(p["team"]), p["season"])
            self.player_count[key] = self.player_count.get(key, 0) + 1
            if p["team"] and p["team"] not in self.team_rows:
                for alias in self.aliases(p["team"]):
                    self.team_alias.setdefault(alias, set()).add(p["team"])
                if p.get("country"):
                    self.team_country.setdefault(p["team"], p["country"])
        self.max_words = max([len(a.split()) for a in self.team_alias] +
                             [len(a.split()) for a in self.player_rows] + [1])

    @staticmethod
    def aliases(team):
        full = norm(team)
        out = {full} if len(full) >= 4 else set()
        short = " ".join(w for w in full.split() if w not in GENERIC_TEAM_WORDS)
        if len(short) >= 5 and short not in ALIAS_STOPWORDS and not short.isdigit():
            out.add(short)
        # single distinctive words, so "Chalon" finds "Elan Chalon"
        for w in short.split():
            if len(w) >= 5 and w not in ALIAS_STOPWORDS and not w.isdigit():
                out.add(w)
        return out

    def match(self, text):
        words = norm(text).split()
        grams = set()
        for n in range(1, self.max_words + 1):
            for i in range(len(words) - n + 1):
                grams.add(" ".join(words[i:i + n]))
        teams = set()
        for g in grams & self.team_alias.keys():
            teams |= self.team_alias[g]
        players = sorted(grams & self.player_rows.keys())
        return sorted(teams), players

    def team_summary(self, team):
        rows = self.team_rows.get(team, [])
        lines = []
        for r in sorted(rows, key=lambda r: r["season"], reverse=True)[:3]:
            parts = []
            if r["budget"]:
                parts.append(f"budget {r['budget']}")
            if r["salary"]:
                parts.append(f"salary budget {r['salary']}")
            label = r["season"] or r["section"]
            lines.append(f"{label}: " + (", ".join(parts) if parts else "row exists, no amounts"))
        if not rows:
            lines.append("not in budget tabs")
        counts = [(s, c) for (t, s), c in self.player_count.items() if t == norm(team)]
        if counts:
            counts.sort(reverse=True)
            lines.append("player rows: " + ", ".join(f"{s} ({c})" for s, c in counts[:3]))
        return lines

    def player_summary(self, name_norm):
        rows = sorted(self.player_rows[name_norm], key=lambda r: r["season"], reverse=True)
        lines = []
        for r in rows[:4]:
            amount = r.get("contract") or "no amount"
            length = f" ({r['length']})" if r.get("length") else ""
            lines.append(f"{r['season']} {r['team']}: {amount}{length}")
        return rows[0]["name"], lines


def load_sheet(fetcher, warnings):
    budgets, players = [], []
    for tab, gid, season in BUDGET_TABS:
        try:
            budgets += parse_budgets(fetch_csv(fetcher, gid), tab, season)
        except Exception as e:  # keep scanning even if the sheet fails
            warnings.append(f"Could not read sheet tab {tab}: {e}")
    try:
        players = parse_players(fetch_csv(fetcher, PLAYER_TAB))
    except Exception as e:
        warnings.append(f"Could not read sheet tab Player Salaries: {e}")
    print(f"Sheet: {len(budgets)} budget rows, {len(players)} player rows")
    return SheetIndex(budgets, players)


# ---------------------------------------------------- finding new articles

def discover(fetcher, site):
    """Return a list of (url, published datetime or None)."""
    kind = site.get("type", "rss")
    r = fetcher.get(site["url"])
    found = []
    if kind == "rss":
        soup = BeautifulSoup(r.content, "xml")
        for item in soup.find_all(["item", "entry"]):
            link = item.find("link")
            url = (link.get("href") or link.text).strip() if link else ""
            date_tag = item.find(["pubDate", "published", "updated", "dc:date", "date"])
            found.append((url, parse_date(date_tag.text) if date_tag else None))
    elif kind == "sitemap":
        soup = BeautifulSoup(r.content, "xml")
        children = soup.find_all("sitemap")
        if children:  # sitemap index: read the newest few sitemaps
            children.sort(key=lambda s: s.find("lastmod").text if s.find("lastmod") else "", reverse=True)
            for child in children[:3]:
                found += discover(fetcher, {**site, "url": child.find("loc").text.strip()})
        for u in soup.find_all("url"):
            loc = u.find("loc")
            date_tag = u.find(["publication_date", "news:publication_date", "lastmod"])
            if loc:
                found.append((loc.text.strip(), parse_date(date_tag.text) if date_tag else None))
    else:  # listing page
        soup = BeautifulSoup(r.text, "lxml")
        host = urlparse(site["url"]).netloc.replace("www.", "")
        for a in soup.find_all("a", href=True):
            url = urljoin(site["url"], a["href"]).split("#")[0]
            if urlparse(url).netloc.replace("www.", "") == host:
                found.append((url, None))
    pattern = site.get("link_pattern")
    if pattern:
        found = [(u, d) for u, d in found if re.search(pattern, u)]
    seen, unique = set(), []
    for u, d in found:
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            unique.append((u, d))
    return unique


def read_article(fetcher, url):
    r = fetcher.get(url)
    soup = BeautifulSoup(r.text, "lxml")
    meta = lambda *names: next((m.get("content") for n in names
                                for m in [soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})]
                                if m and m.get("content")), None)
    title = meta("og:title", "twitter:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "") \
        or (soup.title.get_text(strip=True) if soup.title else url)
    published = parse_date(meta("article:published_time", "datePublished", "date", "pubdate"))
    if not published and soup.find("time", datetime=True):
        published = parse_date(soup.find("time", datetime=True)["datetime"])
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure"]):
        tag.decompose()
    container = soup.find("article")
    paragraphs = container.find_all(["p", "li", "h2", "h3"]) if container else []
    text = "\n".join(p.get_text(" ", strip=True) for p in paragraphs)
    if len(text) < 300:
        text = "\n".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    description = meta("og:description", "description") or ""
    return title.strip(), published, (description + "\n" + text).replace("\xa0", " ")


# ------------------------------------------------------ keyword matching

def compile_keywords(words):
    return [(w, re.compile(r"(?<![a-z0-9])" + re.escape(norm(w)))) for w in words if norm(w)]


def analyse(title, text, kw):
    """Return the keyword hits, snippets, amounts and seasons, or None."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 20]
    hits = {"Budget": set(), "Salary": set()}
    snippets, amounts = [], []
    for i, sentence in enumerate([title] + sentences):
        n = norm(sentence)
        matched = False
        for kind, patterns in (("Budget", kw["budget"]), ("Salary", kw["salary"])):
            for word, rx in patterns:
                if rx.search(n):
                    hits[kind].add(word)
                    matched = True
        if matched:
            if i > 0 and len(snippets) < 3:
                snippets.append(sentence if len(sentence) <= 300 else sentence[:297] + "…")
            context = sentence + " " + (sentences[i] if i < len(sentences) else "")
            for a in AMOUNT_RE.findall(context):
                a = a.strip(" .,")
                if a and a not in amounts and re.search(r"\d", a):
                    amounts.append(a)
    if not hits["Budget"] and not hits["Salary"]:
        return None
    seasons = []
    for a, b in SEASON_RE.findall(title + " " + text):
        if (int(a) + 1) % 100 == int(b) and f"{a}/{b}" not in seasons:
            seasons.append(f"{a}/{b}")
    return {
        "type": [k for k in ("Budget", "Salary") if hits[k]],
        "keywords": sorted(hits["Budget"] | hits["Salary"]),
        "snippets": snippets, "amounts": amounts[:8], "seasons": seasons[:5],
    }


# ------------------------------------------------------------------- main

def main():
    sites_cfg = load_json(os.path.join(CONFIG, "sites.json"), {})
    kw_cfg = load_json(os.path.join(CONFIG, "keywords.json"), {})
    kw = {"budget": compile_keywords(kw_cfg.get("budget", [])),
          "salary": compile_keywords(kw_cfg.get("salary", []))}
    exclude = [norm(w) for w in kw_cfg.get("exclude_if_in_title_or_url", [])]
    flags = [norm(w) for w in kw_cfg.get("flag_if_in_text", [])]
    max_per_site = sites_cfg.get("max_articles_per_site", 40)
    max_age = dt.timedelta(days=sites_cfg.get("max_age_days", 3))

    out_path = os.path.join(DATA, "articles.json")
    seen_path = os.path.join(DATA, "seen.json")
    previous = load_json(out_path, {"items": []})
    seen = load_json(seen_path, {})
    now = now_utc()
    today = now.date().isoformat()

    fetcher = Fetcher()
    warnings, run = [], []
    sites = [s for s in sites_cfg.get("sites", []) if s.get("enabled", True)]
    sheet = load_sheet(fetcher, warnings) if sites else SheetIndex([], [])
    new_items = []

    for site in sites:
        stats = {"site": site["name"], "checked": 0, "found": 0, "error": ""}
        run.append(stats)
        try:
            if not fetcher.allowed(site["url"]):
                raise RuntimeError("robots.txt does not allow reading this page")
            candidates = discover(fetcher, site)
        except Exception as e:
            stats["error"] = str(e)[:200]
            print(f"[{site['name']}] ERROR {e}", file=sys.stderr)
            continue
        candidates = [(u, d) for u, d in candidates
                      if u not in seen and (d is None or now - d <= max_age)]
        for url, listed_date in candidates[:max_per_site]:
            seen[url] = today
            if any(re.search(r"(?<![a-z0-9])" + re.escape(x) + r"(?![a-z0-9])", norm(url)) for x in exclude):
                continue
            if not fetcher.allowed(url):
                continue
            try:
                title, published, text = read_article(fetcher, url)
            except Exception as e:
                print(f"[{site['name']}] could not read {url}: {e}", file=sys.stderr)
                continue
            stats["checked"] += 1
            title_n = norm(title)
            if any(re.search(r"(?<![a-z0-9])" + re.escape(x) + r"(?![a-z0-9])", title_n) for x in exclude):
                continue
            result = analyse(title, text, kw)
            if not result:
                continue
            teams, players = sheet.match(title + "\n" + text)
            team_info = [{"name": t, "country": sheet.team_country.get(t, ""),
                          "sheet": sheet.team_summary(t)} for t in teams[:6]]
            player_info = []
            for p in players[:8]:
                name, lines = sheet.player_summary(p)
                player_info.append({"name": name, "sheet": lines})
            country = next((t["country"] for t in team_info if t["country"]), "") or site.get("country", "")
            date = published or listed_date
            text_n = " " + norm(text) + " "
            new_items.append({
                "id": hashlib.sha1(url.encode()).hexdigest()[:12],
                "url": url, "title": title, "site": site["name"], "country": country,
                "published": date.date().isoformat() if date else "",
                "found": today,
                **result,
                "teams": team_info, "players": player_info,
                "nba": any(f" {f} " in text_n for f in flags),
            })
            stats["found"] += 1
        print(f"[{site['name']}] checked {stats['checked']}, found {stats['found']}")

    cutoff = (now - dt.timedelta(days=KEEP_ITEMS_DAYS)).date().isoformat()
    known = {i["id"] for i in new_items}
    items = new_items + [i for i in previous.get("items", [])
                         if i["id"] not in known and i.get("found", "") >= cutoff]
    items.sort(key=lambda i: (i.get("found", ""), i.get("published", "")), reverse=True)

    seen_cutoff = (now - dt.timedelta(days=KEEP_SEEN_DAYS)).date().isoformat()
    seen = {u: d for u, d in seen.items() if d >= seen_cutoff}

    save_json(out_path, {"updated": now.isoformat(timespec="minutes"), "warnings": warnings,
                         "run": run, "items": items})
    save_json(seen_path, dict(sorted(seen.items())))
    print(f"Done: {len(new_items)} new, {len(items)} on the page")


if __name__ == "__main__":
    main()

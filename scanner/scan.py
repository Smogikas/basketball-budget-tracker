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
import socket
import sys
import threading
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
    "latvijas", "lietuvos", "golden", "orange", "maccabi", "hapoel", "olympic",
    "universitates", "universidad", "university", "school", "college",
}

SPECIAL_CHARS = str.maketrans({
    "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "ss",
    "ı": "i", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d", "’": "'", "\xa0": " ",
})
# Serbian / Russian Cyrillic -> Latin, so Latin keywords and team names match.
CYRILLIC = dict(zip("абвгдежзиклмнопрстуфхцчшјћђ", "abvgdezziklmnoprstufhccsjcd"))
CYRILLIC.update({"џ": "dz", "љ": "lj", "њ": "nj", "й": "j", "ы": "y", "э": "e", "ю": "ju",
                 "я": "ja", "щ": "sc", "ъ": "", "ь": "", "ё": "e", "є": "e", "і": "i", "ї": "i"})
CYRILLIC.update({k.upper(): v for k, v in list(CYRILLIC.items())})
SPECIAL_CHARS.update(str.maketrans(CYRILLIC))

AMOUNT_RE = re.compile(
    r"(?:[€$£]\s?\d[\d.,]*(?:\s\d{3})*(?:\s?(?:m|mln|million\w*|millones|milioni|milijon\w*|k|tūkst\.?|thousand|mil)\b)?"
    r"|\d[\d.,]*(?:\s\d{3})*\s?(?:€|eur\b|euro\w*|eurų|\$|usd\b|dollars?\b|£|m€|k€|mln\.?|million\w*|millones|milioni|milijon\w*|tūkst\.?|thousand\b|mil\b"
    r"|milion\w*|miliard\w*|evra\b|evro\b|hiljad\w*|tisíc\w*|tis\.|tys\.|miljon\w*|miljoen\w*|bin\b|milyon\w*|lira\b|tl\b"
    r"|ευρώ|εκατ\w*|χιλ\w*|евра|евро|милион\w*|хиљад\w*|מיליון|אלף|דולר|יורו|ש\"ח|₪))",
    re.IGNORECASE,
)
SEASON_RE = re.compile(r"\b(?:20)?(\d{2})\s?[/–-]\s?(?:20)?(\d{2})\b")


# ----------------------------------------------------------------- helpers

def norm(text):
    """Lowercase, remove accents and punctuation, collapse spaces.
    Greek, Cyrillic and Hebrew letters are kept (accents removed)."""
    if not text:
        return ""
    text = text.translate(SPECIAL_CHARS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\W_]+", " ", text.lower().replace("ς", "σ"))
    return text.strip()


NOT_ARTICLE = re.compile(
    r"/(tag|tags|category|categories|kategorija|author|autor|authors|page|search|login|"
    r"register|video|videos|gallery|galerija|photo|foto|live|tv|wp-content|wp-admin|"
    r"komentari|comments|comentarios|kommentare|yorumlar)(/|$)|"
    r"\.(jpg|jpeg|png|gif|webp|svg|pdf|css|js|xml|mp4)$", re.I)


def article_like(url, page_url):
    """Guess whether a link on a news page points to an article."""
    u, p = urlparse(url), urlparse(page_url)
    if u.scheme not in ("http", "https"):
        return False
    if u.netloc.replace("www.", "") != p.netloc.replace("www.", ""):
        return False
    path = u.path.rstrip("/")
    if not path or path == p.path.rstrip("/") or NOT_ARTICLE.search(path):
        return False
    last = path.rsplit("/", 1)[-1]
    slug = last.count("-") >= 3 and len(last) >= 20
    numbered = re.search(r"\d{4,}", path + "?" + u.query) and (len(path) >= 15 or u.query)
    return bool(slug or numbered)


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


class Page:
    """Downloaded page: a small stand-in for requests.Response."""

    def __init__(self, status_code, url, content, encoding):
        self.status_code, self.url, self.content = status_code, url, content
        self.encoding = encoding or "utf-8"

    @property
    def text(self):
        return self.content.decode(self.encoding, errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")


def _cut_connection(response):
    """Called by the watchdog: shut the socket so a blocked read returns."""
    for path in (("raw", "_connection", "sock"), ("raw", "_fp", "fp", "raw", "_sock")):
        obj = response
        try:
            for attr in path:
                obj = getattr(obj, attr)
            obj.shutdown(socket.SHUT_RDWR)
            return
        except Exception:
            continue


def bounded_get(session, url, budget=20, max_bytes=5_000_000):
    """GET with a hard time and size limit, so a slow site can't hang the run."""
    with session.get(url, timeout=(6, 10), stream=True, allow_redirects=True) as r:
        watchdog = threading.Timer(budget, _cut_connection, args=(r,))
        watchdog.daemon = True
        watchdog.start()
        chunks, size = [], 0
        try:
            for chunk in r.iter_content(16384):
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    break
        except Exception:
            if not chunks:
                raise RuntimeError(f"too slow, gave up after {budget}s: {url}")
        finally:
            watchdog.cancel()
        content = b"".join(chunks)
        encoding = r.encoding
        if not encoding or encoding.lower() == "iso-8859-1":
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4000], re.I)
            encoding = m.group(1).decode() if m else "utf-8"
        return Page(r.status_code, r.url, content, encoding)


class Fetcher:
    """requests session with robots.txt checks and a per-site delay."""

    def __init__(self, delay=None):
        self.delay = REQUEST_DELAY if delay is None else delay
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
                r = bounded_get(self.session, f"{urlparse(url).scheme}://{host}/robots.txt", budget=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host].can_fetch(USER_AGENT, url)

    def get(self, url):
        host = urlparse(url).netloc
        wait = self.delay - (time.time() - self.last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.time()
        r = bounded_get(self.session, url)
        r.raise_for_status()
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
        weak = {}               # single-word alias -> teams
        for b in budgets:
            canon = b["team"]
            self.team_rows.setdefault(canon, []).append(b)
            if b["country"]:
                self.team_country.setdefault(canon, b["country"])
            strong, single = self.aliases(canon)
            for alias in strong:
                self.team_alias.setdefault(alias, set()).add(canon)
            for alias in single:
                weak.setdefault(alias, set()).add(canon)
        for p in players:
            n = norm(p["name"])
            if len(n.split()) >= 2:
                self.player_rows.setdefault(n, []).append(p)
            key = (norm(p["team"]), p["season"])
            self.player_count[key] = self.player_count.get(key, 0) + 1
            if p["team"] and p["team"] not in self.team_rows:
                strong, single = self.aliases(p["team"])
                for alias in strong:
                    self.team_alias.setdefault(alias, set()).add(p["team"])
                for alias in single:
                    weak.setdefault(alias, set()).add(p["team"])
                if p.get("country"):
                    self.team_country.setdefault(p["team"], p["country"])
        # "Chalon" -> Elan Chalon is useful; "Bulls" -> 3 different clubs is not.
        self.weak_aliases = set()
        for alias, teams in weak.items():
            if len(teams) == 1 and alias not in self.team_alias:
                self.team_alias[alias] = teams
                self.weak_aliases.add(alias)
        self.max_words = max([len(a.split()) for a in self.team_alias] +
                             [len(a.split()) for a in self.player_rows] + [1])

    @staticmethod
    def aliases(team):
        """(full-name aliases, single-word aliases) for a team name."""
        full = norm(team)
        strong = {full} if len(full) >= 4 else set()
        short = " ".join(w for w in full.split() if w not in GENERIC_TEAM_WORDS)
        if len(short) >= 5 and short not in ALIAS_STOPWORDS and not short.isdigit():
            strong.add(short)
        # single distinctive words, so "Chalon" finds "Elan Chalon"
        single = {w for w in short.split()
                  if len(w) >= 5 and w not in ALIAS_STOPWORDS and not w.isdigit()} - strong
        return strong, single

    def match(self, text):
        words = norm(text).split()
        grams = set()
        for n in range(1, self.max_words + 1):
            for i in range(len(words) - n + 1):
                grams.add(" ".join(words[i:i + n]))
        hits = grams & self.team_alias.keys()
        weak_hits = hits & self.weak_aliases
        if weak_hits:
            # a one-word alias counts only when written with a capital letter
            # ("Pazar" the club, not "pazar" = Sunday in Turkish)
            capitalised = {norm(w) for w in re.findall(r"\w+", text) if w[:1].isupper()}
            hits -= weak_hits - capitalised
        teams = set()
        for g in hits:
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

def discover(fetcher, site, url=None):
    """Article links on one page: a feed, a sitemap or an HTML section page
    (detected from the content). Returns a list of (url, published or None)."""
    r = fetcher.get(url or site["url"])
    head = r.content[:800].lower()
    found = []
    if b"<rss" in head or b"<feed" in head or b"<rdf" in head:
        soup = BeautifulSoup(r.content, "xml")
        for item in soup.find_all(["item", "entry"]):
            link = item.find("link")
            u = (link.get("href") or link.text).strip() if link else ""
            date_tag = item.find(["pubDate", "published", "updated", "dc:date", "date"])
            found.append((u, parse_date(date_tag.text) if date_tag else None))
    elif b"<urlset" in head or b"<sitemapindex" in head:
        soup = BeautifulSoup(r.content, "xml")
        children = soup.find_all("sitemap")
        if children:  # sitemap index: read the newest few sitemaps
            children.sort(key=lambda c: c.find("lastmod").text if c.find("lastmod") else "", reverse=True)
            for child in children[:3]:
                found += discover(fetcher, site, child.find("loc").text.strip())
        for u in soup.find_all("url"):
            loc = u.find("loc")
            date_tag = u.find(["publication_date", "news:publication_date", "lastmod"])
            if loc:
                found.append((loc.text.strip(), parse_date(date_tag.text) if date_tag else None))
    else:  # HTML section page
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            u = urljoin(r.url, a["href"]).split("#")[0]
            if article_like(u, r.url):
                found.append((u, None))
        # same article linked with and without a trailing slash -> keep one
        found = list({u.rstrip("/"): (u, d) for u, d in found}.values())
    pattern = site.get("link_pattern")
    if pattern:
        found = [(u, d) for u, d in found if re.search(pattern, u)]
    seen, unique = set(), []
    for u, d in found:
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            unique.append((u, d))
    return unique


def page_urls(site, first_page=1, backfill=False):
    """Page 1 is the site's feed/section; older pages use its next_page template.
    In a backfill, a site's backfill_url (e.g. a big sitemap with dates, or a
    list of monthly sitemaps) is used instead when it has one."""
    if backfill and site.get("backfill_url"):
        sources = site["backfill_url"]
        sources = [sources] if isinstance(sources, str) else sources
        for n, src in enumerate(sources, start=1):
            if n >= first_page:
                yield n, src
        return
    n = first_page
    while True:
        if n == 1:
            yield 1, site["url"]
        elif site.get("next_page"):
            yield n, site["next_page"].format(n=n)
        else:
            return
        n += 1


def main_text(root):
    """Text of the element that holds the most paragraph text: the article
    body, not sidebars or 'latest news' lists."""
    scores = {}
    for p in root.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(t) >= 40:
            scores[id(p.parent)] = scores.get(id(p.parent), (p.parent, 0))
            scores[id(p.parent)] = (p.parent, scores[id(p.parent)][1] + len(t))
    if not scores:
        return ""
    best = max(scores.values(), key=lambda x: x[1])[0]
    return "\n".join(p.get_text(" ", strip=True) for p in best.find_all(["p", "h2", "h3"]))


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
    text = main_text(soup.find("article") or soup)
    if len(text) < 200 and soup.find("article"):
        text = main_text(soup)
    description = meta("og:description", "description") or ""
    return title.strip(), published, (description + "\n" + text).replace("\xa0", " ")


# ------------------------------------------------------ keyword matching

def latin_share(text):
    """Share of letters that are Latin after normalising (Cyrillic counts as Latin)."""
    letters = [c for c in norm(text) if c.isalpha()]
    return sum("a" <= c <= "z" for c in letters) / len(letters) if letters else 1.0


def compile_keywords(words):
    """'word' = start of a word, 'word$' = whole word, '~word' = anywhere,
    '+word' = counts only when the same sentence also has a sum of money."""
    out = []
    for w in words:
        weak = w.startswith("+")
        w = w.lstrip("+")
        anywhere, whole = w.startswith("~"), w.endswith("$")
        core = norm(w.strip("~$"))
        if not core:
            continue
        rx = re.escape(core)
        if not anywhere:
            rx = r"(?<!\w)" + rx + (r"(?!\w)" if whole else "")
        out.append((w.strip("~$"), re.compile(rx), weak))
    return out


def any_match(patterns, text_norm):
    return [w for w, rx, *_ in patterns if rx.search(text_norm)]


def analyse(title, text, kw):
    """Return the keyword hits, snippets, amounts and seasons, or None."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 20]
    hits = {"Budget": set(), "Salary": set(), "Amount": set()}
    word_snips, money_snips, amounts, context_parts = [], [], [], []
    for i, sentence in enumerate([title] + sentences):
        n = norm(sentence)
        found = set()
        money = [w for w, rx, _ in kw.get("money", []) if rx.search(n)]
        if AMOUNT_RE.search(sentence):          # "€1.2M", "$300,000", "400 000 eurų"
            money.append("sum of money")
        if money:
            hits["Amount"].update(money)
            found.add("Amount")
        n_salary = n
        for rx in kw.get("salary_ignore", []):  # "l'ingaggio di X" = the signing of X
            n_salary = rx.sub(" ", n_salary)
        for kind, key in (("Budget", "budget"), ("Salary", "salary")):
            for word, rx, weak in kw.get(key, []):
                if rx.search(n_salary if key == "salary" else n) and (money or not weak):
                    hits[kind].add(word)
                    found.add(kind)
        if not found:
            continue
        if i > 0:
            snip = sentence if len(sentence) <= 300 else sentence[:297] + "…"
            (word_snips if found - {"Amount"} else money_snips).append(snip)
        context = sentence + " " + (sentences[i] if i < len(sentences) else "")
        context_parts.append(context)
        for a in AMOUNT_RE.findall(context):
            a = a.strip(" .,")
            if a and a not in amounts and re.search(r"\d", a):
                amounts.append(a)
    if not any(hits.values()):
        return None
    seasons = []
    for a, b in SEASON_RE.findall(title + " " + text):
        if (int(a) + 1) % 100 == int(b) and f"{a}/{b}" not in seasons:
            seasons.append(f"{a}/{b}")
    return {
        "type": [k for k in ("Budget", "Salary", "Amount") if hits[k]],
        "keywords": sorted(hits["Budget"] | hits["Salary"] | hits["Amount"]),
        # sentences with budget/salary words first, then sentences with only a sum
        "snippets": (word_snips + money_snips)[:3], "amounts": amounts[:8], "seasons": seasons[:5],
        "_context": "\n".join(context_parts),
    }


# ------------------------------------------------------------------- main

SITE_TIME_LIMIT = 300        # seconds per website (regular run)
RUN_TIME_LIMIT = 1200        # seconds for the whole regular run (workflow limit is 30 min)
BACKFILL_SITE_TIME = 2400    # seconds per website in one backfill chunk
BACKFILL_RUN_TIME = 2600     # seconds per backfill chunk


class Rules:
    def __init__(self, kw_cfg):
        self.kw = {"budget": compile_keywords(kw_cfg.get("budget", [])),
                   "salary": compile_keywords(kw_cfg.get("salary", [])),
                   "money": compile_keywords(kw_cfg.get("money", [])),
                   "salary_ignore": [re.compile(r"(?<!\w)" + re.escape(norm(p)) + r"(?!\w)")
                                     for p in kw_cfg.get("salary_ignore_phrases", []) if norm(p)]}
        self.nba_team_words = compile_keywords([w if w.endswith("$") else w + "$"
                                                for w in kw_cfg.get("nba_team_words", [])])
        self.flags = compile_keywords(kw_cfg.get("flag_if_in_text", []))
        self.basketball_words = compile_keywords(kw_cfg.get("basketball_words", []))
        self.skip_title = compile_keywords(kw_cfg.get("skip_if_in_title", []))
        self.nba_names = compile_keywords(kw_cfg.get("nba_team_names", []))

    def is_nba_team(self, team):
        n = norm(team)
        return bool(any_match(self.nba_names, n) or any_match(self.nba_team_words, n))


def process_article(site, url, listed_date, fetcher, rules, sheet, settings, stats):
    """Read one article. Returns (item or None, published date or None)."""
    try:
        title, published, text = read_article(fetcher, url)
    except Exception as e:
        print(f"[{site['name']}] could not read {url}: {e}", file=sys.stderr, flush=True)
        return None, None
    stats["checked"] += 1
    date = published or listed_date
    if date and date < settings["since"]:
        return None, date                     # older than the period we look at
    if any_match(rules.skip_title, norm(title)):
        return None, date
    text_n = norm(title + "\n" + text)
    # General news sites: the article must be about basketball.
    if site.get("general") and not any_match(rules.basketball_words, text_n):
        return None, date
    result = analyse(title, text, rules.kw)
    if not result:
        return None, date
    # Skip articles about NBA teams/contracts: an NBA team in the title
    # and no team from your sheet in the title ("Lakers sign X").
    # "Former NBA player joins Rytas" is kept.
    title_teams = [t for t in sheet.match(title)[0] if not rules.is_nba_team(t)]
    if any_match(rules.nba_team_words, norm(title) + " " + norm(url)) and not title_teams:
        stats["skipped_nba"] = stats.get("skipped_nba", 0) + 1
        return None, date
    context = result.pop("_context")
    teams, players = sheet.match(title + "\n" + context)
    # NBA salary story with no non-NBA club near the money words -> skip
    # (e.g. "Porzingis could lose $16M"); "Former NBA player joins Rytas" stays.
    nba = bool(any_match(rules.flags, text_n)) or any(rules.is_nba_team(t) for t in teams)
    # Greek/Hebrew text can't match the Latin team names in the sheet,
    # so for those articles only the NBA label is set.
    if nba and latin_share(title) > 0.5 and not [t for t in teams if not rules.is_nba_team(t)]:
        stats["skipped_nba"] = stats.get("skipped_nba", 0) + 1
        return None, date
    if not teams and not players:
        teams, players = sheet.match(title + "\n" + text)
        teams, players = teams[:3], players[:3]
    team_info = [{"name": t, "country": sheet.team_country.get(t, ""),
                  "sheet": sheet.team_summary(t)} for t in teams[:6]]
    player_info = []
    for p in players[:8]:
        name, lines = sheet.player_summary(p)
        player_info.append({"name": name, "sheet": lines})
    country = next((t["country"] for t in team_info if t["country"]), "") or site.get("country", "")
    item = {
        "id": hashlib.sha1(url.encode()).hexdigest()[:12],
        "url": url, "title": title, "site": site["name"], "country": country,
        "published": date.date().isoformat() if date else "",
        "found": settings["today"],
        **result,
        "teams": team_info, "players": player_info, "nba": nba,
    }
    return item, date


def scan_site(site, rules, sheet, seen, settings, state=None):
    """Check one website, newest page first.

    Normal run: stop at the first page with nothing new (everything on it was
    checked before) - so only new articles are read.
    Backfill run: keep going to older pages until articles are older than the
    start date, then mark the site done; the page reached is saved in `state`
    so the next chunk continues there.
    Returns (new items, stats, URLs checked)."""
    started = time.time()
    backfill = settings["backfill"]
    fetcher = Fetcher(delay=site.get("delay", REQUEST_DELAY))
    stats = {"site": site["name"], "checked": 0, "found": 0, "error": "", "pages": 0,
             "oldest": "", "stop": ""}
    items, checked_urls, done_here = [], [], set()
    first_page = state.get("page", 1) if (backfill and state) else 1
    try:
        if not fetcher.allowed(site["url"]):
            raise RuntimeError("robots.txt does not allow reading this page")
    except Exception as e:
        stats["error"] = stats["stop"] = str(e)[:200]
        if backfill and state is not None:
            state.update(done=True, stop=stats["stop"])
        return items, stats, checked_urls
    oldest = None
    for page_no, page_url in page_urls(site, first_page, backfill):
        if page_no - first_page >= settings["max_pages"]:
            stats["stop"] = "page limit"
            break
        if time.time() - started > settings["site_time"]:
            stats["stop"] = "time limit (continues next run)"
            break
        history_source = backfill and bool(site.get("backfill_url"))
        try:
            links = discover(fetcher, site, page_url)
        except Exception as e:
            if history_source:                 # e.g. a monthly sitemap that doesn't exist
                print(f"[{site['name']}] skipped {page_url}: {e}", file=sys.stderr, flush=True)
                continue
            if page_no == 1:
                stats["error"] = str(e)[:200]
            if re.search(r"HTTP (429|503)", str(e)):
                stats["stop"] = f"site asked to slow down on page {page_no} (continues next run)"
            else:
                stats["stop"] = f"page {page_no} failed: {str(e)[:80]}"
            break
        if history_source:
            # sitemaps list the whole archive: only dated links in the period
            links = [(u, d) for u, d in links if d and d >= settings["since"]]
        stats["pages"] += 1
        fresh = [(u, d) for u, d in links if u not in seen and u not in done_here
                 and not (d and d < settings["since"])]
        if history_source:
            if backfill and state is not None:
                state["page"] = page_no        # resume at this sitemap next chunk
        elif not links or (page_no > 1 and not fresh and not backfill):
            stats["stop"] = "no more pages" if not links else "reached articles checked before"
            break
        page_dates = [d for _, d in links if d]
        stopped_by_time = False
        for url, listed_date in fresh:
            if stats["checked"] >= settings["max_per_site"]:
                stats["stop"] = "article limit"
                break
            if time.time() - started > settings["site_time"]:
                stopped_by_time = True
                break
            if listed_date and listed_date < settings["since"]:
                continue
            done_here.add(url)
            checked_urls.append(url)
            if not fetcher.allowed(url):
                continue
            item, date = process_article(site, url, listed_date, fetcher, rules, sheet, settings, stats)
            if date:
                page_dates.append(date)
            if item:
                items.append(item)
                stats["found"] += 1
        if page_dates:
            page_oldest = min(page_dates)
            oldest = page_oldest if not oldest else min(oldest, page_oldest)
        if stopped_by_time:
            stats["stop"] = "time limit (continues next run)"
            break                              # this page is redone next chunk
        if stats["stop"] == "article limit":
            break
        if backfill and state is not None:
            state["page"] = page_no + 1
        if history_source:
            continue                           # read every history sitemap listed
        # whole page older than the start date -> nothing older is needed
        if page_dates and sorted(page_dates)[len(page_dates) // 2] < settings["since"]:
            stats["stop"] = "reached start date"
            break
        if not fresh and not backfill:
            stats["stop"] = "reached articles checked before"
            break
    else:
        stats["stop"] = stats["stop"] or "no older pages"
    if backfill and site.get("backfill_url") and stats["stop"] in ("", "no older pages"):
        stats["stop"] = "all history sitemaps read"
    if not stats["stop"]:
        stats["stop"] = "no older pages"
    stats["oldest"] = oldest.date().isoformat() if oldest else ""
    if backfill and state is not None:
        state["done"] = "continues next run" not in stats["stop"]
        state["stop"], state["oldest"] = stats["stop"], stats["oldest"] or state.get("oldest", "")
    print(f"[{site['name']}] pages {stats['pages']}, {stats['checked']} checked, {stats['found']} found, "
          f"oldest {stats['oldest']}, stop: {stats['stop']} ({time.time() - started:.0f}s)", flush=True)
    return items, stats, checked_urls


def main():
    from concurrent.futures import ThreadPoolExecutor, wait

    sites_cfg = load_json(os.path.join(CONFIG, "sites.json"), {})
    rules = Rules(load_json(os.path.join(CONFIG, "keywords.json"), {}))
    out_path = os.path.join(DATA, "articles.json")
    seen_path = os.path.join(DATA, "seen.json")
    previous = load_json(out_path, {"items": []})
    seen = load_json(seen_path, {})
    now = now_utc()
    since_env = os.environ.get("BACKFILL_SINCE", "").strip()
    backfill = bool(since_env)
    settings = {"now": now, "today": now.date().isoformat(), "backfill": backfill}
    if backfill:   # one-off history run, in chunks; progress in data/backfill.json
        settings.update(since=dt.datetime.fromisoformat(since_env).replace(tzinfo=dt.timezone.utc),
                        max_per_site=100000, max_pages=400, site_time=BACKFILL_SITE_TIME)
    else:          # regular run: only new articles
        settings.update(since=now - dt.timedelta(days=sites_cfg.get("max_age_days", 3)),
                        max_per_site=sites_cfg.get("max_articles_per_site", 150),
                        max_pages=sites_cfg.get("max_pages", 10), site_time=SITE_TIME_LIMIT)
    state_path = os.path.join(DATA, "backfill.json")
    bstate = load_json(state_path, {}) if backfill else {}
    if backfill and bstate.get("since") != since_env:
        bstate = {"since": since_env, "sites": {}}

    warnings = []
    sites = [s for s in sites_cfg.get("sites", []) if s.get("enabled", True)]
    sheet = load_sheet(Fetcher(), warnings) if sites else SheetIndex([], [])

    # Different websites are read at the same time; each one slowly, one page at a time.
    pool = ThreadPoolExecutor(max_workers=8)
    if backfill:
        sites = [s for s in sites if not bstate["sites"].get(s["name"], {}).get("done")]
        for s in sites:
            bstate["sites"].setdefault(s["name"], {"page": 1})
    futures = [(s, pool.submit(scan_site, s, rules, sheet, seen, settings,
                               bstate["sites"][s["name"]] if backfill else None)) for s in sites]
    wait([f for _, f in futures], timeout=BACKFILL_RUN_TIME if backfill else RUN_TIME_LIMIT)
    new_items, run = [], []
    for site, f in futures:
        if f.done() and not f.exception():
            items, stats, checked = f.result()
            new_items += items
            for u in checked:
                seen[u] = settings["today"]
        else:
            error = "did not finish in time" if not f.done() else str(f.exception())[:200]
            stats = {"site": site["name"], "checked": 0, "found": 0, "error": error}
        run.append(stats)

    cutoff = (now - dt.timedelta(days=KEEP_ITEMS_DAYS)).date().isoformat()
    known = {i["id"] for i in new_items}
    removed = set(load_json(os.path.join(DATA, "removed.json"), []))   # deleted with the "Delete rejected rows" workflow
    items = new_items + [i for i in previous.get("items", [])
                         if i["id"] not in known and i.get("found", "") >= cutoff]
    items = [i for i in items if i["id"] not in removed]
    items.sort(key=lambda i: (i.get("found", ""), i.get("published", "")), reverse=True)

    seen_cutoff = (now - dt.timedelta(days=KEEP_SEEN_DAYS)).date().isoformat()
    seen = {u: d for u, d in seen.items() if d >= seen_cutoff}

    save_json(out_path, {"updated": now.isoformat(timespec="minutes"), "warnings": warnings,
                         "run": run, "items": items})
    save_json(seen_path, dict(sorted(seen.items())))
    if backfill:
        remaining = [n for n, st in bstate["sites"].items() if not st.get("done")]
        bstate["remaining"] = remaining
        save_json(state_path, bstate)
        print(f"Backfill: {len(remaining)} sites not finished yet: {remaining}", flush=True)
    print(f"Done: {len(new_items)} new, {len(items)} on the page", flush=True)
    os._exit(0)  # don't wait for a thread stuck on a hanging site


if __name__ == "__main__":
    main()

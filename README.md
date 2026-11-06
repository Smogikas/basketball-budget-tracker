# Budget Article Finder

Every day this project reads the newest articles on the basketball websites listed in
`config/sites.json`, keeps the ones that mention a **budget**, **salary budget** or
**player salary** (found by keywords, no AI), and shows them on a page:

**https://smogikas.github.io/basketball-budget-tracker/**

For each article the page shows the date, country, teams, the sentences with the keywords,
any amounts (€, mln, k…), and what your Basketball Google Sheet already has for the teams
and players named in it. You open the source, decide yourself, and add data to the sheet by hand.
**Reject** hides a row (saved in your browser only).

Nothing is written to the Google Sheet. The scanner only reads its public CSV export.

## How it works

1. GitHub Actions runs `scanner/scan.py` every day at 04:23 UTC (`.github/workflows/scan.yml`).
   You can also run it any time: **Actions → Daily scan → Run workflow**.
2. For each enabled site it reads the RSS feed, sitemap, or news listing page, and opens
   articles from the last `max_age_days` days that it hasn't checked before (`data/seen.json`).
3. Articles whose title or URL contains `nba`, `wnba`, `g league`… are skipped. If an article
   only mentions the NBA in the text, it is kept and marked "NBA mentioned".
4. Articles with budget or salary keywords go to `data/articles.json`, which the page reads.
   Only links and short snippets are stored, not whole articles, so the repo stays small.

## Adding a website

Edit `config/sites.json` on GitHub (pencil icon) and add an entry:

```json
{ "name": "BeBasket", "country": "France", "type": "rss",
  "url": "https://www.bebasket.fr/rss.xml", "link_pattern": "", "enabled": true }
```

- `type`: `rss` (best), `sitemap` (e.g. a news sitemap .xml), or `listing` (a news page;
  then set `link_pattern` to a regular expression that article links match, e.g. `/news/\\d+`).
- `country`: used when no team from your sheet is recognised in the article.

## Changing keywords

Edit `config/keywords.json`. Words match the start of a word, ignoring case and accents, so
one entry covers every ending (`biudzet` → biudžetas, biudžetą, biudžeto, biudžetui…).
`word$` = only that exact word (all forms listed), `~word` = anywhere in a word (Hebrew).

- `budget` words → **Budget** label, `salary` words → **Salary** label
- `money` words (euro, dollar, million, thousand in each language) and any € $ £ ₪ next to a
  number → **Amount** label. Use the type filter "Budget or Salary" on the page to hide
  articles that only mention a sum of money.

## Notes

- The sheet must stay shared as "Anyone with the link can view", or the "In your sheet"
  column will be empty (the page will show a warning).
- Sites that block automated readers show an error at the top of the page.
- GitHub pauses scheduled workflows in repositories with no activity for 60 days. The daily
  commits normally count as activity; if it ever stops, re-enable it in the Actions tab.

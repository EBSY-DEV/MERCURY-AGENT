# Prospecting

Mercury's prospecting runs in stages: **discover** which businesses exist, **profile** what their websites say about them, find a **person** and an address, **verify** the address, then hand verified prospects to the Writer. Every fact gathered along the way is stored as a signal observation, and only signals you have confirmed are collected. This page explains the signal model, the cohort builder, each discovery provider and what it costs, profiling, email finding, and exporting the list.

## Signals

A signal is one kind of fact about a business: "runs Google Ads", "website built by agency X", "ranks 14th for roofing Denver", "no online booking". Mercury proposes; you confirm.

- **The vocabulary** lives in the `signal_codes` table. Mercury seeds it from the catalog in `mercury/signals.py` with every signal set to `proposed`. Seeding never overrides a decision you made, and new signals shipped in an upgrade simply appear as `proposed`.
- **Only `confirmed` signals are collected.** Collectors ask for the confirmed set and skip everything else. A database trigger rejects any observation whose code is not in the vocabulary, so a typo fails loudly instead of inventing a junk signal.
- **Every fact is a row, not a column.** Each observation in the `observations` table records the company, signal code, a numeric or text value, a confidence, an evidence URL, the collector and run that produced it, and when it was observed. A new signal needs no migration, re-observing a site later produces a time series ("they dropped their agency last quarter"), and provenance travels with the fact.

### The catalog

| Category | Signals | Cost |
|---|---|---|
| Discovery | `FOUND_IN_SERP`, `SERP_RANK`, `NO_WEBSITE`, `UNCLAIMED_LISTING`, `REVIEW_RATING`, `REVIEW_COUNT` | Included in the discovery call. `SERP_RANK` requires a paid SERP provider. |
| Profile | `INCUMBENT_AGENCY`, `RUNNING_GOOGLE_ADS`, `RUNNING_META_ADS`, `TECH_STACK`, `NO_SCHEMA_MARKUP`, `NO_ONLINE_BOOKING`, `SITE_PAGE_COUNT`, `BLOG_STALE`, `BLOCKS_AI_CRAWLERS`, `HIRING_ROLE` | Free (homepage, robots.txt, sitemap.xml, careers page). |
| People | `CONTACT_FOUND`, `DECISION_MAKER_TITLE`, `REGISTRY_VERIFIED`, `LIKELY_OWNER` | Free. |
| Verification | `EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL` | `EMAIL_STATUS` uses a verification credit; the others are free or nearly so. |

`INCUMBENT_AGENCY` is only recorded when the footer credit wording is explicit ("Website by X"), because a wrong incumbent in an email is worse than none.

### Confirming

```bash
mercury signals                          # the catalog, grouped, with company counts
mercury signals --confirm free           # every signal whose cost note starts with "free" or "included"
mercury signals --confirm SERP_RANK,EMAIL_STATUS
mercury signals --confirm all
mercury signals --reject BLOG_STALE,BLOCKS_AI_CRAWLERS
```

`--confirm free` deliberately excludes `SERP_RANK` and `EMAIL_STATUS`, whose cost notes mention credits. The dashboard's Signals tab does the same, one signal or a whole category at a time.

Discovery refuses to run while no signal is confirmed, and Today shows a "signals waiting for your confirmation" item until you have decided.

## The cohort builder

At the bottom of the Signals tab, pick the signals a good prospect must have and any that disqualify them. Mercury counts matching companies live and lists up to 200 of them.

A company "has" a signal when its most recent observation of that signal is present and not zero, so a business that stopped running ads drops out. The intersection is computed in SQL.

The cohort builder is a view over your data. It does not currently restrict which companies the Scout works on.

## Discovery providers

Discovery is the only stage that spends money, so every run estimates first, refuses to start if the estimate exceeds your cap, re-checks actual spend between queries, and reads a stop switch between queries.

```bash
mercury discover --providers                     # the menu: cost, free tier, setup state
mercury discover --estimate                      # projected spend, then exit
mercury discover                                 # default provider: osm (free)
mercury discover --provider dataforseo_listings --max-spend 2.00
mercury discover --city "Denver, CO;Boulder, CO" --limit 50
mercury discover --provider serper --depth 30
mercury discover --no-profile                    # skip the free profiling step
```

| Flag | Default | Meaning |
|---|---|---|
| `--provider` | `osm` | Provider key from the table below. |
| `--providers` | | List providers and exit. |
| `--estimate` | | Print queries and projected cost, call nothing. |
| `--city` | `icp.geography` | Semicolon-separated, because "Denver, CO" contains a comma. |
| `--depth` | `30` | SERP depth (results per search). Ignored by listings providers. |
| `--limit` | `100` | Max records per query. |
| `--max-spend` | `1.00` | Hard cap in dollars. |
| `--no-profile` | | Don't profile what was found. |

| Provider (`key`) | Kind | Cost (from the code) | Free tier | Needs | Best for |
|---|---|---|---|---|---|
| OpenStreetMap (`osm`) | listings | free | unlimited, within Overpass etiquette | nothing | Trying the whole pipeline with no account. |
| Google Maps, self-hosted (`google_maps`) | listings | free; runs a scraper on your machine | unlimited | a running [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper); `GMAPS_SCRAPER_URL` if not on `127.0.0.1:8085` | Markets OSM barely covers. Scraping Google Maps is against Google's terms; keep jobs small. Drops listings with fewer than 3 reviews. |
| DataForSEO Business Listings (`dataforseo_listings`) | listings | $0.012 per query + $0.00036 per business (about $0.372 per 1,000) | $1 signup credit; $50 minimum deposit to go live | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` | Local trades, clinics, contractors: phone, domain, rating, claimed status, and businesses with no website at all. |
| DataForSEO SERP (`dataforseo_serp`) | serp | $0.0006 per 10 results ($0.0018 at depth 30) | $1 credit; free sandbox | same as above | Rank as the buying signal. |
| Serper (`serper`) | serp | estimated at $0.001 per 10 results; roughly $0.30-$1.00 per 1,000 searches depending on plan | 2,500 queries, no card | `SERPER_API_KEY` | The easiest paid source to try. Paid plans start at a $50 prepaid pack; credits expire after 6 months. |
| Semrush competitors (`semrush_competitors`) | serp | about 1,200 API units per sector, plus one Serper search for the seed | depends on your Semrush plan | `SEMRUSH_API_KEY` (and Serper for the seed) | Finding a sector by its organic competitors, with traffic per domain. Finds sectors rather than cities. |

Run `mercury discover --providers` for the live list and whether each one is configured. Set `DATAFORSEO_SANDBOX=1` to try DataForSEO against dummy data without being charged.

### Queries and places

Discovery builds one query per industry per place, from `icp.industries` x `icp.geography`. If `icp.markets` is set, each market searches its own `terms` in its own `places` instead (see [Configuration](configuration.md#icp)).

Listings providers search a radius, so each place needs coordinates. Put them in `icp.geo_coordinates` (`"Denver, CO": "39.7392,-104.9903,50"`), or let Mercury geocode the city once through OpenStreetMap Nominatim (40 km radius, cached permanently). Country-level entries have no useful centre; use cities.

### What a run does

For each query: fetch results, drop junk (directories, marketplaces, social networks, media, `.edu`/`.gov`, hospital systems, website builders, aggregators, all matched by pattern), resolve the business by its normalised domain (or the provider's own id when there is no website), insert new companies, and write observations for confirmed signals. Observations are flushed after every query, so a run that dies keeps what it learned. Every run is logged in the `runs` table with its cost, shown as "Collector runs" on Today.

A run stops early when the stop switch is set (the Stop button on the Discover tab), when actual spend passes `--max-spend`, or when the estimate alone is over the cap.

### SERP depth: 20-30, not 100

Google removed 100-results-per-page in September 2025, so depth 100 is now billed as ten pages at most providers. Positions 11-30 are the useful band (visible enough to be trying, not yet winning), and below 30 it is mostly directories. The default depth is 30.

### The OpenStreetMap caveat

OSM maps physical premises, so service-area businesses (roofers, HVAC, plumbers) are badly under-represented: it holds under 2,000 roofers for the entire US. Use it to try the pipeline end to end and as a cross-reference, not as your only list.

### Daily background discovery

Running a provider from the dashboard's Discover tab also selects it for a daily background run: while `mercury run` is up, the heartbeat starts discovery for that provider once every 24 hours with a $1.00 cap and 40 results per query. The Stop button ends only the run in progress; the next daily run starts as scheduled. If you selected a paid provider and don't want recurring spend, run `osm` once from the Discover tab to make the free source the selection. `mercury discover` on the command line does not change the selection.

## Profiling

Profiling reads what a business publishes about itself: the homepage, `robots.txt`, `sitemap.xml`, and the team and careers pages when present. It costs nothing: a few HTTP requests per company, no browser, no model call. From that HTML it records the profile and people signals in the catalog above.

It runs automatically after every discovery, rides along on every heartbeat for up to 25 companies, and can be run by hand:

```bash
mercury profile                       # up to 200 companies
mercury profile --limit 50 --stale-days 30
```

A company is profiled when it has a domain and has never been profiled, or was last profiled more than `--stale-days` ago (default 90). Re-reading on a schedule is what turns a signal into a trend.

## Finding people and addresses

The Scout turns companies into prospects through several strategies, each isolated so one failing does not stop the others:

- **Inbox sweep** (every cycle): for discovered companies that have a website but no contact yet, look for an address the company publishes on its own site, verify it, and create a prospect. The batch is sized to the verifier credits you have left and pauses when none remain.
- **Web search and team pages**: search for companies matching your ICP and read their team and about pages for named people. Search tries Serper, then Tavily (when keys are set), then DuckDuckGo, Bing and Google scraping. Free search is heavily rate limited, especially from datacenter IPs.
- **Job boards** (optional): with `pip install python-jobspy`, Mercury looks for companies hiring for roles in `icp.hiring_signals` (or `icp.titles`), the strongest in-market signal. Skipped when the package is not installed.
- **LinkedIn** (optional): browser automation with Playwright when `channels.linkedin.enabled` is true and `LINKEDIN_EMAIL` is set. This violates LinkedIn's terms of service and can get the account restricted.

While reading sites, Mercury also detects the tools a site runs (HubSpot, Shopify, Intercom and about 35 others) from the HTML it already fetched, at no extra request. Claude only scores and personalizes contacts that Python already found; it does not do the searching.

For each named person, the address is found pattern-first and verified once. See [Address verification](email-and-deliverability.md#address-verification) for the order and the `verified` / `risky` / `guess` / `invalid` statuses. Only sendable prospects (verified, plus risky with `send_to_risky`) are handed to the Writer.

## Exporting the list

Mercury is useful as a list builder even if you never let it send.

```bash
mercury export                                   # verified + risky addresses -> prospects.csv
mercury export --out leads.csv --min-score 7
mercury export --email-status verified
mercury export --status new,queued
mercury export --all                             # everything, no filters
```

Columns: `email, first_name, last_name, company_name, title, website, linkedin_url, industry, personalization, email_status, score, seniority, source, status, created_at`. The first columns match what Instantly, Smartlead and similar sequencers expect, so the file imports without remapping.

The dashboard's Contacts tab has "Export deliverable CSV" and "Export all" buttons, and Today has an Export prospects shortcut.

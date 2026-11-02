# Current Events Digest — feed intake

Replaces Blogtrottr as the input to the Current Events Digest. Every hour a GitHub Action fetches the feeds in `feeds.json`, drops anything already seen, and commits the new items. The digest (a Claude scheduled task) clones this repo read-only, lists the items in its window, and does the ranking, enrichment, writing and sending. No AI runs here; this part costs no tokens.

## What's in the repo

| Path | What it is |
|---|---|
| `feeds.json` | The feed list (64 feeds: the Blogtrottr export of 2026-11-02, less Cassandra Unchained). Edit this to add or remove a source. |
| `intake.py` | The hourly job. Python standard library only. |
| `items/YYYY-MM-DD.jsonl` | New items, one JSON record per line, filed by the UTC date first seen. Kept 30 days. |
| `state/meta.json` | `generated_at`: the time of the last successful intake run. The digest uses it as its window end. |
| `state/feeds.json` | Per-feed status: last success, last new item, errors. Drives Feed health. |
| `state/seen.json` | Dedupe index, kept 90 days. |
| `tools/digest.py` | What the digest runs: `window`, `list`, `show`, `health`. |

## An item record

```json
{"id":"3f9a1c0b2e","seen":"2026-11-02T19:07:12Z","pub":"2026-11-02T18:40:00Z",
 "src":"FT US","group":"FT","kind":"editorial","feeds":["ft-us","ft-markets"],
 "title":"…","by":"…","summary":"… (≤500 chars)","url":"https://www.ft.com/content/…"}
```

`src` is the label the digest prints under "Surfaced by"; `group` is the publisher that counts once per story (FT's seven feeds, WSJ's three, Reuters' four, and so on). `feeds` lists every feed in the group that carried the item in the same run. `url` is the real publisher link, with tracking parameters removed. Reuters items come through Google News, so their `url` is a Google News redirect and they have no summary.

## Dedupe rules

An item is new only if none of these has been seen in the last 90 days: its normalized URL; its guid within the publisher group; its normalized title within the publisher group. Items published more than 48 hours before the intake first saw them are recorded as seen but not emitted (first-run backlogs, feeds that resurface old posts). Blank titles and Reuters stock-quote pages are dropped.

## Setup

1. Create the empty public repo `rettyoung/current-events-intake` and push these files.
2. Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.
3. Actions → *Feed intake* → **Run workflow**. It should go green and commit `items/` and `state/`.

The first run only marks the feeds' current contents as seen and emits items from the last 48 hours.

## When something goes wrong

- **One feed fails** (a 403, a timeout, bad XML): the run carries on. The failure is recorded in `state/feeds.json`, and the digest reports it under Feed health once it has failed three runs in a row.
- **More than half the feeds fail**: the run still commits what it got, then exits with an error so GitHub emails you.
- **The run doesn't happen**: GitHub sometimes delays or skips scheduled runs. The digest reads `generated_at`, so a late run shifts items into the next digest rather than losing them. The digest flags the intake as stale when `generated_at` is more than 6 hours old.
- GitHub pauses schedules on repos with no activity for 60 days. The hourly commits should count as activity; if the schedule is ever paused, re-enable it in the Actions tab.

#!/usr/bin/env python3
"""Current Events Digest intake.

Runs hourly in GitHub Actions. Fetches every feed in feeds.json, drops items it
has already seen, and appends the new ones to items/YYYY-MM-DD.jsonl (UTC date
the item was first seen). Python standard library only.

Every item written in one run carries the same "seen" time, the run's start.
state/meta.json records that time as "generated_at"; the digest uses it as the
end of its window, so an item can never fall between two digests.

Files written:
  items/YYYY-MM-DD.jsonl  one JSON record per new item
  state/seen.json         dedupe index {key: first-seen date}
  state/feeds.json        per-feed status (feed health)
  state/meta.json         generated_at and run totals
"""
import concurrent.futures as cf
import email.utils
import gzip
import hashlib
import html
import html.entities
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDS = os.path.join(ROOT, "feeds.json")
ITEMS = os.path.join(ROOT, "items")
STATE = os.path.join(ROOT, "state")

BACKFILL_HOURS = 48      # drop items published this long before we first saw them
SEEN_KEEP_DAYS = 90      # dedupe memory
ITEMS_KEEP_DAYS = 30     # item shards older than this are deleted
SUMMARY_CHARS = 500
TIMEOUT = 25
WORKERS = 12
UA = ("Mozilla/5.0 (compatible; current-events-intake/1.0; "
      "+https://github.com/rettyoung/current-events-intake)")

DROP_PARAMS = re.compile(r"^(utm_|smid$|smtyp$|ref$|ref_src$|oc$|cmpid$|mod$|"
                         r"traffic_source$|partner$|rss$|src$|at_medium$|at_campaign$|cmp$|cmpid$)", re.I)
SKIP_TITLE = re.compile(r"stock price & latest news|^(ap news|reuters\.com)$", re.I)
JUNK = [re.compile(p, re.I | re.S) for p in (
    r"\bThe post .{0,300}? appeared first on .{0,120}?\.\s*$",
    r"\bContinue reading\.*\s*$", r"\bRead more\.*\s*$",
    r"\[(?:…|\.\.\.|&#8230;)\]\s*$", r"Media files:.*$")]


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def text(s):
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", " ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(html.unescape(s))
    return re.sub(r"\s+", " ", s).strip()


def clean_summary(s, title):
    s = text(s)
    for j in JUNK:
        s = j.sub("", s).strip()
    if not s or s.lower().startswith(title.lower()[:60]) and len(s) < len(title) + 40:
        return ""                       # Google News style: summary is just the headline
    if len(s) > SUMMARY_CHARS:
        s = s[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return s


def norm_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    p = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not DROP_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", p.netloc.lower(), path, urllib.parse.urlencode(q), ""))


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def key(*parts):
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


# ---------- fetching and parsing ----------

def fetch(feed, cache):
    hdr = {"User-Agent": UA, "Accept-Encoding": "gzip",
           "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"}
    c = cache.get(feed["id"], {})
    if c.get("etag"):
        hdr["If-None-Match"] = c["etag"]
    if c.get("last_modified"):
        hdr["If-Modified-Since"] = c["last_modified"]
    req = urllib.request.Request(feed["url"], headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return r.status, body, r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", c.get("etag"), c.get("last_modified")
        raise


XML_ENTS = {"amp", "lt", "gt", "quot", "apos"}


def repair(raw):
    s = raw.decode("utf-8", "replace").lstrip("﻿ \r\n\t")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|\w+;)", "&amp;", s)

    def ent(m):
        n = m.group(1)
        if n in XML_ENTS:
            return m.group(0)
        cp = html.entities.name2codepoint.get(n)
        return "&#%d;" % cp if cp else "&amp;%s;" % n
    s = re.sub(r"&(\w+);", ent, s)
    s = re.sub(r"^<\?xml[^>]*\?>", "", s)
    return s


def local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_feed(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = ET.fromstring(repair(raw))
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {"title": "", "link": "", "guid": "", "date": "", "desc": "", "content": "", "by": ""}
        for ch in el:
            n = local(ch.tag)
            val = (ch.text or "").strip()
            if n == "title":
                f["title"] = text(val)
            elif n == "link":
                href = ch.get("href")
                if href:
                    if ch.get("rel") in (None, "alternate") and not f["link"]:
                        f["link"] = href.strip()
                elif val and not f["link"]:
                    f["link"] = val
            elif n in ("guid", "id"):
                f["guid"] = val
                if n == "guid" and ch.get("isPermaLink", "true") != "false" and val.startswith("http"):
                    f.setdefault("guid_link", val)
            elif n in ("pubdate", "published", "date", "issued"):
                f["date"] = f["date"] or val
            elif n == "updated" and not f["date"]:
                f["date"] = val
            elif n in ("description", "summary"):
                f["desc"] = f["desc"] or val
            elif n in ("encoded", "content"):
                f["content"] = f["content"] or val
            elif n in ("creator", "author"):
                name = next((g.text for g in ch if local(g.tag) == "name" and g.text), None)
                f["by"] = f["by"] or text(name or val)
        if not f["link"]:
            f["link"] = f.get("guid_link", "")
        out.append(f)
    return out


# ---------- main ----------

def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def dump(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def main():
    os.makedirs(ITEMS, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    feeds = load(FEEDS, {})["feeds"]
    seen = load(os.path.join(STATE, "seen.json"), {})
    status = load(os.path.join(STATE, "feeds.json"), {})
    first_run = not seen
    T = now_utc()
    today = T.strftime("%Y-%m-%d")

    results = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch, f, status): f for f in feeds}
        for fut in cf.as_completed(futs):
            f = futs[fut]
            try:
                results[f["id"]] = ("ok",) + fut.result()
            except Exception as e:
                results[f["id"]] = ("err", getattr(e, "code", None), str(e)[:200])

    new, run_keys = [], {}          # run_keys: dedupe key -> record, to merge feeds within a run
    totals = {"feeds": len(feeds), "ok": 0, "not_modified": 0, "failed": 0,
              "items_in_feeds": 0, "new": 0, "dupes": 0, "backfill": 0, "skipped": 0}
    for f in feeds:
        st = status.setdefault(f["id"], {})
        st.update(src=f["src"], cadence=f["cadence"], kind=f["kind"])
        res = results[f["id"]]
        if res[0] == "err":
            totals["failed"] += 1
            st.update(ok=False, http=res[1], error=res[2], last_try=iso(T),
                      fails=st.get("fails", 0) + 1)
            continue
        _, code, body, etag, lm = res
        st.update(ok=True, http=code, error="", last_try=iso(T), last_ok=iso(T), fails=0,
                  etag=etag or "", last_modified=lm or "")
        if code == 304:
            totals["not_modified"] += 1
            st["new_last_run"] = 0
            continue
        try:
            entries = parse_feed(body)
        except Exception as e:
            totals["failed"] += 1
            st.update(ok=False, error="parse: " + str(e)[:180], fails=1, etag="", last_modified="")
            continue
        totals["ok"] += 1
        st["items_in_feed"] = len(entries)
        totals["items_in_feeds"] += len(entries)
        n_new, newest_pub = 0, None
        for e in entries:
            title = e["title"]
            pub = parse_dt(e["date"])
            if pub and (newest_pub is None or pub > newest_pub):
                newest_pub = pub
            if f["group"] == "Reuters" or "news.google.com" in f["url"]:
                title = re.sub(r"(?:^|\s+)-\s+[^-]{2,40}$", "", title).strip()   # " - Reuters"
            if not title or SKIP_TITLE.search(title):
                totals["skipped"] += 1
                continue
            url = norm_url(e["link"])
            k_url = key("u", url) if url else None
            k_guid = key("g", f["group"], e["guid"]) if e["guid"] else None
            k_title = key("t", f["group"], norm_title(title))
            keys = [k for k in (k_url, k_guid, k_title) if k]
            hit = next((run_keys[k] for k in keys if k in run_keys), None)
            if hit is not None:                       # same story from another feed this run
                if f["id"] not in hit["feeds"]:
                    hit["feeds"].append(f["id"])
                for k in keys:
                    run_keys[k] = hit
                    seen.setdefault(k, today)
                totals["dupes"] += 1
                continue
            if any(k in seen for k in keys):
                for k in keys:
                    seen.setdefault(k, today)
                totals["dupes"] += 1
                continue
            for k in keys:
                seen[k] = today
            if pub and pub < T - timedelta(hours=BACKFILL_HOURS):
                totals["backfill"] += 1
                continue
            rec = {"id": k_title[:10], "seen": iso(T), "pub": iso(pub), "src": f["src"],
                   "group": f["group"], "kind": f["kind"], "feeds": [f["id"]],
                   "title": title, "by": e["by"][:120],
                   "summary": clean_summary(e["desc"] or e["content"], title), "url": url}
            for k in keys:
                run_keys[k] = rec
            new.append(rec)
            n_new += 1
        st["new_last_run"] = n_new
        if n_new:
            st["last_new"] = iso(T)
        elif first_run and newest_pub:
            st.setdefault("last_new", iso(min(newest_pub, T)))

    totals["new"] = len(new)
    if new:
        new.sort(key=lambda r: (r["pub"] or r["seen"]), reverse=True)
        with open(os.path.join(ITEMS, today + ".jsonl"), "a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    seen_cut = (T - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen = {k: d for k, d in seen.items() if d >= seen_cut}
    item_cut = (T - timedelta(days=ITEMS_KEEP_DAYS)).strftime("%Y-%m-%d")
    for name in os.listdir(ITEMS):
        if name.endswith(".jsonl") and name[:10] < item_cut:
            os.remove(os.path.join(ITEMS, name))

    meta = load(os.path.join(STATE, "meta.json"), {})
    meta.update(generated_at=iso(T), first_run=first_run, last_run=totals,
                schema="current-events-intake-v1")
    if not meta.get("started_at"):
        meta["started_at"] = iso(T)
    dump(os.path.join(STATE, "seen.json"), seen)
    dump(os.path.join(STATE, "feeds.json"), status)
    dump(os.path.join(STATE, "meta.json"), meta)

    print("ok" if totals["failed"] <= len(feeds) // 2 else "DEGRADED", json.dumps(totals))
    for fid, st in sorted(status.items()):
        if not st.get("ok"):
            print(f"  failed {fid}: {st.get('http')} {st.get('error')}")
    if totals["failed"] > len(feeds) // 2:
        sys.exit(2)          # more than half the feeds failed: fail the run so GitHub emails


if __name__ == "__main__":
    main()

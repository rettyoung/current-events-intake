#!/usr/bin/env python3
"""Read the intake for the Current Events Digest. Standard library only.

  digest.py window [--since ISO]           window start/end and intake freshness
  digest.py list --since ISO --until ISO   compact listing: id | source | time ET | title — dek
  digest.py show ID [ID ...]               full records (summary, byline, URL, feeds)
  digest.py health [--now ISO]             feed-health flags against each feed's cadence

The window is on "seen" time (when the intake first saw an item), and runs
(since, until]: an item seen exactly at the previous digest's end belongs to
that digest, not this one.
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:                     # no tz database: fall back to UTC labels
    ET_TZ = timezone.utc

THRESH = {"daily": 5, "weekly": 21}   # days without a new item before flagging
DEK = 160
SEED_OVERLAP_H = 12             # first-run items kept if published up to this long before the window


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc) if s else None


def et(d, fmt="%a %H:%M"):
    return d.astimezone(ET_TZ).strftime(fmt) if d else "--"


def et_iso(d):
    return d.astimezone(ET_TZ).isoformat(timespec="minutes")


def meta():
    with open(os.path.join(ROOT, "state", "meta.json")) as fh:
        return json.load(fh)


def items(since, until):
    days = set()
    d = since.date() - timedelta(days=1)
    while d <= until.date() + timedelta(days=1):
        days.add(d.isoformat())
        d += timedelta(days=1)
    seed = meta().get("started_at")          # the intake's first run backfilled 48h
    out, dropped = [], 0
    for path in sorted(glob.glob(os.path.join(ROOT, "items", "*.jsonl"))):
        if os.path.basename(path)[:10] not in days:
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    s = dt(r["seen"])
                    if not since < s <= until:
                        continue
                    if r["seen"] == seed and r["pub"] and dt(r["pub"]) <= since - timedelta(hours=SEED_OVERLAP_H):
                        dropped += 1           # published well before the window: an earlier digest had it
                        continue
                    out.append(r)
    return out, dropped


def cmd_window(a):
    m = meta()
    gen = dt(m["generated_at"])
    now = datetime.now(timezone.utc)
    print(f"intake generated_at: {m['generated_at']}  ({et_iso(gen)})")
    print(f"intake age: {(now - gen).total_seconds() / 3600:.1f} h")
    lr = m.get("last_run", {})
    print(f"last intake run: {lr.get('ok', 0)} ok, {lr.get('not_modified', 0)} unchanged, "
          f"{lr.get('failed', 0)} failed of {lr.get('feeds', 0)} feeds; {lr.get('new', 0)} new items")
    print(f"intake started_at: {m.get('started_at')}")
    if a.since:
        s = dt(a.since)
        print(f"window: {et_iso(s)} -> {et_iso(gen)}  ({(gen - s).total_seconds() / 3600:.1f} h)")


def cmd_list(a):
    since, until = dt(a.since), dt(a.until)
    rs, dropped = items(since, until)
    srcs = sorted({r["group"] for r in rs if r["kind"] == "editorial"})
    wires = sorted({r["group"] for r in rs if r["kind"] == "wire"})
    print(f"# window {et_iso(since)} -> {et_iso(until)}: {len(rs)} items; "
          f"{len(srcs)} editorial sources, {len(wires)} wire sources")
    print(f"# editorial: {', '.join(srcs)}")
    print(f"# wire: {', '.join(wires)}")
    seed = meta().get("started_at")
    if seed and since < dt(seed) <= until:
        print(f"# FIRST INTAKE RUN IN WINDOW ({seed}): its 48h backfill is included, less {dropped} items "
              f"published more than {SEED_OVERLAP_H}h before window start. The previous digest may "
              f"already have run some of these stories.")
    for kind in ("editorial", "wire"):
        sel = [r for r in rs if r["kind"] == kind]
        sel.sort(key=lambda r: (r["group"], r["pub"] or r["seen"]))
        print(f"\n## {kind} ({len(sel)})")
        for r in sel:
            dek = r.get("summary", "")
            if a.no_dek or not dek:
                dek = ""
            else:
                dek = " — " + (dek if len(dek) <= DEK else dek[:DEK].rsplit(" ", 1)[0] + "…")
            n = len(r["feeds"])
            dup = f" [x{n}]" if n > 1 else ""
            print(f"{r['id']} | {r['src']} | {et(dt(r['pub']) or dt(r['seen']))} | {r['title']}{dup}{dek}")


def cmd_show(a):
    want = set(a.ids)
    for path in sorted(glob.glob(os.path.join(ROOT, "items", "*.jsonl")), reverse=True):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    if r["id"] in want:
                        print(json.dumps(r, ensure_ascii=False, indent=1))
                        want.discard(r["id"])
        if not want:
            break
    for i in sorted(want):
        print(f"not found: {i}", file=sys.stderr)


def cmd_health(a):
    now = dt(a.now) if a.now else datetime.now(timezone.utc)
    with open(os.path.join(ROOT, "state", "feeds.json")) as fh:
        st = json.load(fh)
    with open(os.path.join(ROOT, "feeds.json")) as fh:
        feeds = {f["id"]: f for f in json.load(fh)["feeds"]}
    m = meta()
    started = dt(m.get("started_at"))
    flags = 0
    for fid, f in feeds.items():
        s = st.get(fid, {})
        th = THRESH.get(f["cadence"])
        if s.get("fails", 0) >= 3:
            print(f"FETCH FAILING  {fid} ({f['src']}): {s.get('fails')} runs in a row, "
                  f"last ok {s.get('last_ok') or 'never'}: {s.get('http')} {s.get('error')}")
            flags += 1
        if th is None:
            continue
        last = dt(s.get("last_new"))
        ref = last or started
        if ref and now - ref > timedelta(days=th):
            print(f"SILENT  {fid} ({f['src']}, {f['cadence']}): no new items since "
                  f"{et(last, '%Y-%m-%d') if last else 'intake start ' + et(started, '%Y-%m-%d')}"
                  f" — worth checking")
            flags += 1
    unknown = sorted(set(st) - set(feeds))
    if unknown:
        print("STALE STATUS (feed removed from feeds.json): " + ", ".join(unknown))
    if not flags:
        print("all feeds within cadence; no fetch failures")


def main():
    global ROOT
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=os.environ.get("DIGEST_ROOT", ROOT),
                   help="intake repo checkout (default: the repo this script sits in)")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window"); w.add_argument("--since")
    l = sub.add_parser("list"); l.add_argument("--since", required=True)
    l.add_argument("--until", required=True); l.add_argument("--no-dek", action="store_true")
    s = sub.add_parser("show"); s.add_argument("ids", nargs="+")
    h = sub.add_parser("health"); h.add_argument("--now")
    a = p.parse_args()
    ROOT = os.path.abspath(a.root)
    {"window": cmd_window, "list": cmd_list, "show": cmd_show, "health": cmd_health}[a.cmd](a)


if __name__ == "__main__":
    main()

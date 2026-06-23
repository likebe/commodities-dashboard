#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices -> data.json for the dashboard.
Runs on GitHub Actions. Stdlib only.

Per instrument it tries sources in order until one returns data:
  1) Yahoo Finance chart API (near-real-time daily, covers all)   <- preferred (fresh)
  2) FRED fredgraph.csv (authoritative but lags ~1wk; energy only) <- fallback
  3) Stooq daily CSV (metals fallback; may be IP-blocked on cloud)
Whatever a run can't get is logged in data.json["errors"] and the page
falls back to its baked value for that instrument.
TTF (Europe gas) has no reliable free daily feed -> left to baked/approx.
"""
import csv, io, json, sys, datetime, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
LB_TO_T = 2204.62

def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")

# ---------- source parsers -> ascending list of (date, value) ----------
def src_yahoo(symbol, mult=1.0):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=6mo" % symbol
    j = json.loads(http_get(url))
    res = j.get("chart", {}).get("result")
    if not res:
        raise ValueError("yahoo empty")
    r0 = res[0]
    ts = r0.get("timestamp") or []
    closes = r0.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()
        out.append((d, float(c) * mult))
    out.sort(key=lambda x: x[0])
    if not out:
        raise ValueError("yahoo no points")
    return out

def src_fred(series_id):
    text = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series_id)
    out = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if not d or v in (".", ""):
            continue
        try:
            out.append((datetime.date.fromisoformat(d), float(v)))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    if not out:
        raise ValueError("fred no data")
    return out

def src_stooq(symbol, mult=1.0):
    text = http_get("https://stooq.com/q/d/l/?s=%s&i=d" % symbol)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0] or "Date" not in rows[0][0]:
        raise ValueError("stooq blocked/empty")
    ci = rows[0].index("Close")
    out = []
    for row in rows[1:]:
        if len(row) <= ci:
            continue
        try:
            out.append((datetime.date.fromisoformat(row[0]), float(row[ci]) * mult))
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    if not out:
        raise ValueError("stooq no data")
    return out

# ---------- metrics ----------
def val_on_or_before(series, target):
    pick = None
    for d, v in series:
        if d <= target:
            pick = (d, v)
        else:
            break
    return pick

def pct(new, old):
    return None if old in (None, 0) else round((new / old - 1.0) * 100.0, 1)

def compute(series, decimals):
    if not series:
        return None
    last_d, last_v = series[-1]
    w = val_on_or_before(series, last_d - datetime.timedelta(days=7))
    m = val_on_or_before(series, last_d - datetime.timedelta(days=30))
    yb = val_on_or_before(series, datetime.date(last_d.year - 1, 12, 31))
    if yb is None:
        yb = next(((d, v) for d, v in series if d.year == last_d.year), None)
    cutoff = last_d - datetime.timedelta(days=185)
    recent = [[d.isoformat(), round(v, decimals)] for d, v in series if d >= cutoff]
    return {"px_val": round(last_v, decimals), "asof": last_d.isoformat(),
            "w": pct(last_v, w[1] if w else None), "m": pct(last_v, m[1] if m else None),
            "y": pct(last_v, yb[1] if yb else None), "series": recent}

# nmEn -> (decimals, px_tpl, [ (label,url,callable) ... ])
CFG = [
    ("WTI Crude",  2, "~${v}/bbl",  [("Yahoo CL=F","https://finance.yahoo.com/quote/CL=F", lambda: src_yahoo("CL=F")),
                                      ("FRED DCOILWTICO","https://fred.stlouisfed.org/series/DCOILWTICO", lambda: src_fred("DCOILWTICO"))]),
    ("Brent Crude",2, "~${v}/bbl",  [("Yahoo BZ=F","https://finance.yahoo.com/quote/BZ=F", lambda: src_yahoo("BZ=F")),
                                      ("FRED DCOILBRENTEU","https://fred.stlouisfed.org/series/DCOILBRENTEU", lambda: src_fred("DCOILBRENTEU"))]),
    ("US Nat Gas", 2, "~${v}/MMBtu",[("Yahoo NG=F","https://finance.yahoo.com/quote/NG=F", lambda: src_yahoo("NG=F")),
                                      ("FRED DHHNGSP","https://fred.stlouisfed.org/series/DHHNGSP", lambda: src_fred("DHHNGSP"))]),
    ("Gold",       0, "~${v}/oz",   [("Yahoo GC=F","https://finance.yahoo.com/quote/GC=F", lambda: src_yahoo("GC=F")),
                                      ("Stooq XAUUSD","https://stooq.com/q/d/l/?s=xauusd&i=d", lambda: src_stooq("xauusd"))]),
    ("Silver",     2, "~${v}/oz",   [("Yahoo SI=F","https://finance.yahoo.com/quote/SI=F", lambda: src_yahoo("SI=F")),
                                      ("Stooq XAGUSD","https://stooq.com/q/d/l/?s=xagusd&i=d", lambda: src_stooq("xagusd"))]),
    ("LME Copper", 0, "~${v}/t",    [("Yahoo HG=F x2204.62 (LME proxy)","https://finance.yahoo.com/quote/HG=F", lambda: src_yahoo("HG=F", LB_TO_T)),
                                      ("Stooq HG.F x2204.62 (LME proxy)","https://stooq.com/q/d/l/?s=hg.f&i=d", lambda: src_stooq("hg.f", LB_TO_T))]),
]

def fmt_px(tpl, v, decimals):
    return tpl.replace("{v}", ("{:,.%df}" % decimals).format(v))

def build():
    instruments, errors = {}, {}
    for nm, dec, tpl, sources in CFG:
        got = None; used = None; tries = []
        for label, url, fn in sources:
            try:
                series = fn(); c = compute(series, dec)
                if c:
                    got, used = c, (label, url); break
            except Exception as e:
                tries.append("%s: %s" % (label, e))
        if not got:
            errors[nm] = " | ".join(tries) or "no source"
            print("ERR %-12s %s" % (nm, errors[nm]), file=sys.stderr); continue
        d0 = datetime.date.fromisoformat(got["asof"])
        instruments[nm] = {"px": fmt_px(tpl, got["px_val"], dec), "pxEn": fmt_px(tpl, got["px_val"], dec),
                           "asof_iso": got["asof"], "asof": "%d/%d" % (d0.month, d0.day),
                           "w": got["w"], "m": got["m"], "y": got["y"], "yl": "YTD",
                           "series": got["series"], "srcName": used[0], "srcUrl": used[1]}
        print("OK  %-12s %s  via %-28s asof %s (1W %s 1M %s YTD %s) pts=%d"
              % (nm, instruments[nm]["px"], used[0], got["asof"], got["w"], got["m"], got["y"], len(got["series"])))
    out = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "Real daily closes; per-instrument source shown in srcName (Yahoo preferred for freshness, FRED/Stooq fallback). TTF not included (no free daily feed).",
           "instruments": instruments}
    if errors:
        out["errors"] = errors
    return out

def selftest():
    s = src_fred.__wrapped__ if hasattr(src_fred, "__wrapped__") else None
    # parser-only checks with inline samples
    fred = "observation_date,X\n2025-12-31,71.00\n2026-06-22,74.30\n"
  
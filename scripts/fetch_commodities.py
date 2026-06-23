#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices and write data.json for the dashboard.
Runs on GitHub Actions (network OK there). Stdlib only.

Sources (most accurate free):
  WTI       FRED DCOILWTICO        (EIA Cushing spot, $/bbl)
  Brent     FRED DCOILBRENTEU      (Europe Brent spot, $/bbl)
  US Nat Gas FRED DHHNGSP          (Henry Hub spot, $/MMBtu)
  Gold      FRED GOLDAMGBD228NLBM  (LBMA London AM fix, $/oz)
  Silver    Stooq xagusd           (silver spot, $/oz)
  LME Copper Stooq hg.f            (COMEX copper $/lb -> x2204.62 = $/t LME proxy)
TTF (Europe gas) has no reliable free daily feed -> left to the baked/approx value.
"""
import csv, io, json, sys, datetime, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (commodities-dashboard data bot)"}
TODAY = datetime.date.today()

def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")

def parse_fred(text):
    """FRED fredgraph.csv -> list of (date, float) ascending; '.' = missing."""
    out = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if not rows:
        return out
    for row in rows[1:]:
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
    return out

def parse_stooq(text, mult=1.0):
    """Stooq daily CSV (Date,Open,High,Low,Close,Volume) -> list of (date, close*mult)."""
    out = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if not rows or "Date" not in rows[0][0]:
        # stooq returns 'No data' or html on bad symbol
        return out
    hdr = rows[0]
    try:
        ci = hdr.index("Close")
    except ValueError:
        return out
    for row in rows[1:]:
        if len(row) <= ci:
            continue
        try:
            out.append((datetime.date.fromisoformat(row[0]), float(row[ci]) * mult))
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    return out

def val_on_or_before(series, target):
    """Last value with date <= target."""
    pick = None
    for d, v in series:
        if d <= target:
            pick = (d, v)
        else:
            break
    return pick

def pct(new, old):
    if old in (None, 0):
        return None
    return round((new / old - 1.0) * 100.0, 1)

def compute(series, decimals):
    """Return dict: latest px, asof, w/m/ytd %, and recent daily series (~180d)."""
    if not series:
        return None
    last_d, last_v = series[-1]
    w = val_on_or_before(series, last_d - datetime.timedelta(days=7))
    m = val_on_or_before(series, last_d - datetime.timedelta(days=30))
    # YTD: vs last close of prior year (fallback first close this year)
    prior_year_end = datetime.date(last_d.year - 1, 12, 31)
    yb = val_on_or_before(series, prior_year_end)
    if yb is None:
        yb = next(((d, v) for d, v in series if d.year == last_d.year), None)
    cutoff = last_d - datetime.timedelta(days=185)
    recent = [[d.isoformat(), round(v, decimals)] for d, v in series if d >= cutoff]
    return {
        "px_val": round(last_v, decimals),
        "asof": last_d.isoformat(),
        "w": pct(last_v, w[1] if w else None),
        "m": pct(last_v, m[1] if m else None),
        "y": pct(last_v, yb[1] if yb else None),
        "series": recent,
    }

# instrument config: nmEn key -> (fetcher, url, mult, decimals, px_fmt, src)
LB_TO_T = 2204.62
CFG = [
    ("WTI Crude",  "fred",  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO",     1.0, 2, "~${v}/bbl",  "FRED DCOILWTICO (EIA WTI spot)",   "https://fred.stlouisfed.org/series/DCOILWTICO"),
    ("Brent Crude","fred",  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU",   1.0, 2, "~${v}/bbl",  "FRED DCOILBRENTEU (Brent spot)",   "https://fred.stlouisfed.org/series/DCOILBRENTEU"),
    ("US Nat Gas", "fred",  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DHHNGSP",        1.0, 2, "~${v}/MMBtu","FRED DHHNGSP (Henry Hub spot)",    "https://fred.stlouisfed.org/series/DHHNGSP"),
    ("Gold",       "fred",  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GOLDAMGBD228NLBM",1.0,2, "~${v}/oz",  "FRED LBMA Gold AM fix",            "https://fred.stlouisfed.org/series/GOLDAMGBD228NLBM"),
    ("Silver",     "stooq", "https://stooq.com/q/d/l/?s=xagusd&i=d",                             1.0, 2, "~${v}/oz",  "Stooq XAGUSD (silver spot)",       "https://stooq.com/q/d/l/?s=xagusd&i=d"),
    ("LME Copper", "stooq", "https://stooq.com/q/d/l/?s=hg.f&i=d",                          LB_TO_T, 0, "~${v}/t",   "Stooq COMEX HG x2204.62 (LME proxy)","https://stooq.com/q/d/l/?s=hg.f&i=d"),
]

def fmt_px(tpl, v, decimals):
    s = ("{:,.%df}" % decimals).format(v)
    return tpl.replace("{v}", s)

def build():
    instruments = {}
    errors = {}
    for nm, kind, url, mult, dec, tpl, srcn, srcu in CFG:
        try:
            text = http_get(url)
            series = parse_fred(text) if kind == "fred" else parse_stooq(text, mult)
            c = compute(series, dec)
            if not c:
                errors[nm] = "no data parsed"
                continue
            px = fmt_px(tpl, c["px_val"], dec)
            instruments[nm] = {
                "px": px, "pxEn": px, "asof_iso": c["asof"],
                "asof": "%d/%d" % (datetime.date.fromisoformat(c["asof"]).month, datetime.date.fromisoformat(c["asof"]).day),
                "w": c["w"], "m": c["m"], "y": c["y"], "yl": "YTD",
                "series": c["series"], "srcName": srcn, "srcUrl": srcu,
            }
            print("OK  %-12s %s  (1W %s / 1M %s / YTD %s)  pts=%d" % (nm, px, c["w"], c["m"], c["y"], len(c["series"])))
        except Exception as e:
            errors[nm] = str(e)
            print("ERR %-12s %s" % (nm, e), file=sys.stderr)
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "note": "Real daily closes. WTI/Brent/HenryHub/Gold via FRED; Silver/Copper via Stooq (Copper=COMEX x2204.62, LME proxy). TTF not included (no free daily feed).",
        "instruments": instruments,
    }
    if errors:
        out["errors"] = errors
    return out

# ---- self-test on embedded sample CSV (no network) ----
def selftest():
    fred_sample = "observation_date,DCOILWTICO\n2025-12-31,71.00\n2026-01-02,73.50\n2026-05-22,95.10\n2026-06-15,77.50\n2026-06-19,77.54\n2026-06-22,74.30\n"
    s = parse_fred(fred_sample); c = compute(s, 2)
    assert c["px_val"] == 74.30, c
    assert c["asof"] == "2026-06-22"
    assert c["y"] == pct(74.30, 71.00), c["y"]   # YTD vs prior-year-end 71.00
    assert len(c["series"]) >= 3
    stooq_sample = "Date,Open,High,Low,Close,Volume\n2026-06-19,6.20,6.35,6.18,6.30,1000\n2026-06-22,6.25,6.31,6.20,6.28,900\n"
    s2 = parse_stooq(stooq_sample, LB_TO_T); c2 = compute(s2, 0)
    assert round(c2["px_val"]) == round(6.28 * LB_TO_T), c2
    print("SELFTEST OK: FRED ytd=%s px=%s ; Stooq copper $/t=%s" % (c["y"], c["px_val"], c2["px_val"]))

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        data = build()
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print("wrote data.json with %d instruments" % len(data["instruments"]))

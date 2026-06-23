#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices -> data.json (runs on GitHub Actions, stdlib only).

Primary source: Twelve Data time_series (key from env TWELVEDATA_KEY; free tier).
Fallback for energy: FRED fredgraph.csv (authoritative, lags ~1wk).
The API key is read from the environment and NEVER printed or committed.
Per-instrument result + any errors are written into data.json and printed to the log.
TTF (Europe gas) has no reliable free daily feed -> left to the page's baked value.
"""
import os, io, csv, sys, json, time, datetime, urllib.request, urllib.parse

KEY = os.environ.get("TWELVEDATA_KEY", "").strip()
UA = {"User-Agent": "commodities-dashboard/1.0"}
LB_TO_T = 2204.62
TD_URL = "https://api.twelvedata.com/time_series"
_td_calls = 0

def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")

def td_series(symbol):
    """Twelve Data daily close series, ascending. Respects free-tier 8 req/min."""
    global _td_calls
    if not KEY:
        raise ValueError("no TWELVEDATA_KEY env")
    if _td_calls:           # pace calls: free tier = 8/min
        time.sleep(8)
    _td_calls += 1
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1day",
                                 "outputsize": 200, "apikey": KEY, "format": "JSON"})
    j = json.loads(http_get(TD_URL + "?" + qs))   # key is in URL only; never logged
    if isinstance(j, dict) and j.get("status") == "error":
        raise ValueError("TD %s -> %s" % (symbol, str(j.get("message", ""))[:110]))
    vals = j.get("values") if isinstance(j, dict) else None
    if not vals:
        raise ValueError("TD %s -> no values" % symbol)
    out = []
    for row in vals:
        try:
            out.append((datetime.date.fromisoformat(row["datetime"][:10]), float(row["close"])))
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    if not out:
        raise ValueError("TD %s -> empty parse" % symbol)
    return out

def fred_series(series_id):
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
        raise ValueError("FRED %s -> no data" % series_id)
    return out

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
    last_d, last_v = series[-1]
    w = val_on_or_before(series, last_d - datetime.timedelta(days=7))
    m = val_on_or_before(series, last_d - datetime.timedelta(days=30))
    yb = val_on_or_before(series, datetime.date(last_d.year - 1, 12, 31)) \
         or next(((d, v) for d, v in series if d.year == last_d.year), None)
    cutoff = last_d - datetime.timedelta(days=185)
    recent = [[d.isoformat(), round(v, decimals)] for d, v in series if d >= cutoff]
    return {"px_val": round(last_v, decimals), "asof": last_d.isoformat(),
            "w": pct(last_v, w[1] if w else None), "m": pct(last_v, m[1] if m else None),
            "y": pct(last_v, yb[1] if yb else None), "series": recent}

def scale_copper(series):
    """Twelve Data copper may be $/lb (~6) or $/tonne (~13000). Normalize to $/tonne."""
    if series and series[-1][1] < 100:
        return [(d, v * LB_TO_T) for d, v in series]
    return series

# nmEn -> (decimals, px_tpl, scaler, [TD symbol candidates], FRED fallback id or None)
CFG = [
    ("WTI Crude",  2, "~${v}/bbl",   None,         ["WTI/USD", "WTI"],            "DCOILWTICO"),
    ("Brent Crude",2, "~${v}/bbl",   None,         ["XBR/USD", "BRENT/USD", "UKOIL"], "DCOILBRENTEU"),
    ("US Nat Gas", 2, "~${v}/MMBtu", None,         ["NG/USD", "NG", "NATGAS/USD"], "DHHNGSP"),
    ("Gold",       0, "~${v}/oz",    None,         ["XAU/USD"],                   None),
    ("Silver",     2, "~${v}/oz",    None,         ["XAG/USD"],                   None),
    ("LME Copper", 0, "~${v}/t",     scale_copper, ["XCU/USD", "COPPER", "HG/USD"], None),
]

def fmt_px(tpl, v, decimals):
    return tpl.replace("{v}", ("{:,.%df}" % decimals).format(v))

def build():
    instruments, errors = {}, {}
    for nm, dec, tpl, scaler, td_syms, fred_id in CFG:
        series = None; src = None; tries = []
        for sym in td_syms:
            try:
                s = td_series(sym)
                if scaler:
                    s = scaler(s)
                series, src = s, ("TwelveData %s" % sym, "https://twelvedata.com/")
                break
            except Exception as e:
                tries.append(str(e))
        if series is None and fred_id:
            try:
                series = fred_series(fred_id)
                src = ("FRED %s (fallback, ~1wk lag)" % fred_id, "https://fred.stlouisfed.org/series/%s" % fred_id)
            except Exception as e:
                tries.append(str(e))
        if series is None:
            errors[nm] = " | ".join(tries) or "no source"
            print("ERR %-12s %s" % (nm, errors[nm]))
            continue
        c = compute(series, dec)
        d0 = datetime.date.fromisoformat(c["asof"])
        instruments[nm] = {"px": fmt_px(tpl, c["px_val"], dec), "pxEn": fmt_px(tpl, c["px_val"], dec),
                           "asof_iso": c["asof"], "asof": "%d/%d" % (d0.month, d0.day),
                           "w": c["w"], "m": c["m"], "y": c["y"], "yl": "YTD",
                           "series": c["series"], "srcName": src[0], "srcUrl": src[1]}
        print("OK  %-12s %-12s asof %-10s 1W %-6s 1M %-6s YTD %-6s pts %-4d via %s"
              % (nm, instruments[nm]["px"], c["asof"], c["w"], c["m"], c["y"], len(c["series"]), src[0]))
    out = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "Real daily closes via Twelve Data (FRED fallback for energy). Copper normalized to $/t. TTF not included (no free daily feed).",
           "key_present": bool(KEY), "instruments": instruments}
    if errors:
        out["errors"] = errors
    return out

if __name__ == "__main__":
    print("TWELVEDATA_KEY present:", bool(KEY))
    data = build()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("wrote data.json: %d ok, %d errors" % (len(data["instruments"]), len(data.get("errors", {}))))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices -> data.json (GitHub Actions, stdlib only).
Sources per instrument: Twelve Data time_series (env TWELVEDATA_KEY; explicit pair
symbols only) then FRED (energy fallback, ~1wk lag). A series is ACCEPTED only if
its latest value is inside a sane band -> a wrong symbol (e.g. a stock) can never
pollute the data. Key never printed/committed. Copper -> $/tonne. TTF kept baked.
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

def td_diagnostic_list():
    if not KEY:
        return
    try:
        j = json.loads(http_get("https://api.twelvedata.com/commodities?apikey=" + urllib.parse.quote(KEY)))
        data = j.get("data") if isinstance(j, dict) else None
        if data:
            syms = ["%s(%s)" % (d.get("symbol"), d.get("name", "")[:18]) for d in data]
            print("TD commodities available (%d): %s" % (len(syms), ", ".join(syms[:50])))
        else:
            print("TD commodities list -> %s" % str(j)[:160])
    except Exception as e:
        print("TD commodities list error: %s" % e)

def td_series(symbol):
    global _td_calls
    if not KEY:
        raise ValueError("no TWELVEDATA_KEY env")
    if _td_calls:
        time.sleep(8)
    _td_calls += 1
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1day",
                                 "outputsize": 200, "apikey": KEY, "format": "JSON"})
    j = json.loads(http_get(TD_URL + "?" + qs))
    if isinstance(j, dict) and j.get("status") == "error":
        raise ValueError("TD %s -> %s" % (symbol, str(j.get("message", ""))[:100]))
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
    if series and series[-1][1] < 100:
        return [(d, v * LB_TO_T) for d, v in series]
    return series

CFG = [
    ("WTI Crude",  2, "~${v}/bbl",   (20, 200),     None,         ["WTI/USD"],                  "DCOILWTICO"),
    ("Brent Crude",2, "~${v}/bbl",   (20, 200),     None,         ["XBR/USD", "BRENT/USD"],     "DCOILBRENTEU"),
    ("US Nat Gas", 2, "~${v}/MMBtu", (1, 20),       None,         ["NG/USD", "NATURALGAS/USD"], "DHHNGSP"),
    ("Gold",       0, "~${v}/oz",    (1000, 10000), None,         ["XAU/USD"],                  None),
    ("Silver",     2, "~${v}/oz",    (5, 300),      None,         ["XAG/USD"],                  None),
    ("LME Copper", 0, "~${v}/t",     (3000, 25000), scale_copper, ["XCU/USD", "COPPER/USD"],    None),
]

def fmt_px(tpl, v, decimals):
    return tpl.replace("{v}", ("{:,.%df}" % decimals).format(v))

def accept(nm, band, series):
    lo, hi = band
    return lo <= series[-1][1] <= hi

def build():
    td_diagnostic_list()
    instruments, errors = {}, {}
    for nm, dec, tpl, band, scaler, td_syms, fred_id in CFG:
        series = None; src = None; tries = []
        for sym in td_syms:
            try:
                s = td_series(sym)
                if scaler:
                    s = scaler(s)
                if not accept(nm, band, s):
                    tries.append("TD %s rejected last=%.4f out of band %s" % (sym, s[-1][1], band))
                    continue
                series, src = s, ("TwelveData %s" % sym, "https://twelvedata.com/"); break
            except Exception as e:
                tries.append(str(e))
        if series is None and fred_id:
            try:
                s = fred_series(fred_id)
                if accept(nm, band, s):
                    series, src = s, ("FRED %s (fallback, ~1wk lag)" % fred_id,
                                      "https://fred.stlouisfed.org/series/%s" % fred_id)
                else:
                    tries.append("FRED %s rejected last=%.4f" % (fred_id, s[-1][1]))
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
           "note": "Real daily closes; Twelve Data (metals) + FRED fallback (energy, ~1wk lag). Value-band guarded. Copper->$/t. TTF not included.",
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

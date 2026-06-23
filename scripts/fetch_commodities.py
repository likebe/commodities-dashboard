#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices -> data.json (GitHub Actions, stdlib only).

Per instrument:
  Energy (WTI/Brent/NatGas): API Ninjas historical (fresh futures) ->
      else FRED history + API Ninjas latest price grafted on the tip -> else FRED only.
  Metals (Gold/Silver/Copper): Twelve Data (XAU/XAG are free 'forex'; copper best-effort).
A series is ACCEPTED only if its latest value sits inside a sane band, so a wrong
symbol can never pollute the data. Keys come from env (TWELVEDATA_KEY, APININJAS_KEY)
and are never printed or committed. Copper -> $/tonne. TTF kept baked.
"""
import os, io, csv, sys, json, time, datetime, urllib.request, urllib.parse, urllib.error

TD_KEY = os.environ.get("TWELVEDATA_KEY", "").strip()
AN_KEY = os.environ.get("APININJAS_KEY", "").strip()
UA = {"User-Agent": "commodities-dashboard/1.0"}
LB_TO_T = 2204.62
TD_URL = "https://api.twelvedata.com/time_series"
AN_URL = "https://api.api-ninjas.com/v1/"
_td_calls = 0

def http_get(url, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")

# ---------------- Twelve Data (metals) ----------------
def td_series(symbol):
    global _td_calls
    if not TD_KEY:
        raise ValueError("no TWELVEDATA_KEY")
    if _td_calls:
        time.sleep(8)
    _td_calls += 1
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1day",
                                 "outputsize": 200, "apikey": TD_KEY, "format": "JSON"})
    j = json.loads(http_get(TD_URL + "?" + qs))
    if isinstance(j, dict) and j.get("status") == "error":
        raise ValueError("TD %s -> %s" % (symbol, str(j.get("message", ""))[:90]))
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
        raise ValueError("TD %s -> empty" % symbol)
    return out

# ---------------- API Ninjas (energy) ----------------
def an_hist(name):
    if not AN_KEY:
        raise ValueError("no APININJAS_KEY")
    raw = http_get(AN_URL + "commoditypricehistorical?name=%s&period=1d" % name, {"X-Api-Key": AN_KEY})
    j = json.loads(raw)
    items = j.get("data") if isinstance(j, dict) and "data" in j else (j if isinstance(j, list) else None)
    if not items:
        raise ValueError("AN hist %s -> %s" % (name, str(j)[:80]))
    out = []
    for it in items:
        try:
            t = it.get("time", it.get("timestamp"))
            c = it.get("close", it.get("price"))
            out.append((datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).date(), float(c)))
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    if not out:
        raise ValueError("AN hist %s -> empty parse" % name)
    return out

def an_latest(name):
    if not AN_KEY:
        raise ValueError("no APININJAS_KEY")
    j = json.loads(http_get(AN_URL + "commodityprice?name=%s" % name, {"X-Api-Key": AN_KEY}))
    p = float(j["price"])
    t = j.get("updated")
    d = datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).date() if t else datetime.date.today()
    return (d, p)

# ---------------- FRED (energy fallback) ----------------
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

# ---------------- metrics ----------------
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

def in_band(band, v):
    lo, hi = band
    return lo <= v <= hi

def graft_latest(series, name, band):
    """Replace/append the series tip with API Ninjas' fresh latest price."""
    d, p = an_latest(name)
    if not in_band(band, p):
        raise ValueError("AN latest %s=%.4f out of band" % (name, p))
    return [x for x in series if x[0] < d] + [(d, p)]

# nmEn -> (decimals, px_tpl, band, scaler, td_syms, fred_id, an_name)
CFG = [
    ("WTI Crude",  2, "~${v}/bbl",   (20, 200),     None,         [],                          "DCOILWTICO",   "crude_oil"),
    ("Brent Crude",2, "~${v}/bbl",   (20, 200),     None,         [],                          "DCOILBRENTEU", "brent_crude_oil"),
    ("US Nat Gas", 2, "~${v}/MMBtu", (1, 20),       None,         [],                          "DHHNGSP",      "natural_gas"),
    ("Gold",       0, "~${v}/oz",    (1000, 10000), None,         ["XAU/USD"],                 None,           None),
    ("Silver",     2, "~${v}/oz",    (5, 300),      None,         ["XAG/USD"],                 None,           None),
    ("LME Copper", 0, "~${v}/t",     (3000, 25000), scale_copper, ["XCU/USD", "COPPER/USD"],   None,           None),
]

def fmt_px(tpl, v, decimals):
    return tpl.replace("{v}", ("{:,.%df}" % decimals).format(v))

def build():
    instruments, errors = {}, {}
    for nm, dec, tpl, band, scaler, td_syms, fred_id, an_name in CFG:
        series = None; src = None; tries = []
        # A) API Ninjas historical (energy, fresh full series)
        if an_name and AN_KEY:
            try:
                s = an_hist(an_name)
                if scaler:
                    s = scaler(s)
                if in_band(band, s[-1][1]):
                    series, src = s, ("API Ninjas %s (hist futures)" % an_name, "https://api-ninjas.com/")
                else:
                    tries.append("AN hist %s rejected last=%.4f" % (an_name, s[-1][1]))
            except Exception as e:
                tries.append(str(e))
        # B) Twelve Data (metals)
        if series is None:
            for sym in td_syms:
                try:
                    s = td_series(sym)
                    if scaler:
                        s = scaler(s)
                    if in_band(band, s[-1][1]):
                        series, src = s, ("TwelveData %s" % sym, "https://twelvedata.com/"); break
                    tries.append("TD %s rejected last=%.4f" % (sym, s[-1][1]))
                except Exception as e:
                    tries.append(str(e))
        # C) FRED (energy fallback)
        if series is None and fred_id:
            try:
                s = fred_series(fred_id)
                if in_band(band, s[-1][1]):
                    series, src = s, ("FRED %s (~1wk lag)" % fred_id,
                                      "https://fred.stlouisfed.org/series/%s" % fred_id)
                else:
                    tries.append("FRED %s rejected last=%.4f" % (fred_id, s[-1][1]))
            except Exception as e:
                tries.append(str(e))
        # D) graft fresh API Ninjas latest onto a FRED tip (energy)
        if series is not None and an_name and AN_KEY and src and src[0].startswith("FRED"):
            try:
                series = graft_latest(series, an_name, band)
                src = ("FRED hist + API Ninjas %s latest" % an_name, "https://api-ninjas.com/")
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
           "note": "Real daily closes. Energy via API Ninjas futures (FRED fallback + fresh tip); metals via Twelve Data. Value-band guarded. Copper->$/t. TTF not included.",
           "keys": {"twelvedata": bool(TD_KEY), "apininjas": bool(AN_KEY)}, "instruments": instruments}
    if errors:
        out["errors"] = errors
    return out

if __name__ == "__main__":
    print("keys present: TwelveData=%s APININJAS=%s" % (bool(TD_KEY), bool(AN_KEY)))
    data = build()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("wrote data.json: %d ok, %d errors" % (len(data["instruments"]), len(data.get("errors", {}))))

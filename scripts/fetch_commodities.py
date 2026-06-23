#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch REAL daily commodity prices -> data.json (GitHub Actions, stdlib only).

Energy (WTI/Brent/NatGas): FRED daily history (retry on timeout) + API Ninjas
  'commodityprice' LATEST grafted on the tip -> headline price is current.
Metals (Gold/Silver/Copper): Twelve Data (XAU/XAG free 'forex'; copper best-effort).
A series is accepted only if its latest value sits inside a sane band (blocks wrong
symbols). Keys come from env (TWELVEDATA_KEY, APININJAS_KEY); never printed/committed.
HTTP error bodies are surfaced (key-free) for diagnosis. Copper -> $/t. TTF kept baked.
"""
import os, io, csv, sys, json, time, datetime, urllib.request, urllib.parse, urllib.error

TD_KEY = os.environ.get("TWELVEDATA_KEY", "").strip()
AN_KEY = os.environ.get("APININJAS_KEY", "").strip()
UA = {"User-Agent": "commodities-dashboard/1.0"}
LB_TO_T = 2204.62
_td_calls = 0

def http_get(url, headers=None, label=""):
    h = dict(UA)
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:120].replace("\n", " ")
        except Exception:
            pass
        raise ValueError("HTTP %s %s %s" % (e.code, label, body))

# -------- Twelve Data (metals) --------
def td_series(symbol):
    global _td_calls
    if not TD_KEY:
        raise ValueError("no TWELVEDATA_KEY")
    if _td_calls:
        time.sleep(8)
    _td_calls += 1
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1day",
                                 "outputsize": 200, "apikey": TD_KEY, "format": "JSON"})
    j = json.loads(http_get("https://api.twelvedata.com/time_series?" + qs, label="TD %s" % symbol))
    if isinstance(j, dict) and j.get("status") == "error":
        raise ValueError("TD %s -> %s" % (symbol, str(j.get("message", ""))[:80]))
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

# -------- API Ninjas latest (free) --------
def an_latest(name):
    if not AN_KEY:
        raise ValueError("no APININJAS_KEY")
    j = json.loads(http_get("https://api.api-ninjas.com/v1/commodityprice?name=%s" % name,
                            {"X-Api-Key": AN_KEY}, label="AN %s" % name))
    if "price" not in j:
        raise ValueError("AN %s -> %s" % (name, str(j)[:80]))
    p = float(j["price"])
    t = j.get("updated")
    d = datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).date() if t else datetime.date.today()
    return (d, p)

# -------- FRED (energy history, retry) --------
def fred_series(series_id):
    last = None
    for attempt in range(3):
        try:
            text = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series_id,
                            label="FRED %s" % series_id)
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
            if out:
                return out
            last = ValueError("FRED %s -> no rows" % series_id)
        except Exception as e:
            last = e
            time.sleep(3)
    raise last

# -------- metrics --------
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
    return [(d, v * LB_TO_T) for d, v in series] if series and series[-1][1] < 100 else series

def in_band(band, v):
    return band[0] <= v <= band[1]

# nmEn -> (dec, tpl, band, scaler, td_syms, fred_id, an_name)
CFG = [
    ("WTI Crude",  2, "~${v}/bbl",   (20, 200),     None,         [],                        "DCOILWTICO",   "crude_oil"),
    ("Brent Crude",2, "~${v}/bbl",   (20, 200),     None,         [],                        "DCOILBRENTEU", "brent_crude_oil"),
    ("US Nat Gas", 2, "~${v}/MMBtu", (1, 20),       None,         [],                        "DHHNGSP",      "natural_gas"),
    ("Gold",       0, "~${v}/oz",    (1000, 10000), None,         ["XAU/USD"],               None,           "gold"),
    ("Silver",     2, "~${v}/oz",    (5, 300),      None,         ["XAG/USD", "SILVER/USD"], None,           "silver"),
    ("LME Copper", 0, "~${v}/t",     (3000, 25000), scale_copper, ["XCU/USD", "COPPER/USD"], None,           None),
]

def fmt_px(tpl, v, decimals):
    return tpl.replace("{v}", ("{:,.%df}" % decimals).format(v))

def build():
    instruments, errors = {}, {}
    for nm, dec, tpl, band, scaler, td_syms, fred_id, an_name in CFG:
        series = None; src = None; tries = []
        # A) Twelve Data (metals)
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
        # B) FRED history (energy)
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
        # C) Graft API Ninjas LATEST onto the tip (fresh headline) for any instrument with an_name
        if series is not None and an_name and AN_KEY:
            try:
                d, p = an_latest(an_name)
                if in_band(band, p):
                    series = [x for x in series if x[0] < d] + [(d, p)]
                    if src and src[0].startswith("FRED"):
                        src = ("FRED hist + API Ninjas %s latest" % an_name, "https://api-ninjas.com/")
                    else:
                        src = (src[0] + " + AN %s tip" % an_name, src[1])
                else:
                    tries.append("AN latest %s out of band %.4f" % (an_name, p))
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
        if tries:
            print("    %-12s notes: %s" % (nm, " | ".join(tries)[:160]))
        print("OK  %-12s %-12s asof %-10s 1W %-6s 1M %-6s YTD %-6s pts %-4d via %s"
              % (nm, instruments[nm]["px"], c["asof"], c["w"], c["m"], c["y"], len(c["series"]), src[0]))
    out = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "Real daily closes. Energy: FRED history + API Ninjas fresh-latest tip. Metals: Twelve Data. Value-band guarded. Copper->$/t. TTF not included.",
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

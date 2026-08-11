#!/usr/bin/env python3
"""Sweep live fares for full-service carriers (KLM, Swiss, Lufthansa, BA, ...).

The companion to ryanair_sweep.py. That script owns the low-cost carriers, which
sell direct and appear in no neutral pipe. This one owns everything that files
fares to ATPCO and sells through NDC.

Four sources, used for four different jobs:

  travelpayouts   free, all carriers, cached up to 7 days.  Tells you WHO flies
                  a route and in what band.  Too sparse to fill a grid.
  scrappa         free, 500/month recurring, no card.  Google Flights data, all
                  carriers.  THE DEFAULT.  This is what prices the trip.
  afklm           free, 5,000/day, but AF/KL-marketed flights only.  Prices the
                  KLM option authoritatively; `calendar` mode sweeps a whole
                  date range in one call.
  duffel          $0.005/search.  Only when the output must be BOOKABLE.

Price every carrier or state no conclusion. A Ryanair-only pass on AMS->AGP
returned €182.89 door to door; the all-carrier pass returned €153.08, because
KLM flew the peak return for €93 against Ryanair's €154 on the same day. One
airline's API does not give you a cheap answer, it gives you a confident wrong
one.

Sweep each leg ONE-WAY first. That prices every mixed-airport pair at once
(any out pairs with any back), which a round-trip grid cannot express at all.
Use round-trip calls only to confirm the two or three finalists.

Auth, all optional depending on mode:
  export TRAVELPAYOUTS_TOKEN=...   # free, travelpayouts.com signup
  export SCRAPPA_API_KEY=...       # free, scrappa.co signup, no card
  export AFKLM_API_KEY=...         # free, developer.airfranceklm.com, manual approval
  export DUFFEL_TOKEN=...          # duffel.com dashboard

Modes:
  grid      cheapest fare per date pair, travelpayouts cache      (free)
  oneway    cheapest live fare per date, one direction            (free on scrappa)
  flex      live date grid, out x back                            (free on scrappa)
  verify    live price for specific date pairs                    (free on scrappa)
  calendar  whole date range in ONE call, AF-KLM only             (free)

Examples:
  ./fullservice_sweep.py oneway   --from AMS,EIN,BRU --to AGP --out 2026-08-24 --span 0
  ./fullservice_sweep.py oneway   --from AGP --to AMS,EIN --out 2026-08-29 --span 2
  ./fullservice_sweep.py verify   --from AMS --to AGP --pairs 2026-08-24:2026-08-29
  ./fullservice_sweep.py flex     --from AMS --to ZRH --out 2026-09-12 --back 2026-09-15 --span 3
  ./fullservice_sweep.py calendar --from AMS --to ZRH --out 2026-09-12 --span 3 --provider afklm
  ./fullservice_sweep.py grid     --from AMS --to ZRH --out 2026-09-12 --back 2026-09-15
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

TP_HOST = "https://api.travelpayouts.com"
DUFFEL_HOST = "https://api.duffel.com"
DUFFEL_VERSION = "v2"
IGNAV_HOST = "https://ignav.com"
SCRAPPA_HOST = "https://scrappa.co"
AFKLM_HOST = "https://api.airfranceklm.com"
THREADS = 8

# Scrappa sits behind Cloudflare, which rejects urllib's default User-Agent with
# a 403 "error code: 1010" (banned browser signature). Any real UA string passes.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) price-trip/1.0"

# Live-fare providers, in the order you should reach for them.
PROVIDERS = {
    "scrappa": {"env": "SCRAPPA_API_KEY", "cost": 0.0,  "note": "500 credits/month, recurring, no card"},
    "afklm":   {"env": "AFKLM_API_KEY",   "cost": 0.0,  "note": "5,000 calls/day, but AF/KL flights only"},
    "ignav":   {"env": "IGNAV_API_KEY",   "cost": 0.0,  "note": "1,000 requests one-time, then $2/1000"},
    "duffel":  {"env": "DUFFEL_TOKEN",    "cost": 0.005, "note": "$0.005/search, bookable offers"},
}


# ---------------------------------------------------------------- http


class FetchFailed(RuntimeError):
    """The provider did not answer. Never an empty result.

    A 503 and a day with no flights leave the wire as different bytes, and they
    have to stay different objects. The moment a failed fetch is allowed to
    answer [], the caller cannot tell "nothing flies that day" from "we could not
    look", and every guard downstream reads a confident zero. That is the exact
    failure this tool was written to stop, so it is stopped here, at the
    boundary, and not one layer later.
    """

    def __init__(self, url, detail):
        self.host = urllib.parse.urlsplit(url).netloc
        self.detail = detail
        super().__init__(f"{self.host} did not answer: {detail}")


def _get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise SystemExit(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}: {body}")
    except Exception as e:
        return {"__error__": str(e)}


def _post_json(url, payload, headers=None, timeout=45):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": UA}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        if e.code in (401, 403):
            host = urllib.parse.urlsplit(url).netloc
            raise SystemExit(f"HTTP {e.code} from {host} — the API key was rejected. {body}")
        return {"__error__": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"__error__": str(e)}


# ---------------------------------------------------------------- providers


def tp_prices(origin, dest, out_month, back_month, currency, token, direct_only):
    """Travelpayouts v3. Cached, all carriers, free. Returns list of dicts."""
    q = {
        "origin": origin,
        "destination": dest,
        "departure_at": out_month,
        "currency": currency,
        "sorting": "price",
        "limit": 1000,
        "one_way": "false" if back_month else "true",
        "token": token,
    }
    if back_month:
        q["return_at"] = back_month
    if direct_only:
        q["direct"] = "true"
    url = f"{TP_HOST}/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(q)
    body = _get_json(url)
    if "__error__" in body:
        return []
    return body.get("data") or []


def duffel_search(origin, dest, out_date, back_date, cabin, adults, token,
                  max_connections=None, timeout_ms=20000):
    """One Duffel offer request. Live, bookable. Returns the cheapest offer or None."""
    slices = [{"origin": origin, "destination": dest, "departure_date": out_date}]
    if back_date:
        slices.append({"origin": dest, "destination": origin, "departure_date": back_date})
    payload = {"data": {
        "slices": slices,
        "passengers": [{"type": "adult"} for _ in range(adults)],
        "cabin_class": cabin,
    }}
    if max_connections is not None:
        payload["data"]["max_connections"] = max_connections
    q = urllib.parse.urlencode({"return_offers": "true", "supplier_timeout": timeout_ms})
    url = f"{DUFFEL_HOST}/air/offer_requests?{q}"
    body = _post_json(url, payload, {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": DUFFEL_VERSION,
    })
    if "__error__" in body:
        return {"error": body["__error__"]}
    offers = (body.get("data") or {}).get("offers") or []
    if not offers:
        return None
    best = min(offers, key=lambda o: float(o.get("total_amount") or 1e12))
    return _offer_summary(best, out_date, back_date)


def ignav_search(origin, dest, out_date, back_date, cabin, adults, key,
                 direct_only=False, raw=False):
    """One Ignav fare search. Live prices + booking links, 1,000 free requests."""
    path = "/api/fares/round-trip" if back_date else "/api/fares/one-way"
    payload = {"origin": origin, "destination": dest, "departure_date": out_date,
               "adults": adults, "cabin_class": cabin}
    if back_date:
        payload["return_date"] = back_date
    if direct_only:
        payload["max_stops"] = 0
    body = _post_json(f"{IGNAV_HOST}{path}", payload, {"X-Api-Key": key})
    if "__error__" in body:
        return {"error": body["__error__"]}
    if raw:
        return {"raw": body}
    fares = _first_list(body)
    if not fares:
        return None
    best = min(fares, key=lambda f: _num(_dig(f, "price", "total_price", "total",
                                              "amount", "fare", default=1e12)))
    return _ignav_summary(best, out_date, back_date)


def _scrappa_url(origin, dest, out_date, back_date, cabin, adults,
                 direct_only=False, currency="EUR", market="nl"):
    path = "/api/flights/round-trip" if back_date else "/api/flights/one-way"
    q = {"origin": origin, "destination": dest, "departure_date": out_date,
         "currency": currency.upper(), "adults": adults,
         "cabin_class": cabin, "gl": market, "hl": "en"}
    if back_date:
        q["return_date"] = back_date
    if direct_only:
        # The API validates this: any | nonstop | one_or_fewer | two_or_fewer.
        # Passing a number is accepted and then silently ignored.
        q["max_stops"] = "nonstop"
    return f"{SCRAPPA_HOST}{path}?" + urllib.parse.urlencode(q)


def scrappa_search(origin, dest, out_date, back_date, cabin, adults, key,
                   direct_only=False, raw=False, currency="EUR", market="nl"):
    """One Scrappa fare search. Google Flights data, 500 credits/month, no card.

    On the round-trip endpoint the returned price is the FULL round-trip total for
    that outbound option, and return_legs is empty — Google prices the trip at
    outbound selection, before you have picked a return. So one call per date pair
    prices the whole return, which is why grids run on round-trip, not two one-ways.
    """
    url = _scrappa_url(origin, dest, out_date, back_date, cabin, adults,
                       direct_only, currency, market)
    body = _get_json_or_fail(url, {"X-API-KEY": key})
    if raw:
        return {"raw": body}
    fares = body.get("flights") or []
    if not fares:
        return None
    best = min(fares, key=lambda f: _num(f.get("price")))
    return _scrappa_summary(best, out_date, back_date)


def scrappa_options(origin, dest, out_date, cabin, adults, key,
                    currency="EUR", market="nl"):
    """Every offer on one date, not just the winner. Same credit, more evidence.

    The cheapest fare cannot say why it is the cheapest fare. Fifteen offers and
    none of them nonstop is a closed day; one offer at half the price is an open
    one. `schedule_guard.py` reads that difference, so keep the whole list.

    Raises FetchFailed when the provider does not answer. An empty list here
    means the provider answered and had nothing, which is a fact about flights.
    """
    url = _scrappa_url(origin, dest, out_date, None, cabin, adults,
                       False, currency, market)
    body = _get_json_or_fail(url, {"X-API-KEY": key})
    out = []
    for f in body.get("flights") or []:
        s = _scrappa_summary(f, out_date, None)
        out.append({"price": s["price"], "stops": s["out"].get("stops", 0),
                    "name": s["owner_name"] or s["owner"], "carrier": s["owner"],
                    "flight": s["out"].get("flight", ""),
                    "dep": s["out"].get("dep", ""), "arr": s["out"].get("arr", ""),
                    "minutes": s["minutes"]})
    return out


def _scrappa_leg(legs):
    if not legs:
        return {}
    first, last = legs[0], legs[-1]
    return {"dep": str(first.get("departure_time") or "")[11:16],
            "arr": str(last.get("arrival_time") or "")[11:16],
            "carrier": first.get("airline") or "",
            "flight": f"{first.get('airline','')}{first.get('flight_number','')}",
            "stops": max(len(legs) - 1, 0)}


def _scrappa_summary(f, out_date, back_date):
    out_legs = f.get("outbound_legs") or f.get("legs") or []
    return {
        "price": _num(f.get("price")),
        "currency": f.get("currency") or "EUR",
        "owner": _scrappa_leg(out_legs).get("carrier", ""),
        "owner_name": f.get("airline_name") or "",
        "out": _scrappa_leg(out_legs),
        "back": _scrappa_leg(f.get("return_legs") or []),
        "out_date": out_date,
        "back_date": back_date,
        "stops": f.get("stops", 0),
        "minutes": f.get("total_duration_minutes"),
        "co2": (f.get("emissions") or {}).get("co2_kilograms"),
        "link": f.get("booking_token", ""),
    }


def _afklm_headers(key):
    return {"API-Key": key,
            "Accept": "application/hal+json;charset=utf8",
            "Accept-Language": "en-NL",
            "AFKL-TRAVEL-Host": "KL",
            "AFKL-TRAVEL-Country": "NL"}


def _afklm_conn(origin, dest, dep, interval=None):
    c = {"departureDate": dep,
         "origin": {"airport": {"code": origin}},
         "destination": {"airport": {"code": dest}}}
    if interval:
        c["dateInterval"] = interval
    return c


def afklm_search(origin, dest, out_date, back_date, cabin, adults, key,
                 direct_only=False, raw=False):
    """One AF-KLM Open Data priced-offer search. AF/KL-marketed flights only."""
    conns = [_afklm_conn(origin, dest, out_date)]
    if back_date:
        conns.append(_afklm_conn(dest, origin, back_date))
    payload = {"commercialCabins": [cabin.upper().replace("_", " ") if cabin != "economy" else "ECONOMY"],
               "passengerCount": {"ADT": adults},
               "bookingFlow": "LEISURE",
               "displayPriceContent": "ALL_PAX_ROUNDED",
               "currency": "EUR",
               "requestedConnections": conns}
    body = _post_json(f"{AFKLM_HOST}/opendata/offers/v3/available-offers",
                      payload, _afklm_headers(key))
    if "__error__" in body:
        return {"error": body["__error__"]}
    if raw:
        return {"raw": body}
    fares = _first_list(body)
    if not fares:
        return None
    best = min(fares, key=lambda f: _num(_dig(f, "displayPrice", "totalPrice",
                                              "price", "fare", default=1e12)))
    r = _ignav_summary(best, out_date, back_date)
    if r["price"] >= 1e12:
        r["price"] = _num(_dig(best, "displayPrice", "totalPrice", default=1e12))
    r["owner"] = r["owner"] or "KL/AF"
    return r


def afklm_calendar(origin, dest, first, last, cabin, adults, key, raw=False):
    """AF-KLM lowest-fare-offers: a whole date range in ONE call, not one per date."""
    payload = {"type": "DAY",
               "commercialCabins": ["ECONOMY" if cabin == "economy" else cabin.upper()],
               "passengerCount": {"ADT": adults},
               "bookingFlow": "LEISURE",
               "displayPriceContent": "ALL_PAX_ROUNDED",
               "customer": {},
               "requestedConnections": [
                   _afklm_conn(origin, dest, first, f"{first}/{last}")]}
    body = _post_json(f"{AFKLM_HOST}/opendata/offers/v3/lowest-fare-offers",
                      payload, _afklm_headers(key))
    if "__error__" in body:
        return {"error": body["__error__"]}
    if raw:
        return {"raw": body}
    out = {}
    for f in _first_list(body):
        d = str(_dig(f, "departureDate", "date", "travelDate", default=""))[:10]
        p = _num(_dig(f, "displayPrice", "totalPrice", "price", "fare", default=1e12))
        if d and p < 1e12 and (d not in out or p < out[d]):
            out[d] = p
    return out


def mode_calendar(a):
    """One call per route for the whole window. AF-KLM only."""
    if a.provider != "afklm":
        raise SystemExit("calendar mode is AF-KLM only — add --provider afklm.")
    cred, spec = _live_auth(a)
    origins = [o.strip().upper() for o in a.origins.split(",") if o.strip()]
    dests = [d.strip().upper() for d in a.to.split(",") if d.strip()]
    dates = _dates_around(a.out, a.span)
    first, last = dates[0], dates[-1]
    print(f"AF-KLM fare calendar {first} to {last}: "
          f"{len(origins)*len(dests)} call(s), free (5,000/day)...")
    for o in origins:
        for d in dests:
            res = afklm_calendar(o, d, first, last, a.cabin, a.adults, cred, a.raw)
            if a.raw:
                print(json.dumps(res, indent=2)[:4000]); return
            if isinstance(res, dict) and "error" in res:
                print(f"  {o}->{d}: {res['error'][:120]}"); continue
            print(f"\n=== {o} -> {d}, cheapest AF/KL fare per day")
            for dt in dates:
                v = res.get(dt)
                print(f"  {dt}  {_fmt(v) if v else '-':>11}")


def _get_json_or_fail(url, headers=None, timeout=45):
    """GET that raises FetchFailed. A search that did not happen is not a result.

    A rejected key still exits the process. That is not a transient failure, and
    every remaining call in the sweep would fail the same way.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        if e.code in (401, 403):
            host = urllib.parse.urlsplit(url).netloc
            raise SystemExit(f"HTTP {e.code} from {host} — the API key was rejected. {body}")
        raise FetchFailed(url, f"HTTP {e.code}: {body}")
    except Exception as e:
        raise FetchFailed(url, str(e))


def _first_list(body):
    """Ignav's envelope is undocumented. Find the first list of fare-shaped dicts."""
    if isinstance(body, list):
        return body
    for k in ("fares", "data", "results", "offers", "itineraries", "flights"):
        v = body.get(k) if isinstance(body, dict) else None
        if isinstance(v, list) and v:
            return v
        if isinstance(v, dict):
            inner = _first_list(v)
            if inner:
                return inner
    return []


def _dig(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _num(v):
    try:
        if isinstance(v, dict):
            v = _dig(v, "amount", "value", "total", default=1e12)
        return float(str(v).replace(",", "").replace("€", "").strip())
    except Exception:
        return 1e12


def _ignav_summary(f, out_date, back_date):
    price = _num(_dig(f, "price", "total_price", "total", "amount", "fare"))
    cur = _dig(f, "currency", "currency_code", default="EUR")
    if isinstance(_dig(f, "price"), dict):
        cur = _dig(f["price"], "currency", "currency_code", default=cur)
    carrier = _dig(f, "carrier", "airline", "marketing_carrier", "validating_carrier",
                   default="")
    if isinstance(carrier, dict):
        carrier = _dig(carrier, "iata_code", "code", "name", default="")
    stops = _dig(f, "stops", "transfers", "number_of_changes", "max_stops", default=0)
    return {
        "price": price, "currency": str(cur), "owner": str(carrier),
        "owner_name": str(_dig(f, "carrier_name", "airline_name", default=carrier)),
        "out": {"dep": str(_dig(f, "departure_time", "departs_at", default=""))[11:16],
                "arr": str(_dig(f, "arrival_time", "arrives_at", default=""))[11:16],
                "stops": stops if isinstance(stops, int) else 0},
        "back": {}, "out_date": out_date, "back_date": back_date,
        "link": _dig(f, "booking_link", "link", "deeplink", "ignav_id", default=""),
    }


def _offer_summary(o, out_date, back_date):
    def leg(sl):
        segs = sl.get("segments") or []
        if not segs:
            return {}
        first, last = segs[0], segs[-1]
        return {
            "dep": (first.get("departing_at") or "")[11:16],
            "arr": (last.get("arriving_at") or "")[11:16],
            "carrier": ((first.get("marketing_carrier") or {}).get("iata_code")) or "",
            "stops": len(segs) - 1,
        }
    slices = o.get("slices") or []
    return {
        "price": float(o.get("total_amount") or 0),
        "currency": o.get("total_currency") or "",
        "owner": ((o.get("owner") or {}).get("iata_code")) or "",
        "owner_name": ((o.get("owner") or {}).get("name")) or "",
        "out": leg(slices[0]) if slices else {},
        "back": leg(slices[1]) if len(slices) > 1 else {},
        "out_date": out_date,
        "back_date": back_date,
    }


# ---------------------------------------------------------------- helpers


def _dates_around(anchor, span):
    d = datetime.strptime(anchor, "%Y-%m-%d").date()
    return [(d + timedelta(days=i)).isoformat() for i in range(-span, span + 1)]


def _months(dates):
    return sorted({d[:7] for d in dates})


def _fmt(v, cur="EUR"):
    sym = {"EUR": "€", "GBP": "£", "USD": "$"}.get(cur.upper(), cur + " ")
    return f"{sym}{v:,.2f}"


def _need(token, name, flag):
    if not token:
        raise SystemExit(f"{name} not set. export {flag}=... first. See --help.")


def _grid_print(out_dates, back_dates, cell, cur, min_nights, max_nights):
    head = "         " + "".join(f"{d[5:]:>9}" for d in back_dates)
    print("\n=== cheapest fare, out (rows) x back (columns)")
    print(head)
    best = None
    for od in out_dates:
        row = f"  {od[5:]:<7}"
        for bd in back_dates:
            n = (date.fromisoformat(bd) - date.fromisoformat(od)).days
            v = cell.get((od, bd)) if min_nights <= n <= max_nights else None
            row += f"{'-' if v is None else format(v, '.2f'):>9}"
            if v is not None and (best is None or v < best[0]):
                best = (v, od, bd, n)
        print(row)
    return best


# ---------------------------------------------------------------- modes


def mode_grid(a):
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    _need(token, "TRAVELPAYOUTS_TOKEN", "TRAVELPAYOUTS_TOKEN")
    out_dates = _dates_around(a.out, a.span)
    back_dates = _dates_around(a.back, a.span) if a.back else [None]
    dests = [d.strip().upper() for d in a.to.split(",") if d.strip()]
    origins = [o.strip().upper() for o in getattr(a, "origins").split(",") if o.strip()]

    rows = []
    for o in origins:
        for d in dests:
            for om in _months(out_dates):
                bm = _months([b for b in back_dates if b])[0] if a.back else None
                rows.extend(
                    {**r, "_o": o, "_d": d}
                    for r in tp_prices(o, d, om, bm, a.currency, token, a.direct)
                )

    if not rows:
        print("No cached prices returned. Travelpayouts only holds routes real users "
              "have searched in the last 7 days — try a busier route or a wider window.")
        return

    cell, meta = {}, {}
    ok_out, ok_back = set(out_dates), set(b for b in back_dates if b)
    for r in rows:
        od = (r.get("departure_at") or "")[:10]
        bd = (r.get("return_at") or "")[:10]
        if od not in ok_out:
            continue
        if a.back and bd not in ok_back:
            continue
        key = (od, bd or "")
        p = float(r.get("price") or 0)
        if p and (key not in cell or p < cell[key]):
            cell[key] = p
            meta[key] = r

    if not a.back:
        print(f"\n=== cheapest cached one-way fare per date  ({a.currency.upper()})")
        for od in out_dates:
            r = meta.get((od, ""))
            if r:
                print(f"  {od}  {_fmt(cell[(od,'')], a.currency):>10}  "
                      f"{r.get('airline','')}{r.get('flight_number','')}  "
                      f"{r['_o']}->{r['_d']}  {r.get('transfers',0)} stop(s)")
            else:
                print(f"  {od}  {'-':>10}")
        return

    best = _grid_print(out_dates, [b for b in back_dates if b],
                       cell, a.currency, a.min_nights, a.max_nights)
    print("\n=== cheapest cached pairs")
    ranked = sorted(
        ((v, k) for k, v in cell.items()
         if k[1] and a.min_nights <= (date.fromisoformat(k[1]) - date.fromisoformat(k[0])).days <= a.max_nights),
        key=lambda x: x[0])[:a.top]
    for v, (od, bd) in ranked:
        r = meta[(od, bd)]
        n = (date.fromisoformat(bd) - date.fromisoformat(od)).days
        print(f"  {_fmt(v, a.currency):>11}  {od} -> {bd}  {n}n  "
              f"{r.get('airline','')}  {r['_o']}->{r['_d']}  "
              f"{r.get('transfers',0)} stop(s)")
    if best:
        print(f"\nCheapest cell {_fmt(best[0], a.currency)} on {best[1]} -> {best[2]} ({best[3]}n).")
    print("\nCACHED, up to 7 days old, and indicative only. Re-price the top few with:")
    print(f"  {sys.argv[0]} verify --from {a.origins} --to {a.to} --pairs "
          + ",".join(f"{od}:{bd}" for _, (od, bd) in ranked[:3]))


def _live_auth(a):
    """Resolve the live-fare provider and its credential, or explain what is missing."""
    spec = PROVIDERS[a.provider]
    cred = os.environ.get(spec["env"])
    if not cred:
        raise SystemExit(
            f"{spec['env']} not set — needed for provider '{a.provider}' ({spec['note']}).\n"
            f"Either export {spec['env']}=..., or pick another provider with "
            f"--provider {'/'.join(k for k in PROVIDERS if k != a.provider)}.")
    return cred, spec


def _quote(n, spec):
    total = n * spec["cost"]
    return (f"{n} live searches, free ({spec['note']})" if total == 0
            else f"{n} live searches, about ${total:.2f}")


def _search_many(jobs, a, cred):
    def run(j):
        o, d, od, bd = j
        if a.provider == "scrappa":
            r = scrappa_search(o, d, od, bd, a.cabin, a.adults, cred, a.direct, a.raw)
        elif a.provider == "afklm":
            r = afklm_search(o, d, od, bd, a.cabin, a.adults, cred, a.direct, a.raw)
        elif a.provider == "ignav":
            r = ignav_search(o, d, od, bd, a.cabin, a.adults, cred, a.direct, a.raw)
        else:
            r = duffel_search(o, d, od, bd, a.cabin, a.adults, cred,
                              0 if a.direct else None)
        return j, r
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        return list(ex.map(run, jobs))


def mode_verify(a):
    cred, spec = _live_auth(a)
    origins = [o.strip().upper() for o in a.origins.split(",") if o.strip()]
    dests = [d.strip().upper() for d in a.to.split(",") if d.strip()]
    pairs = []
    for p in a.pairs.split(","):
        p = p.strip()
        if not p:
            continue
        od, _, bd = p.partition(":")
        pairs.append((od, bd or None))
    jobs = [(o, d, od, bd) for o in origins for d in dests for od, bd in pairs]
    print(f"Pricing {_quote(len(jobs), spec)} on {a.provider}...")
    results = _search_many(jobs, a, cred)
    if a.raw:
        for j, r in results:
            print(json.dumps(r, indent=2)[:4000]); break
        return
    good = [(j, r) for j, r in results if r and "error" not in r]
    good.sort(key=lambda x: x[1]["price"])
    print(f"\n=== live bookable offers, cheapest first")
    for (o, d, od, bd), r in good[:a.top]:
        cur = r["currency"]
        line = (f"  {_fmt(r['price'], cur):>11}  {od}" + (f" -> {bd}" if bd else "")
                + f"  {o}->{d}  {r['owner_name']} ({r['owner']})")
        if r["out"]:
            line += f"  OUT {r['out']['dep']}-{r['out']['arr']} {r['out']['stops']}st"
        if r["back"]:
            line += f" | BACK {r['back']['dep']}-{r['back']['arr']} {r['back']['stops']}st"
        print(line)
    bad = [(j, r) for j, r in results if not r or "error" in r]
    for j, r in bad:
        print(f"  {'no offers':>11}  {j[2]}{' -> ' + j[3] if j[3] else ''}  {j[0]}->{j[1]}"
              + (f"  [{r['error'][:60]}]" if r and "error" in r else ""))


def mode_oneway(a):
    cred, spec = _live_auth(a)
    origins = [o.strip().upper() for o in a.origins.split(",") if o.strip()]
    dests = [d.strip().upper() for d in a.to.split(",") if d.strip()]
    dates = _dates_around(a.out, a.span)
    jobs = [(o, d, od, None) for o in origins for d in dests for od in dates]
    print(f"Pricing {_quote(len(jobs), spec)} on {a.provider}...")
    if a.raw:
        for j, r in _search_many(jobs, a, cred):
            print(json.dumps(r, indent=2)[:4000]); break
        return
    if a.provider != "scrappa":
        raise SystemExit(
            f"oneway keeps every offer, and only scrappa returns them. "
            f"Run --provider scrappa (free), or use `verify` on {a.provider}.")

    # Keep every offer, not the winner. Same credit spend, and it is the only way
    # to see that a date has fifteen offers and no nonstop. Cheapest-only output
    # once sent two travellers to another country's airport for no reason.
    def run(j):
        o, d, od, _ = j
        return j, scrappa_options(o, d, od, a.cabin, a.adults, cred, a.currency)

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        results = list(ex.map(run, jobs))
    rows = [{"origin": o, "dest": d, "date": od, "opts": opts}
            for (o, d, od, _), opts in results]

    by_date = {}
    for r in rows:
        for opt in r["opts"]:
            k = r["date"]
            if k not in by_date or opt["price"] < by_date[k][1]["price"]:
                by_date[k] = ((r["origin"], r["dest"]), opt)
    print("\n=== cheapest live one-way per date")
    for od in dates:
        hit = by_date.get(od)
        if not hit:
            print(f"  {od}  {'-':>11}")
            continue
        (o, d), r = hit
        print(f"  {od}  {_fmt(r['price'], a.currency):>11}  {o}->{d}  "
              f"{r['name']}  {r.get('dep','')}-{r.get('arr','')}  "
              f"{r.get('stops', 0)} stop(s)")

    if a.save:
        with open(a.save, "w") as fh:
            json.dump({"rows": rows}, fh, indent=1)
        print(f"\nSaved {len(rows)} route-days to {a.save}")
    _audit(rows)


def _audit(rows):
    """Run the schedule guard on this sweep, now, before anyone reads the prices.

    Not a separate step the caller may forget. The Glasgow miss (Nov 2026) was
    provable from the sweep's own output at zero extra cost; what was missing was
    the question, so the sweep asks it itself.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from schedule_guard import audit_route, routes, widen

    findings = 0
    for (o, d), days in sorted(routes(rows).items()):
        block, _ = audit_route(o, d, days, 1.8)
        for kind, msg in block:
            findings += 1
            print(f"\nBLOCK  {o}->{d}  {kind}\n       {msg}")
            print(f"       fix: ./schedule_guard.py calendar --from {o} --to {d} {widen(days)}")
    if findings:
        print(f"\n{findings} blocking finding(s). Do not price a plan on these routes "
              f"until the calendar sweep says which days they fly.")
        raise SystemExit(1)
    print("\nSchedule guard: clean. No route loses its nonstop inside this window.")


def mode_flex(a):
    cred, spec = _live_auth(a)
    if not a.back:
        raise SystemExit("flex needs --back")
    origins = [o.strip().upper() for o in a.origins.split(",") if o.strip()]
    dests = [d.strip().upper() for d in a.to.split(",") if d.strip()]
    out_dates, back_dates = _dates_around(a.out, a.span), _dates_around(a.back, a.span)
    jobs = [(o, d, od, bd) for o in origins for d in dests
            for od in out_dates for bd in back_dates
            if a.min_nights <= (date.fromisoformat(bd) - date.fromisoformat(od)).days <= a.max_nights]
    cost = len(jobs) * spec["cost"]
    print(f"Pricing {_quote(len(jobs), spec)} on {a.provider}...")
    if cost > a.max_spend:
        raise SystemExit(f"Refusing: ${cost:.2f} exceeds --max-spend ${a.max_spend:.2f}. "
                         f"Narrow --span, or switch to --provider scrappa (free).")
    results = _search_many(jobs, a, cred)
    cell, meta, cur = {}, {}, a.currency.upper()
    for (o, d, od, bd), r in results:
        if r and "error" not in r:
            k = (od, bd)
            if k not in cell or r["price"] < cell[k]:
                cell[k], meta[k], cur = r["price"], ((o, d), r), r["currency"]
    best = _grid_print(out_dates, back_dates, cell, cur, a.min_nights, a.max_nights)
    print("\n=== cheapest live pairs")
    for v, k in sorted(((v, k) for k, v in cell.items()), key=lambda x: x[0])[:a.top]:
        (o, d), r = meta[k]
        n = (date.fromisoformat(k[1]) - date.fromisoformat(k[0])).days
        print(f"  {_fmt(v, cur):>11}  {k[0]} -> {k[1]}  {n}n  {o}->{d}  "
              f"{r['owner_name']} ({r['owner']})")
    if best:
        asked = cell.get((a.out, a.back))
        if asked and best[0] < asked:
            pct = (asked - best[0]) / asked * 100
            print(f"\nMoving {a.out} -> {a.back} to {best[1]} -> {best[2]} saves "
                  f"{_fmt(asked - best[0], cur)} ({pct:.0f}%).")
        elif asked:
            print(f"\nThe dates you asked for are the cheapest cell in the window.")


# ---------------------------------------------------------------- cli


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["grid", "oneway", "flex", "verify", "calendar"])
    p.add_argument("--from", dest="origins", default="AMS",
                   help="comma-separated origin IATA codes (default AMS)")
    p.add_argument("--to", default="ZRH",
                   help="comma-separated destination IATA codes (default ZRH)")
    p.add_argument("--out", default=(date.today() + timedelta(days=30)).isoformat(),
                   help="outbound date YYYY-MM-DD (default: 30 days out)")
    p.add_argument("--back", default=None, help="return date YYYY-MM-DD; omit for one-way")
    p.add_argument("--span", type=int, default=3, help="days either side of each leg (default 3)")
    p.add_argument("--pairs", default="", help="verify mode: OUT:BACK,OUT:BACK,...")
    p.add_argument("--min-nights", type=int, default=1)
    p.add_argument("--max-nights", type=int, default=30)
    p.add_argument("--cabin", default="economy",
                   choices=["economy", "premium_economy", "business", "first"])
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="EUR")
    p.add_argument("--direct", action="store_true", help="non-stop only")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--provider", default="scrappa", choices=sorted(PROVIDERS),
                   help="live-fare provider (default scrappa — free every month)")
    p.add_argument("--raw", action="store_true",
                   help="dump the first raw response and stop; use to map a new schema")
    p.add_argument("--save", default=None,
                   help="oneway mode: write every offer to this JSON, for schedule_guard.py")
    p.add_argument("--max-spend", type=float, default=2.00,
                   help="flex mode: refuse to run above this many dollars (default 2.00)")
    a = p.parse_args()

    if a.mode == "grid":
        mode_grid(a)
    elif a.mode == "oneway":
        mode_oneway(a)
    elif a.mode == "flex":
        mode_flex(a)
    elif a.mode == "calendar":
        mode_calendar(a)
    elif a.mode == "verify":
        if not a.pairs:
            raise SystemExit("verify needs --pairs 2026-09-12:2026-09-15,...")
        mode_verify(a)


if __name__ == "__main__":
    main()

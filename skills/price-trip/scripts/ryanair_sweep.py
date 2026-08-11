#!/usr/bin/env python3
"""Sweep live Ryanair fares. No API key needed.

Four modes:

  oneway   cheapest fare to every destination, from each origin, on one date
  roundtrip cheapest out-and-back pair per destination
  combo    cheapest mixed-airport pair: out of airport A, back into airport B
  flex     the same pair, priced across every date within +/- N days of both legs

Examples:

  ./ryanair_sweep.py oneway   --from EIN,CRL --out 2026-08-14
  ./ryanair_sweep.py roundtrip --from EIN    --out 2026-08-14 --back 2026-08-15
  ./ryanair_sweep.py combo    --from EIN,CRL,BRU,NRN,CGN,AMS \
                              --home EIN,CRL,BRU,NRN,DUS,CGN,AMS,MST,LGG,LIL \
                              --out 2026-08-14 --back 2026-08-15
  ./ryanair_sweep.py flex     --from EIN,CRL,BRU,NRN,CGN,DUS,MST,LIL \
                              --home EIN,CRL,BRU,NRN,DUS,CGN,AMS,MST,LGG,LIL \
                              --to FCO,CIA --out 2026-08-14 --back 2026-08-16 --span 3

Note: roundTripFares rejects limit > 16. oneWayFares accepts 100.
"""

import argparse
import datetime
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://services-api.ryanair.com/farfnd/v4"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch(path, **params):
    params.setdefault("currency", "EUR")
    params.setdefault("market", "en-ie")
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("fares", [])
    except Exception as e:
        print(f"  ! {path} {params.get('departureAirportIataCode', '')}: {e}")
        return []


def one_way(origin, date, limit=100):
    return fetch(
        "oneWayFares",
        departureAirportIataCode=origin,
        outboundDepartureDateFrom=date,
        outboundDepartureDateTo=date,
        limit=limit,
    )


def round_trip(origin, out_date, back_date):
    return fetch(
        "roundTripFares",
        departureAirportIataCode=origin,
        outboundDepartureDateFrom=out_date,
        outboundDepartureDateTo=out_date,
        inboundDepartureDateFrom=back_date,
        inboundDepartureDateTo=back_date,
        limit=16,  # hard maximum; 17+ returns InvalidLimit
    )


def hhmm(iso):
    return iso[11:16]


def cmd_oneway(args):
    for origin in args.origins:
        print(f"\n=== {origin} one-way, {args.out}")
        fares = one_way(origin, args.out)
        for f in sorted(fares, key=lambda x: x["summary"]["price"]["value"]):
            o = f["outbound"]
            print(
                f"  {o['price']['value']:7.2f}  {o['arrivalAirport']['iataCode']} "
                f"{o['arrivalAirport']['name'][:22]:24} "
                f"dep {hhmm(o['departureDate'])} arr {hhmm(o['arrivalDate'])}"
            )


def cmd_roundtrip(args):
    for origin in args.origins:
        print(f"\n=== {origin} round trip, {args.out} -> {args.back}")
        fares = round_trip(origin, args.out, args.back)
        if not fares:
            print("  (nothing)")
            continue
        for f in sorted(fares, key=lambda x: x["summary"]["price"]["value"]):
            o, i = f["outbound"], f["inbound"]
            print(
                f"  {f['summary']['price']['value']:7.2f}  "
                f"{o['arrivalAirport']['iataCode']} {o['arrivalAirport']['name'][:18]:20} "
                f"out {hhmm(o['departureDate'])} ({o['price']['value']:6.2f})  "
                f"back {hhmm(i['departureDate'])} ({i['price']['value']:6.2f})"
            )


def cmd_combo(args):
    """Out of any home airport, back into any home airport. The big saving."""
    home = set(args.home)

    cheapest_out = {}
    for origin in args.origins:
        for f in one_way(origin, args.out):
            o = f["outbound"]
            dest = o["arrivalAirport"]["iataCode"]
            rec = (o["price"]["value"], origin, hhmm(o["departureDate"]),
                   hhmm(o["arrivalDate"]), o["arrivalAirport"]["name"])
            if dest not in cheapest_out or rec[0] < cheapest_out[dest][0]:
                cheapest_out[dest] = rec

    ranked = sorted(cheapest_out.items(), key=lambda kv: kv[1][0])[: args.top]
    print(f"\nChecking return legs for the {len(ranked)} cheapest outbounds...")

    combos = []
    for dest, (price, origin, dep, arr, name) in ranked:
        best = None
        for f in one_way(dest, args.back):
            i = f["outbound"]
            if i["arrivalAirport"]["iataCode"] in home:
                rec = (i["price"]["value"], i["arrivalAirport"]["iataCode"],
                       hhmm(i["departureDate"]), hhmm(i["arrivalDate"]))
                if best is None or rec[0] < best[0]:
                    best = rec
        if best:
            combos.append((price + best[0], dest, name, price, origin, dep, best))

    print(f"\n=== cheapest mixed-airport pairs, {args.out} -> {args.back}")
    for total, dest, name, price, origin, dep, b in sorted(combos):
        print(
            f"  {total:7.2f}  {dest} {name[:20]:22} "
            f"OUT {origin}->{dest} {dep} {price:6.2f} | "
            f"BACK {dest}->{b[1]} {b[2]}-{b[3]} {b[0]:6.2f}"
        )
    if not combos:
        print("  (no pair found — widen --home or --top)")


def date_span(centre, span):
    d = datetime.date.fromisoformat(centre)
    return [(d + datetime.timedelta(days=n)).isoformat()
            for n in range(-span, span + 1)]


def cheapest_leg(pairs, keep):
    """pairs: (origin, date). keep: set of acceptable arrival airports.

    Returns {date: (price, origin, arrival, dep_time, arr_time)} — the cheapest
    qualifying flight on each date, across every origin given.
    """
    best = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda p: (p[1], one_way(p[0], p[1]), p[0]), pairs)
        for date, fares, origin in results:
            for f in fares:
                o = f["outbound"]
                arr = o["arrivalAirport"]["iataCode"]
                if arr not in keep:
                    continue
                rec = (o["price"]["value"], origin, arr,
                       hhmm(o["departureDate"]), hhmm(o["arrivalDate"]))
                if date not in best or rec[0] < best[date][0]:
                    best[date] = rec
    return best


def cmd_flex(args):
    """Price every date pair within +/- span days of both legs.

    The requested dates are almost never the cheapest ones. This finds out how
    much the calendar is costing, without leaving the week.
    """
    dests = set(args.dests)
    home = set(args.home)
    out_dates = date_span(args.out, args.span)
    back_dates = date_span(args.back, args.span)

    def usable(od, bd):
        """A date pair the traveller would actually accept."""
        nights = (datetime.date.fromisoformat(bd)
                  - datetime.date.fromisoformat(od)).days
        if not args.min_nights <= nights <= args.max_nights:
            return 0
        # An occasion anchors the trip: be there that whole day.
        if args.must_include and not (od <= args.must_include < bd):
            return 0
        return nights

    print(f"Sweeping {len(out_dates)} outbound dates x {len(back_dates)} return dates...")

    out_best = cheapest_leg(
        [(o, d) for d in out_dates for o in args.origins], dests)
    back_best = cheapest_leg(
        [(d_ap, d) for d in back_dates for d_ap in args.dests], home)

    if not out_best or not back_best:
        print("  (no flights found — check the --to codes)")
        return

    # The grid. Rows are outbound dates, columns are return dates.
    print(f"\n=== total return fare, out (rows) x back (columns)")
    print(" " * 13 + "".join(f"{d[5:]:>9}" for d in back_dates))
    cheapest, requested = None, None
    for od in out_dates:
        row = f"  {od[5:]}  "
        o = out_best.get(od)
        for bd in back_dates:
            b = back_best.get(bd)
            nights = usable(od, bd)
            if not o or not b or not nights:
                row += f"{'-':>9}"
                continue
            total = o[0] + b[0]
            row += f"{total:9.2f}"
            rec = (total, od, bd, nights, o, b)
            if cheapest is None or total < cheapest[0]:
                cheapest = rec
            if od == args.out and bd == args.back:
                requested = rec
        print(row)

    print(f"\n=== cheapest date pairs within +/-{args.span} days")
    combos = []
    for od, o in out_best.items():
        for bd, b in back_best.items():
            nights = usable(od, bd)
            if nights:
                combos.append((o[0] + b[0], od, bd, nights, o, b))
    for total, od, bd, nights, o, b in sorted(combos)[: args.top]:
        mark = " <- as asked" if (od == args.out and bd == args.back) else ""
        print(f"  {total:7.2f}  {od} -> {bd}  {nights}n  "
              f"OUT {o[1]}->{o[2]} {o[3]} {o[0]:6.2f} | "
              f"BACK {b[1]}->{b[2]} {b[3]} {b[0]:6.2f}{mark}")

    if requested and cheapest and requested[0] > cheapest[0]:
        saving = requested[0] - cheapest[0]
        pct = saving / requested[0] * 100
        print(f"\nMoving {cheapest[1]} -> {cheapest[2]} saves "
              f"{saving:.2f} EUR ({pct:.0f}%) against the dates you asked for.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["oneway", "roundtrip", "combo", "flex"])
    p.add_argument("--from", dest="origins", required=True,
                   help="comma-separated origin IATA codes")
    p.add_argument("--home", default="",
                   help="comma-separated IATA codes you can get home from")
    p.add_argument("--to", dest="dests", default="",
                   help="flex mode: destination IATA codes, e.g. FCO,CIA")
    p.add_argument("--out", required=True, help="outbound date, YYYY-MM-DD")
    p.add_argument("--back", help="return date, YYYY-MM-DD")
    p.add_argument("--span", type=int, default=3,
                   help="flex mode: days either side of each leg (default 3)")
    p.add_argument("--min-nights", type=int, default=1,
                   help="flex mode: shortest trip to consider (default 1)")
    p.add_argument("--max-nights", type=int, default=99,
                   help="flex mode: longest trip to consider (default 99)")
    p.add_argument("--must-include", default="",
                   help="flex mode: YYYY-MM-DD the traveller must spend at the "
                        "destination — the occasion the trip is built around")
    p.add_argument("--top", type=int, default=14,
                   help="how many results to print")
    args = p.parse_args()

    args.origins = [a.strip().upper() for a in args.origins.split(",") if a.strip()]
    args.home = [a.strip().upper() for a in args.home.split(",") if a.strip()] or args.origins
    args.dests = [a.strip().upper() for a in args.dests.split(",") if a.strip()]

    if args.mode in ("roundtrip", "combo", "flex") and not args.back:
        p.error("--back is required for roundtrip, combo and flex")
    if args.mode == "flex" and not args.dests:
        p.error("--to is required for flex")

    {"oneway": cmd_oneway, "roundtrip": cmd_roundtrip,
     "combo": cmd_combo, "flex": cmd_flex}[args.mode](args)


if __name__ == "__main__":
    main()

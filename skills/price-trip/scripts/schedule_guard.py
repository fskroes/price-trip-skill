#!/usr/bin/env python3
USAGE = """Catch a thin route before it becomes a wrong answer.

A low-cost carrier on a secondary base flies a route two or three days a week.
Ask for a price on a day it does not fly, and nothing looks broken: a real fare
comes back, from a legacy carrier, with a connection. The route looks expensive.
It is not expensive. It is closed.

Glasgow to Lisbon, November 2026, is the case this file exists for:

    GLA->LIS  2026-11-07   9 offers,  1 nonstop, cheapest EUR 42   easyJet
    GLA->LIS  2026-11-08  17 offers,  0 nonstop, cheapest EUR160   BA, 1 stop
    GLA->LIS  2026-11-09  15 offers,  0 nonstop, cheapest EUR160   BA, 1 stop

The group flew on the 8th, so the run rerouted two travellers to Edinburgh and
charged them EUR122 of fare plus a GBP16 coach. The right answer was EUR93 and
their own airport. Nobody spotted it. A traveller did, days later.

Read those three lines again. The proof was in the sweep the whole time. Every
number needed to reject the 8th was already on disk, at zero extra cost. What
was missing was not data. It was the question.

So this file asks the question, every time:

  audit     Read a saved sweep. Flag every route-day where the schedule, not the
            price, is what is expensive. Exit 1 while anything is unresolved.
  calendar  Sweep one route across a full range of days and print which days the
            nonstop actually operates.

The three checks, in order of how loudly they fire:

  NONSTOP GAP   The route has a nonstop on some day in the sweep, and none on
                this day. That is a timetable, not a market. BLOCKING.
  PRICE CLIFF   The cheapest fare on one day is >= --ratio times the cheapest on
                another day, same route, same sweep. BLOCKING.
  WINDOW EDGE   The cheapest day found is the first or last day swept, so the
                real floor may sit outside the window. Advisory.

Only the first two block, because only they prove something is wrong. The third
says the window was too narrow to know.

Usage:
  ./schedule_guard.py audit sweep.json
  ./schedule_guard.py audit sweep.json --ratio 2.0
  ./schedule_guard.py calendar --from GLA --to LIS --first 2026-11-05 --last 2026-11-12
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EDGE_RATIO = 1.8
FALLING = 0.80
WEEK = 7


def load_rows(path):
    with open(path) as fh:
        doc = json.load(fh)
    rows = doc.get("rows") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: expected a list of rows, or an object with 'rows'.")
    return rows


def carrier(opt):
    for k in ("name", "owner_name", "carrier", "airline"):
        v = opt.get(k)
        if v:
            return str(v)
    return "?"


def depart(opt):
    v = str(opt.get("dep") or "")
    return v[11:16] if "T" in v else v[:5]


def routes(rows):
    out = {}
    for r in rows:
        key = (r.get("origin", "?"), r.get("dest", "?"))
        opts = [o for o in (r.get("opts") or []) if isinstance(o.get("price"), (int, float))]
        nonstop = [o for o in opts if o.get("stops") == 0]
        out.setdefault(key, {})[r.get("date", "?")] = {
            "n": len(opts),
            "cheapest": min((o["price"] for o in opts), default=None),
            "nonstop": min(nonstop, key=lambda o: o["price"]) if nonstop else None,
            "nonstop_n": len(nonstop),
        }
    return out


def audit_route(origin, dest, days, ratio):
    block, warn = [], []
    dates = sorted(days)
    priced = [d for d in dates if days[d]["cheapest"] is not None]

    empty = [d for d in dates if days[d]["n"] == 0]
    if empty:
        warn.append(("NO DATA", f"no offers at all on {', '.join(empty)} — the sweep failed, "
                                f"or the airports do not connect"))
    if not priced:
        return block, warn
    known = len(priced) >= WEEK
    hold = warn if known else block

    flying = [d for d in priced if days[d]["nonstop"]]
    grounded = [d for d in priced if not days[d]["nonstop"]]
    if flying and grounded:
        best = min(flying, key=lambda d: days[d]["nonstop"]["price"])
        b = days[best]["nonstop"]
        if known:
            dow = sorted({date.fromisoformat(d).strftime("%a") for d in flying},
                         key=lambda x: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(x))
            warn.append((
                "THIN ROUTE",
                f"nonstop on {', '.join(dow)} only, from EUR{b['price']:.0f} ({carrier(b)}). "
                f"Swept {len(priced)} days, so this is the timetable, not a gap in the search. "
                f"Book an operating day."))
        else:
            block.append((
                "NONSTOP GAP",
                f"no nonstop on {', '.join(grounded)}, but {carrier(b)} flies it nonstop on "
                f"{best} for EUR{b['price']:.0f} at {depart(b) or '--:--'}. "
                f"The day is wrong, not the route. Only {len(priced)} days swept — "
                f"too few to know which days it flies."))

    lo = min(priced, key=lambda d: days[d]["cheapest"])
    hi = max(priced, key=lambda d: days[d]["cheapest"])
    if days[lo]["cheapest"] > 0 and days[hi]["cheapest"] / days[lo]["cheapest"] >= ratio:
        hold.append((
            "PRICE CLIFF",
            f"EUR{days[lo]['cheapest']:.0f} on {lo} against EUR{days[hi]['cheapest']:.0f} on "
            f"{hi} — {days[hi]['cheapest'] / days[lo]['cheapest']:.1f}x inside one window"
            + (f", across {len(priced)} days swept" if known else "")))

    if len(priced) > 1 and lo in (priced[0], priced[-1]):
        inner = priced[1] if lo == priced[0] else priced[-2]
        if days[lo]["cheapest"] <= FALLING * days[inner]["cheapest"]:
            warn.append((
                "WINDOW EDGE",
                f"EUR{days[lo]['cheapest']:.0f} on {lo} against EUR{days[inner]['cheapest']:.0f} "
                f"one day inside — still falling at the {'first' if lo == priced[0] else 'last'} "
                f"day swept, so the floor may sit outside the window"))
    return block, warn


def widen(dates):
    d = [date.fromisoformat(x) for x in sorted(dates) if x != "?"]
    if not d:
        return "--first <date> --last <date>"
    return f"--first {(d[0] - timedelta(days=3)).isoformat()} --last {(d[-1] + timedelta(days=3)).isoformat()}"


def mode_audit(a):
    rows = load_rows(a.sweep)
    by_route = routes(rows)
    n_days = sum(len(v) for v in by_route.values())
    print(f"=== schedule audit · {len(by_route)} routes, {n_days} route-days, "
          f"cliff ratio {a.ratio}\n")

    blocked = 0
    for (origin, dest), days in sorted(by_route.items()):
        block, warn = audit_route(origin, dest, days, a.ratio)
        for kind, msg in block:
            blocked += 1
            print(f"BLOCK  {origin}->{dest:<4} {kind}")
            print(f"       {msg}")
        for kind, msg in warn:
            print(f"warn   {origin}->{dest:<4} {kind}")
            print(f"       {msg}")
        if block:
            print(f"       fix: ./schedule_guard.py calendar --from {origin} --to {dest} "
                  f"{widen(days)}\n")
        elif warn:
            print()

    if blocked:
        print(f"{blocked} blocking finding(s). Sweep those routes across a full week "
              f"before you price a plan on them.")
        return 1
    print("No schedule problem found. Every route prices flat across the window, "
          "and no route loses its nonstop on any day swept.")
    return 0


def mode_calendar(a):
    from fullservice_sweep import PROVIDERS, scrappa_options

    key = os.environ.get(PROVIDERS["scrappa"]["env"])
    if not key:
        raise SystemExit("SCRAPPA_API_KEY not set. Get a free key at scrappa.co,\n"
                         "then add this to your shell profile and open a new shell:\n"
                         "  export SCRAPPA_API_KEY=...")
    first, last = date.fromisoformat(a.first), date.fromisoformat(a.last)
    if last < first:
        raise SystemExit("--last is before --first")
    dates = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    print(f"Pricing {len(dates)} live searches, free (Scrappa, 500 credits/month)...\n")

    def run(d):
        return d, scrappa_options(a.origin, a.dest, d, a.cabin, a.adults, key,
                                  currency=a.currency)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, dates))

    print(f"=== {a.origin} -> {a.dest}, {a.first} to {a.last}\n")
    print(f"  {'date':12} {'day':4} {'offers':>6} {'cheapest':>9}  nonstop")
    saved, operates = [], set()
    for d, opts in results:
        opts = [o for o in opts if isinstance(o.get("price"), (int, float))]
        saved.append({"origin": a.origin, "dest": a.dest, "date": d, "opts": opts})
        dow = date.fromisoformat(d).strftime("%a")
        if not opts:
            print(f"  {d:12} {dow:4} {0:>6} {'-':>9}  -")
            continue
        ns = [o for o in opts if o.get("stops") == 0]
        cheap = min(o["price"] for o in opts)
        if ns:
            b = min(ns, key=lambda o: o["price"])
            operates.add(dow)
            mark = f"EUR{b['price']:.0f} {carrier(b)} {depart(b)}"
        else:
            mark = "none"
        print(f"  {d:12} {dow:4} {len(opts):>6} {'EUR%.0f' % cheap:>9}  {mark}")

    order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    seen = {date.fromisoformat(d).strftime("%a") for d, _ in results}
    if operates:
        on = [x for x in order if x in operates]
        off = [x for x in order if x in seen and x not in operates]
        print(f"\nNonstop operates: {', '.join(on)}."
              + (f" No nonstop: {', '.join(off)}." if off else " Every day swept."))
        if off:
            print("Price the operating days. A day with no nonstop is a closed day, "
                  "not an expensive one.")
    else:
        print("\nNo nonstop on any day swept. This route genuinely connects — "
              "price the connection, or price a different airport.")

    if a.save:
        with open(a.save, "w") as fh:
            json.dump({"rows": saved}, fh, indent=1)
        print(f"\nSaved {len(saved)} route-days to {a.save}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    au = sub.add_parser("audit", help="check a saved sweep for schedule gaps")
    au.add_argument("sweep", help="JSON from fullservice_sweep.py oneway --save")
    au.add_argument("--ratio", type=float, default=EDGE_RATIO,
                    help=f"price-cliff trigger (default {EDGE_RATIO})")

    ca = sub.add_parser("calendar", help="sweep one route day by day")
    ca.add_argument("--from", dest="origin", required=True)
    ca.add_argument("--to", dest="dest", required=True)
    ca.add_argument("--first", required=True, help="YYYY-MM-DD")
    ca.add_argument("--last", required=True, help="YYYY-MM-DD")
    ca.add_argument("--cabin", default="economy")
    ca.add_argument("--adults", type=int, default=1)
    ca.add_argument("--currency", default="EUR")
    ca.add_argument("--save", default=None, help="write the route-days as JSON")

    a = p.parse_args()
    raise SystemExit(mode_audit(a) if a.mode == "audit" else mode_calendar(a))


if __name__ == "__main__":
    main()

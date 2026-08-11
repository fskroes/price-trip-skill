---
name: price-trip
description: When you want a complete, costed trip plan built from live prices instead of guesses. Give any subset of origin, destination, dates, number of nights, travel mode, and lodging type — the skill fills the gaps, sweeps live fares, sweeps every date within three days of each leg, prices beds and ground transport, and returns a full line-by-line cost breakdown plus a published visual page. Triggers on "/price-trip", "price this trip", "how cheap can I get to X", "I want to go somewhere", "plan a weekend away", "cheapest way to X for N nights", "cost out this trip", "find me a cheap escape", "what would a trip to X cost". Use for real trips with real prices — not for destination inspiration with no numbers.
metadata:
  version: 0.2.1
---

# /price-trip — Cost a real trip from live prices

Most trip advice fails in one of two ways. It quotes a headline fare and hides the
rest of the cost, or it lists destinations with no numbers at all. This skill does
neither. Every number comes from a live source, and every cost line appears in the
output, down to the tram fare.

The load-bearing rule: **price the whole door-to-door trip, or you have not priced
the trip.**

---

## Step 0 — Collect the parameters

Take whatever the user gave. Fill the rest with defaults. Never block on a missing
parameter — state the assumption and continue.

| Parameter | Default if not given |
|---|---|
| Origin | Ask once if unknown. Everything else can be assumed. |
| Destination | **Open** — the skill finds it. This is the interesting case. |
| Dates | Next weekend |
| Date flex | ±3 days on each leg |
| Nights | 1 |
| Travellers | 1 |
| Travel mode | No preference. Price air and ground both. |
| Lodging | Cheapest bed that is private if budget allows, else dorm |
| Budget | "As cheap as possible" |
| Style | Sensible: one real meal, not survival rations |

Two parameters change the shape of the whole run:

- **Destination open vs fixed.** Open means Step 2 becomes a fan-out over every
  reachable airport and city. Fixed means Step 2 prices one route hard, across
  several carriers and modes.
- **Nights.** One night is the hardest case, because the return leg carries the
  whole price. Three or more nights usually unlocks much cheaper fares. Always
  test one night either side of the requested length, and report the delta.

Also ask what the dates are anchored to, or infer it. A birthday, a wedding, or a
match fixes one day at the destination; everything else can move. That single fact
decides which half of the date grid in Step 2b counts. Record it as the occasion
date.

Restate the parameters in one line before you start work.

---

## Step 1 — Check the date for a price trap

Do this before any search. It changes what counts as a good price.

- Peak return days. A Sunday, or the Saturday of a summer weekend, prices the
  inbound leg two to five times above the outbound. Public holidays do the same.
- School holidays in the origin country.
- Big events at the destination.

If the dates sit on a trap, say so in the first line of the final output. The user
must know that the price is high because of the calendar, not because you searched
badly.

---

## Step 2 — Sweep live fares

Read `references/fare-sources.md` for the working endpoints, the exact parameters,
and the sources that do not work. Do not rediscover them.

Rules that matter more than the tooling:

1. **Price both legs separately.** One-way fares in each direction expose the trap
   that a round-trip price hides.
2. **Sweep every airport in ground range of the origin, not just the home one.**
   Two hours of bus can halve the fare.
3. **Try mixed-airport pairs.** Fly out of one airport, back into another. This is
   often the single biggest saving on a peak return day, and no booking site
   offers it.
4. **Price the ground floor.** Bus and train fares set the number that flying must
   beat. Without them there is nothing to compare against.

Run `scripts/ryanair_sweep.py` for the air side. It handles the one-way sweep, the
round-trip sweep, and the mixed-airport combination search.

---

## Step 2b — Sweep the dates, ±3 days on each leg

The requested dates are almost never the cheapest ones, and the traveller cannot
see that from a single quote. Run this every time. It is the second-biggest saving
in the skill, after the mixed-airport trick.

```
scripts/ryanair_sweep.py flex --from <home airports> --home <home airports> \
    --to <destination airports> --out <date> --back <date> --span 3 \
    --min-nights 1 --max-nights 6 --must-include <the occasion date>
```

It prices every departure date within three days of the outbound against every
return date within three days of the inbound — a seven-by-seven grid — and reports
the saving against the dates as asked.

Four rules that keep the grid honest:

1. **Flex both legs independently.** The trip length changes, and that is the
   point. A grid locked to the requested number of nights hides the cheapest cell.
2. **Anchor the occasion.** When the trip exists for a birthday, a wedding, or a
   match, pass `--must-include`. A fare that flies the traveller home on the
   morning of the event is not a cheaper trip, it is a different trip. Show those
   cells, mark them, and exclude them from the recommendation.
3. **Convert every candidate to a trip total before ranking.** The grid holds
   fares. An extra night carries a bed, a tourist tax, and a day of food. The
   cheapest fare in the window is regularly the most expensive trip on the page —
   say so with the two numbers side by side.
4. **Name the constraint that a cheap cell breaks.** A 05:50 departure needs a
   03:30 taxi. An eight-night window needs eight days off work. Price it or drop
   it.

Report the grid as: the cheapest cell, the cheapest cell that satisfies every
constraint, the requested cell, and the saving between the last two as a
percentage.

---

## Step 2c — Audit the sweep before you believe it

A price can be real and still be the wrong answer. A thin low-cost route flies two
or three days a week. Ask for a day it does not fly, and a legacy carrier answers
with a connection at three times the money. Nothing looks broken. The route looks
expensive. It is not expensive. It is closed.

`fullservice_sweep.py oneway` now runs this audit itself and **exits 1** when it
finds something. It costs no extra credits — the evidence is already in the
response, and the old code threw it away by keeping only the cheapest offer per
date.

```
scripts/fullservice_sweep.py oneway --from GLA,EDI --to LIS --out 2026-11-08 --span 1 \
    --save sweep.json          # audits automatically, exit 1 on a finding
scripts/schedule_guard.py audit sweep.json          # re-run it on any saved sweep
```

Three checks. Two block, one advises:

| Finding | Means | Blocks |
|---|---|---|
| **NONSTOP GAP** | the route has a nonstop on some day swept and none on this one | yes |
| **PRICE CLIFF** | cheapest fare swings 1.8x or more inside one window | yes |
| **WINDOW EDGE** | the cheapest day is at the edge and still undercutting its neighbour | no |

**The guard blocks on ignorance, not on thin routes.** Answer a finding by sweeping
that route across a full week:

```
scripts/schedule_guard.py calendar --from GLA --to LIS --first 2026-11-05 --last 2026-11-12
```

It prints the days the nonstop actually operates. Once a route has seven days of
data the timetable counts as known. The same evidence then stops blocking and
reports `THIN ROUTE — nonstop on Wed, Sat only` instead, and the audit passes. Seven
credits per suspect route.

**Do not price a plan on a blocked route, and do not send anybody to another
airport, until the audit exits 0.** Rerouting on a closed day is why this step
exists. Glasgow to Lisbon, November 2026, priced at €160 with a connection on the
group's Sunday. easyJet flies that route Wednesday and Saturday for €42. Two
travellers went to Edinburgh, 80 km away, on a £16 coach and a 04:30 alarm, to
solve a problem that did not exist. A traveller caught it days later, from their
own knowledge of the route. Nothing in the run caught it, because nothing in the
run asked.

---

## Step 3 — Price the bed

Match the lodging type to the parameter, and price the real dates, not a "from"
price. A "from €4" figure on an aggregator is marketing, not availability.

- Hostels: Hostelworld accepts dates in the URL and renders server-side. See
  `references/fare-sources.md`.
- Hotels and Airbnb: the big sites block automated reads. Get a real number where
  you can, and where you cannot, use a stated band for that city and that night of
  the week, marked clearly as an estimate to verify.
- Always price a private room as well as a dorm. Report both. A birthday or an
  anniversary in a six-bed dorm is a false economy.

---

## Step 4 — Build the complete cost breakdown

Read `references/cost-model.md` for the required line items. The short version:
every trip has four cost groups, and a breakdown missing any of them is wrong.

1. **Travel** — every ticket that moves you, including the airport train at both
   ends, which is where most estimates leak money.
2. **Bed** — per night, with the private-room alternative next to it.
3. **On the ground** — food, drinks, local transport, one real meal.
4. **Fees** — bags, seats, tourist tax, card charges.

Produce a ranked table of three to five complete plans. Rank by total cost. Show
the cheapest plan, the cheapest plan that satisfies the stated travel preference
even when it is not the cheapest overall, one plan that moves the dates inside the
Step 2b window, and one plan that changes a parameter to save real money — usually
the nights.

State the price-check date on every output. Fares move within hours.

---

## Step 5 — Publish the visual page

Build one self-contained page and publish it with the Artifact tool. Load the
`artifact-design` skill first, and `dataviz` before writing any chart.

The page carries, in order:

1. A verdict block: cheapest plan, cheapest plan with the preferred travel mode,
   cheapest single ticket found, and the date-trap penalty as a percentage.
2. A stacked bar per plan — travel, bed, on the ground — so the shape of each
   trip is visible before any number is read.
3. One card per plan, holding the full line-by-line table.
4. A chart of the price trap itself: outbound against return, per route. This is
   the finding that changes the decision, so it gets its own section.
5. The date grid from Step 2b as a heatmap — outbound dates down, return dates
   across, the fare in each cell. Fade the cells that break a constraint rather
   than deleting them; the traveller needs to see what they are turning down.
   Outline two cells: the pick, and the dates as asked. Follow it with a small
   table converting the best few cells into full trip totals, because the grid
   holds fares and the decision is about totals.
6. A numbered booking checklist, and the price-check date in the footer.

The heatmap ramp is sequential, so it is one hue from pale to deep, dark meaning
dearer. Never a rainbow, never the categorical series colours. Every cell carries
its number as text, so colour is never the only encoding.

The checklist is a numbered list where each item is a bold action followed by one
plain sentence of reasoning. If it is laid out as a CSS grid, place the number,
the action, and the reasoning explicitly with `grid-column` and `grid-row`. Grid
auto-placement drops the reasoning into the number column, which is the one layout
bug this page reliably produces.

Design notes that carry the register: monospace for every number with
`tabular-nums`, sans for every sentence. It should read like a timetable, not a
brochure. Validate the chart palette with the `dataviz` validator for both themes
before shipping.

Skip this step only if the user asks for numbers alone.

---

## Step 6 — Close with one action

End with a single next action that takes under two minutes, and name the one thing
that will get more expensive if it waits. Budget-airline fares and bus seats both
climb as inventory sells, not as the date nears.

---

## Composes with

- **`artifact-design`** — required before writing the page in Step 5.
- **`dataviz`** — required before writing any chart. Run its palette validator.
- **`deep-research`** — call when the destination is open and the user wants a
  reason to pick one city over another, beyond price.
- **`decide`** — call when two plans land within ten percent of each other and the
  choice is about something other than money.
- **`personal-cfo`** — call when the trip is large enough to model against a real
  budget rather than a single line.

## Quality notes

- **Never quote a fare without its date and direction.** "€27 to Bologna" is not a
  fact. "€26.95, Eindhoven to Bologna, out 14 Aug 18:25, checked 5 Aug" is.
- **Report the number that kills the plan.** A trip that costs three times the
  ground alternative is a finding, not a failure. Say it in the first line.
- **Do not hide a logistics failure inside a low price.** A cheap fare needing an
  06:25 departure from an airport with no night transport is not cheap. Price the
  extra night, or drop the option.
- **Correct your own totals when a line item appears late.** Restate the new total
  plainly, once.
- **Source list at the end, every time.**

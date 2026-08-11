# Fare sources — what works, what does not

Verified 5 August 2026. Each entry says what it gives, and what it costs you in time.

---

## Air — Ryanair fare finder (works, no key, the workhorse)

Public JSON. No authentication. Returns real bookable fares, not "from" prices.

**One-way sweep from one airport, all destinations, one date:**

```
https://services-api.ryanair.com/farfnd/v4/oneWayFares
  ?departureAirportIataCode=EIN
  &outboundDepartureDateFrom=2026-08-14
  &outboundDepartureDateTo=2026-08-14
  &currency=EUR&limit=100&market=en-ie
```

**Round-trip sweep, cheapest pair per destination:**

```
https://services-api.ryanair.com/farfnd/v4/roundTripFares
  ?departureAirportIataCode=EIN
  &outboundDepartureDateFrom=2026-08-14&outboundDepartureDateTo=2026-08-14
  &inboundDepartureDateFrom=2026-08-15&inboundDepartureDateTo=2026-08-15
  &currency=EUR&limit=16&market=en-ie
```

Gotchas that cost time if you meet them cold:

- `limit` on **roundTripFares maxes at 16**. Anything higher returns
  `{"code":"InvalidLimit"}`. `oneWayFares` accepts 100.
- The endpoint only accepts a **departure** airport. To price a return leg into a
  set of home airports, query the destination as the departure airport and filter
  the arrivals yourself.
- Airports with no Ryanair base return an empty body, not an error.
- Times come back as local ISO strings in `departureDate` / `arrivalDate`.

### The mixed-airport trick

The single highest-value move in the whole skill. On a peak return day the
symmetric round trip is often twice the price of an asymmetric one.

Method: sweep one-way fares out of every home-range airport on the outbound date.
Then, for each destination found, sweep one-way fares out of that destination on
the return date and keep any arrival inside the home-range set. Add the two
cheapest.

Worked example, 14–15 August 2026, Amsterdam origin:

| Booking shape | Price |
|---|---|
| Round trip, Eindhoven → Bologna | €218.62 |
| Round trip, Charleroi → Milan Bergamo | €112.98 |
| Out Charleroi → Bergamo, back Bergamo → Weeze | **€81.89** |

### The date grid

`oneWayFares` accepts a date range, but it collapses the range to one cheapest
fare per destination, so a range gives no per-day resolution. Loop the dates one
at a time instead. Seven outbound dates across a dozen origins is about a hundred
calls, which the script runs on eight threads in roughly fifteen seconds.

Worked example, Amsterdam to Rome, 14–16 August 2026 requested:

| Dates | Nights | Fare |
|---|---|---|
| 14 → 16 Aug, as asked | 2 | €249.60 |
| 13 → 15 Aug, one day earlier | 2 | €191.26 |
| 12 → 14 Aug, cheapest cell in the window | 2 | €138.65 |

The €138.65 cell flies home on the morning of the 14th, which is the day the trip
exists for. `--must-include 2026-08-14` removes it. This is why the flag exists.

`scripts/ryanair_sweep.py` implements all four sweeps.

### Home-range airport sets

Build the set from the origin, by ground travel time, not by country.

- **Amsterdam**: EIN, AMS, RTM, CRL, BRU, NRN, DUS, CGN, MST, ANR, LGG, LIL
- Add any airport inside roughly three hours of cheap ground transport. Always
  price that ground leg — Weeze and Charleroi look cheap until you add it.

---

## Air — full-service carriers (KLM, Swiss, Lufthansa, BA, ITA …)

These file published fares to **ATPCO**, the airline-owned clearinghouse, which
distributes to the GDSs and to NDC sellers. So they are reachable — but only
through a commercial pipe, never by scraping. `scripts/fullservice_sweep.py`
implements the two that a single developer can actually open.

**The two-layer method. Do not skip layer 1.**

| Layer | Provider | Cost | Freshness | Job |
|---|---|---|---|---|
| 1. Find the carriers | Travelpayouts / Aviasales Data API | free | cached, ≤ 7 days | who flies it, and in what fare band |
| 2. Price the dates | **Scrappa** (default) | free, 500/month, recurring | live | the actual date grid, and the finalists |
| 2b. If you burn 500 | Ignav | free, 1,000 one-time, then $2/1000 | live | overflow |
| 2c. Price to book | Duffel | $0.005 / search | live, bookable | only when you need a bookable offer |

**AF-KLM Open Data Offers** sits alongside these and is the best free source for
KLM specifically: `POST /opendata/offers/v3/available-offers`, auth is an
`API-Key` header only — no OAuth — plus `AFKL-TRAVEL-Host: KL` and
`AFKL-TRAVEL-Country: NL`. **5,000 calls/day free.** Note `origin.airport.code`,
not `origin.code`; the nesting is the thing that breaks first.

Its `lowest-fare-offers` endpoint with `"type": "DAY"` and a `dateInterval`
returns **a whole date range in one call**, which is why `calendar` mode exists.
A 7-day sweep costs 1 call instead of 7.

Two limits: it returns **AF/KL-marketed flights only**, so it prices the KLM
option authoritatively but is not a market sweep — pair it with Scrappa. And the
key needs manual approval with no published SLA; request the *Offers* product
explicitly, because a signup granted only Flight Status is useless here. Treat
that approval as the go/no-go gate before building on it.

Scrappa is the default because its free tier **recurs monthly** and needs no card.
Ignav is the overflow only — its 1,000 is a one-time allowance, then $2 per 1,000.
Reach for Duffel only when the output has to be a bookable offer, not a price.

**Scrappa contract, verified live 6 Aug 2026.** It serves Google Flights data.

`GET /api/flights/one-way` and `/api/flights/round-trip`, `X-API-KEY` header.

Three traps, all of which cost real time to find:

1. **Send a User-Agent.** Scrappa sits behind Cloudflare, which rejects urllib's
   default agent with `403 error code: 1010` — a banned *browser signature*, not a
   bad key. The message reads exactly like an auth failure and is not one. Any
   normal UA string passes.
2. **`max_stops` takes words, not numbers**: `any | nonstop | one_or_fewer |
   two_or_fewer`. Passing `stops=0` is accepted and then silently ignored, so you
   get connecting flights while believing you filtered them out. The API validates
   the word form and names the legal values in the error — a wrong value is safer
   than a wrong parameter name.
3. **`round-trip` returns the full round-trip total per outbound option**, with
   `trip_type: "one_way"` and `return_legs: []`. That is Google's step-1 model:
   it prices the whole trip when you pick the outbound, before a return is chosen.
   The empty `return_legs` is not a bug and the price is not one-way. So a date
   grid costs one call per pair, not two.

`currency`, `gl` and `hl` are all honoured — pass `currency=EUR&gl=nl` or you get
USD priced against the US market. `search_metadata` echoes every parameter the
server actually applied; diff it against what you sent to catch silent drops.

Response shape: `{flights: [...], search_metadata: {...}}`, each flight
`{price, currency, stops, airline_name, total_duration_minutes, emissions,
booking_token, legs[], outbound_legs[], return_legs[]}`, legs carrying
`{airline, flight_number, departure_airport, arrival_airport, departure_time,
arrival_time}`. `price` is a plain number. Fully mapped in `_scrappa_summary`.

**Layer 1 is carrier discovery, not grid fill.** Measured on AMS→AGP, 5 Aug 2026:
a 7×7 return grid came back with 3 of 49 cells populated, and AMS→ZRH with 1 of
49. A one-way sweep over 25 dates filled 17. The cache holds only what real
Aviasales users searched in the last seven days, so return pairs are sparse and
the gaps are arbitrary — they are not "no flights", and the cheapest cached fare
is not the cheapest fare, only the cheapest one somebody happened to look up.

What it is genuinely good at is telling you **which airlines fly a route at all**,
with a real fare band. On AMS→AGP it surfaced Transavia (HV), easyJet (U2/EC),
Vueling (VY) and Ryanair (FR) in a €50–107 one-way band — four carriers, three of
which no automated source in this file could previously price.

Accuracy where it does have data is good. Ryanair AMS→AGP 22→28 Aug returned
€142.86 from Ryanair's own fare API and €143.00 from the Travelpayouts cache on
the same day.

So: run layer 1 to learn the carrier set and the band, then price the real grid on
Scrappa for nothing. Do not present a cached grid as the answer.

**Sweep both legs separately before you sweep pairs.** Málaga, 6 Aug 2026: a
one-way sweep of 4 origins out and 6 airports back cost ~15 credits and priced
every mixed-airport combination, because any out can pair with any back. The
equivalent round-trip grid costs one call per pair and cannot express a mixed
pair at all. Use `round-trip` only to confirm the two or three finalists.

**One-way-first is a SHORT-HAUL rule. Long-haul inverts it.** Ryanair, easyJet,
Vueling and Transavia price each direction on its own, so two one-ways equal the
round-trip and the sweep above is free money. Legacy long-haul does the opposite:
it fences the cheap buckets behind a return requirement. Dubai to Lisbon,
6 Aug 2026, identical flights on identical dates — **two one-ways €914, the
round-trip €605**. A 34% penalty for buying the same seats in two transactions.
So: sweep one-way to find the shape, then re-price any leg over ~4 hours as a
round-trip before you total anything. On a mixed group the long-haul traveller is
usually most of the bill, which makes this the highest-value single call in the run.

**On a multi-origin trip, price convergence rather than assuming it costs.**
Lisbon, six people from five countries, 6 Aug 2026: the cheapest fare for each
person beat the everyone-lands-together set by only €31.70 across the group —
€5.28 a head. The cheap fares clustered at dawn and near midnight, and they
carried the expensive ground: a €58 fare out of Eindhoven needs a €24.50 transfer
from Amsterdam, and an €80 flight leaving Lisbon at 05:00 needs a €15 taxi
because the metro runs 06:30–01:00. Fare ranking and door-to-door ranking put
these in a different order. Always report the convergence premium as a per-person
number; it is usually small enough to change the decision.

**A thin low-cost route flies two or three days a week. `schedule_guard.py` now
enforces this — do not carry it as a rule you have to remember.** Glasgow to
Lisbon, Nov 2026: on the group's date the cheapest seat was €160, so the run
rerouted those travellers to Edinburgh. Swept across a full week, easyJet — the
only nonstop operator — flew that route on **Wednesday and Saturday only**, at
€40–42. Every other day sold nothing under €149. The fare was never missing; the
*day* was. A ±3 day window centred on the group's date misses this when the route
runs twice a week, and the failure is silent: a real price comes back, so nothing
looks wrong.

Two things had to change, and both are code, not advice:

1. **Keep every offer.** `mode_oneway` used to reduce each date to its cheapest
   fare and discard the rest. That threw away the only evidence that mattered —
   *seventeen offers on the 8th and not one of them nonstop*. `scrappa_options()`
   returns the whole list for the same single credit. Never reduce a sweep to its
   winner before you have audited it.
2. **Ask the question automatically.** `oneway` runs `schedule_guard.audit` on its
   own output and exits 1 on a finding. The proof was on disk the whole time in
   the original run; what was missing was the question, so the sweep asks it now.

The guard blocks on ignorance, not on thinness: a route with seven days of data
reports its timetable and passes. See Step 2c in `SKILL.md`. Seven credits per
suspect route; it moved the Glasgow pair from €164.06 to €118.54 door to door, each.

**Price every carrier, or do not state a conclusion.** The first Málaga run used
Ryanair's API alone and concluded €182.89, Ryanair both ways. Priced across all
carriers the same trip was €153.08 — Ryanair out, **KLM home for €93**, against
Ryanair's €154 for the same airport on the same day. The €61 gap was invisible,
not mispriced. A single-carrier source does not give you a cheap answer; it gives
you a confident wrong one. The full-service carrier can undercut the low-cost one
on the peak leg, because it has more capacity to fill and prices directional
demand rather than every seat.

```
export TRAVELPAYOUTS_TOKEN=…   # free signup, affiliate account, no review
export DUFFEL_TOKEN=…          # dashboard token, self-serve, test or live

scripts/fullservice_sweep.py grid   --from AMS --to ZRH --out 2026-09-12 --back 2026-09-15
scripts/fullservice_sweep.py verify --from AMS --to ZRH --pairs 2026-09-12:2026-09-15
```

Gotchas that cost time if you meet them cold:

- **Travelpayouts only holds routes real users searched in the last 7 days.** A
  quiet route returns an empty list — that is not an error, it is no cache. Fall
  back to a live `oneway` sweep.
- **Duffel test-mode data is fake.** Test tokens return "Duffel Airways" with
  invented schedules and prices. Any real number needs a live token.
- **Duffel bills per booking, not per seat-search** — $3 per order plus 1% of
  order value. Searches are metered separately at $0.005 with 1,500 free per
  booking per month. If you never book through them, every search is billed.
- **KLM is a Duffel direct connect; Swiss and Lufthansa arrive over NDC.** Without
  IATA/ARC accreditation you consume them through Duffel's own accreditation
  ("Managed Content"), which carries a fee. Fine for pricing, relevant for booking.
- `flex` mode refuses to run above `--max-spend` (default $2.00). Deliberate — it
  is the one mode that can quietly spend money in a loop.

### Hidden-city ticketing (skiplagging) — measured, does not pay. Do not re-test.

Buy A→C routing through B, leave the aircraft at B. It needs a fare to the far
city that undercuts the fare to the near one.

Method, 9 August 2026: for five origin→hub pairs, sweep one-way fares to twelve
beyond-points each, keep **every** itinerary that routes through the hub as an
intermediate stop, and compare against the cheapest direct fare on the same date.
65 credits, two dates (6 and 8 November 2026).

| Leg | Cheapest direct | Cheapest fare routing *through* it | Premium |
|---|---|---|---|
| LIS→AMS | €73 KLM nonstop | €100 LIS-AMS-CPH | +37% |
| LIS→DXB | €415 Aegean via ATH | €492 LIS-DXB-DEL | +19% |
| AMS→ZRH | €169 KLM nonstop | €227 AMS-ZRH-ATH | +34% |
| GLA→LIS | €150 BA via LHR | €701 GLA-AMS-RIX-LIS-PDL | +367% |
| DXB→LIS | €435 Eurowings via STR | €623 DXB-WAW-LIS-FAO | +43% |

**91 hub-routing itineraries. Not one cheaper than flying direct.** The best case
lost by €27.

The failure is structural, not a thin sample. Skiplagging needs a monopoly hub
against a competitive beyond-market. European hubs are not monopolies: Amsterdam
to Lisbon is €73 direct because TAP, Transavia, easyJet and Ryanair all fly it, so
there is no fortress premium to arbitrage. Emirates prices past Dubai as extra
distance, not as a discount — DXB→LIS→OPO is €725 against €435 to Lisbon. The
tactic is largely a US-domestic artefact of single-carrier hubs.

It also fails this skill's own rules even where it wins. Carry-on only, because
the bag flies on. One traveller per booking. A cancelled return if the skipped
segment is the outbound, so only the homeward leg is ever available. And the
airline can recalculate the fare or close the loyalty account. *Do not hide a
logistics failure inside a low price* rules it out on its own.

**Second finding from the same sweep, and this one is load-bearing: the hub
carrier loses its own route.** Cheapest LIS→DXB was **Aegean via Athens, €415** —
not Emirates. Cheapest DXB→LIS was **Eurowings via Stuttgart, €435** — not
Emirates and not TAP. Both hub carriers were beaten on their own hub, on the same
sweeps. More evidence for *price every carrier, or do not state a conclusion*.

## Air — what is dead or closed

| Source | Status |
|---|---|
| **Hidden-city / skiplagging** | **Measured 9 Aug 2026, does not pay.** 91 hub-routing fares across 5 routes, none cheaper than direct; best case lost €27. European hubs face low-cost competition, so there is no fortress premium to arbitrage. Evidence and reasoning in the section above. Do not re-test. |
| **Amadeus Self-Service** | **Dead.** Portal decommissioned 17 July 2026, keys disabled. New registrations paused before that. Enterprise only, which wants IATA/ARC accreditation. Do not recommend it. |
| Google Flights / QPX Express | Public API retired 10 April 2018, never replaced. ITA engine is enterprise-only. No API exists in 2026. |
| **Lufthansa Open API** (covers LH/OS/EW, maybe LX) | **Dead end, checked 5 Aug 2026.** Three independent blockers: developer registration is disabled site-wide ("on hold until further notice"), the GTC forbids redistributing or commercialising the data and bans screen scraping outright, and stated eligibility excludes tools "similar to actual meta searchers". Swiss (LX) fare coverage is not even confirmed — `/product` lists fares for Austrian, Eurowings and Lufthansa only, and adds Swiss solely for flight *status*. Do not spend time here. |
| Wizz Air API | Version-pinned host paths, 404 on guesses. Not worth the time. |
| easyJet availability API | Endpoint removed. Returns a 404 page. |
| Transavia | Cloudflare challenge on every path. |
| Kiwi / Skypicker open API | 404, retired. Tequila needs partner approval. |
| Skyscanner Travel API | Real, but partner-gated behind a commercial agreement. |
| Skyscanner, Kayak, Momondo pages | JavaScript rendered, bot-blocked. |

## Air — the legal line

Ryanair's public fare finder (above) has no auth and no terms gate, and is fine
for pricing your own trip. It is **not** a foundation for a product: the Irish
High Court has ruled OTA screen-scraping unlawful, and Ryanair's action against
eDreams was cleared to proceed on 22 July 2026. Booking.com, lastminute.com and
Kiwi took distribution agreements instead. If a run is going to be shipped to
other people, move the low-cost side onto a licensed pipe too, or say plainly in
the output which carriers are unpriced.

---

## Rail — Deutsche Bahn REST (works when up)

`https://v6.db.transport.rest/journeys?from=…&to=…&departure=…` returns journeys
with Sparpreis prices, no key. It returned 503 on 5 August 2026. Test
`/locations?query=…` first and fall back to published fares if it is down.

---

## Rail — published fares (reliable, low resolution)

`nsinternational.com` route pages carry a real floor fare per route, and
`ns.nl/en/routes/<a>-to-<b>` carries the domestic fare. These are floors, not live
availability, so mark them as such. Useful reference points, checked 5 Aug 2026:

- Amsterdam → Antwerp €20 one way, 1 h 15
- Amsterdam → Brussels €25 one way
- Amsterdam → Eindhoven €25.60 one way (the bus is less than half this)

---

## Bus — FlixBus route pages (works, WebFetch)

`https://www.flixbus.com/bus-routes/bus-<origin>-<destination>` renders
server-side. Each page gives the floor fare, duration, departures per day, and
first and last departure. The last two matter as much as the price: they decide
whether a late return is possible at all.

Two traps:

- The `.com` host prices in **USD** by geography. `global.flixbus.com` often
  returns EUR. Convert, and mark converted figures as approximate.
- **First and last departure kill more plans than price does.** Amsterdam to
  Charleroi has no night service, first bus 08:05 — which makes every 06:25 flight
  out of Charleroi a two-night trip in disguise.

Reference floors from Amsterdam, checked 5 Aug 2026: Brussels ~€16, Antwerp ~€17,
Ghent ~€21, Cologne ~€27, Paris €27.98.

---

## Beds — Hostelworld (works, dates in the URL)

```
https://www.hostelworld.com/hostels/europe/<country>/<city>/?from=2026-08-14&to=2026-08-15&guests=1
```

Renders server-side with per-property prices for the real dates, dorm and private
separately. The best automated bed price available.

Booking.com and Airbnb are bot-blocked. Aggregator "from €4" pages are marketing —
never quote them.

---

## Airport transfers — the line everyone forgets

Price both ends, both ways. It is routinely 10 to 20 percent of a cheap trip and
it is the most common reason a quoted total is wrong.

Checked 5 Aug 2026: Marconi Express, Bologna airport to Bologna Centrale, €12.80
single and €23.30 return.

---

## Method notes

- Fetch JSON APIs with `curl` in Bash and parse in Python. Use WebFetch for
  rendered pages. Do not use WebSearch for prices — it returns US-market results
  and marketing floors.
- WebSearch is still right for one thing: the calendar. Holidays, events, school
  breaks.
- Timestamp every price with the date you checked it. Budget-airline fares move
  within hours.

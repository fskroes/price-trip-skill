# price-trip

A Claude Code skill that costs a real trip from live prices.

Most trip advice fails in one of two ways. It quotes a headline fare and hides the
rest of the cost, or it lists destinations with no numbers at all. This skill does
neither. Every number comes from a live source, and every cost line appears in the
output, down to the tram fare.

## Install

This is a private marketplace. Ask the owner for access first, then:

```
/plugin marketplace add fskroes/price-trip-skill
/plugin install price-trip@price-trip-skill
```

Claude Code clones over your own git credentials. Run `gh auth login` once if you
have not already.

## Keys

Two free keys unlock the full-service carriers and the timetable audit. The
Ryanair sweep needs no key and works without them.

| Variable | Where | Cost |
|---|---|---|
| `SCRAPPA_API_KEY` | scrappa.co | free, 500 credits/month, no card |
| `TRAVELPAYOUTS_TOKEN` | travelpayouts.com | free signup |

Put them in your shell profile, not in a file inside the plugin. A plugin update
replaces the installed directory and would delete the file.

```
export SCRAPPA_API_KEY=...
export TRAVELPAYOUTS_TOKEN=...
```

Two more are optional: `AFKLM_API_KEY` (developer.airfranceklm.com, manual
approval) and `DUFFEL_TOKEN`.

**Use your own keys.** A shared key drains one 500-credit monthly quota for
everybody on it.

## Use

```
/price-trip Amsterdam to Lisbon, 3 nights in November, five of us
```

Give any subset of origin, destination, dates, nights, travel mode, and lodging.
The skill fills every gap with a default and states the assumption. Only the
origin makes it ask.

Run it from the directory where you want the output. It writes `runs/` and
`evidence/` there.

## What it does that a booking site does not

1. **Mixed-airport pairs.** Fly out of one airport, back into another. No booking
   site sells this, and it is often the biggest single saving on a peak return day.
2. **A 7x7 date grid.** Every departure date within three days against every return
   date within three days, with each cell converted to a full trip total. The
   cheapest fare in the window is regularly the most expensive trip on the page.
3. **A timetable audit.** It blocks when a route has no nonstop on your day, so a
   closed route never looks like an expensive one.
4. **The whole door-to-door cost.** Airport trains at both ends, bags, seats,
   tourist tax, one real meal.

Output is a ranked table of complete plans plus a published visual page: a verdict
block, cost bars per plan, the date grid as a heatmap, and a booking checklist.

## Scope

This prices your own travel. It is not a foundation for a commercial product.
Ryanair's public fare finder has no auth and no terms gate, but the Irish High
Court has ruled OTA screen-scraping unlawful, and Ryanair's action against eDreams
was cleared to proceed in July 2026. `skills/price-trip/references/fare-sources.md`
records which sources are open, which are closed, and why.

Fares move within hours. Every output carries its price-check date. Treat any
number older than a day as a guess.

## License

MIT. See `LICENSE`.

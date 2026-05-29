# Reference validation — NWS vs Open-Meteo (forward-accumulating)

How often each reference source's daily-max lands in the **resolved winning bucket** (2°F wide). NWS METAR has ~2-day retention, so this table GROWS each time the harness is run (designed for a daily cron).

| source | scored days | pending | match rate | mean signed bias (°F) |
|---|--:|--:|--:|--:|
| nws | 0 | 2 | — | None |
| open_meteo | 17 | 2 | 35% | 1.36 |

_Scored = compared against a resolved winner. Pending = reference captured, awaiting resolution (NWS only reaches the unresolved present, so it scores forward — re-run daily). Reference daily-max is rounded to a whole °F before bucket assignment, matching how the resolver reports the high._

**Interpretation:** even with correct whole-degree rounding, Open-Meteo matches the 2°F winning bucket only **35%** of the time, dragged down by a systematic **+1.36°F** warm bias vs the station resolver — it lands one bucket too high. NWS METAR is the actual station class used to resolve, so it should match far better; it can only be scored AFTER each day resolves, so re-run this daily to accumulate NWS evidence before trusting NWS for live P1.

## Recent comparisons

| date | source | ref_max | ref_bucket | actual_winner | result |
|---|---|--:|---|---|:--:|
| 2026-05-14 | open_meteo | 68.2 | 68-69°F | 66-67°F | ✗ |
| 2026-05-15 | open_meteo | 67.5 | 68-69°F | 64-65°F | ✗ |
| 2026-05-16 | open_meteo | 79.5 | 80-81°F | 76-77°F | ✗ |
| 2026-05-19 | open_meteo | 95.4 | 95°F or below | 95°F or below | ✓ |
| 2026-05-20 | open_meteo | 95.2 | 94-95°F | 94-95°F | ✓ |
| 2026-05-21 | open_meteo | 68.9 | 68-69°F | 66-67°F | ✗ |
| 2026-05-22 | open_meteo | 68.2 | 68-69°F | 68-69°F | ✓ |
| 2026-05-23 | open_meteo | 58.1 | 58-59°F | 56-57°F | ✗ |
| 2026-05-24 | open_meteo | 57.6 | 58-59°F | 56-57°F | ✗ |
| 2026-05-25 | open_meteo | 74.2 | nan | 70°F or higher | ✗ |
| 2026-05-26 | open_meteo | 81.4 | 80-81°F | 80-81°F | ✓ |
| 2026-05-27 | open_meteo | 84.9 | 84-85°F | 84-85°F | ✓ |
| 2026-05-28 | open_meteo | 78.0 | 78-79°F | — | pending |
| 2026-05-28 | nws | 75.2 | 74-75°F | — | pending |
| 2026-05-29 | open_meteo | 79.2 | 78-79°F | — | pending |
| 2026-05-29 | nws | 78.8 | 78-79°F | — | pending |

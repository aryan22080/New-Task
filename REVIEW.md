# REVIEW.md

Review of the starter `app/main.py`. Everything below was fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Default client returns **naive** datetimes; the whole file mixes naive local time with `IST`-aware values. | Server not on UTC: stored punch times shift by the server's offset; `compute_overtime` raises `TypeError` (naive minus aware). | `tz_aware=True`; every instant goes through `as_utc` / `trunc_seconds` / `ms_to_dt` / `dt_to_ms`. |
| 2 | `punch_in`: `datetime.fromtimestamp(ms/1000)`, `datetime.now()` | Uses the server's local zone, so the attendance `date` is not the IST date (R1). Seconds are not truncated. | Punch-in at 23:00 IST on a UTC server is stored under the wrong date. `.900` ms changes late minutes. | `ms_to_dt` (UTC, whole seconds) then `attendance_date()` in IST. |
| 3 | `punch_in`: no overnight rule | Early-morning punch-in of a 22:00-06:00 shift is dated the same day, not the previous day. | Punch-in 03:00 IST for EMP0005 gives the wrong `date` and `late_minutes` 0. | `attendance_date()` applies R1; late is measured from that day's shift start. |
| 4 | `compute_late_minutes` | Takes `int(seconds/60) > 10`, so 09:40:01 gives 0 (should be 10). Builds shift start on the punch-in's own date (breaks overnight). Uses the punch-in's own tz. | 09:40:01 against 09:30 returns 0; overnight late is wrong. | Compare **seconds** `> 600`, then `floor(delta/60)`, from the IST shift start of the attendance date. |
| 5 | `compute_work_hours` | Python `round()` is banker's rounding on a float. Microseconds not truncated. | 29250 s = 8.125 h returns 8.12, should be 8.13. | Decimal arithmetic, `ROUND_HALF_UP`, whole seconds. |
| 6 | `compute_overtime` | No 30-minute minimum (R3), end always on the record date (wrong for overnight), `max(0, int())` on floats. | Out at 18:40 for a 18:30 shift returns 10, should be 0. | `delta // 60`, kept only if `>= 30`; shift end moves to the next day for overnight shifts. |
| 7 | `compute_*` (no half-day helper) | `half_day` never computed from the rounded hours (R5). | Needed for punch-out. | `compute_half_day` on the rounded `work_hours`; `derive()` computes all derived fields in one place. |
| 8 | `health` | Always returns ok, even with MongoDB down. | Stop Mongo: still 200. | Pings Mongo, 503 on failure. |
| 9 | `create_employee` | `find_one` then `insert_one` is a race; no unique index, so concurrent creates produce duplicates. | Fire 10 parallel POSTs: several 201s. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 10 | `EmployeeIn` | No validation: bad `emp_code`, email, `HH:MM`, `joined_on`, equal shift times, lengths. `created_at` is naive local and returned as a datetime, not epoch ms. | `shift_start="25:99"` is accepted and crashes later. | Pydantic patterns and a validator; `created_at` returned as epoch ms. |
| 11 | `list_employees` | `skip = page * page_size` skips the first page. `total` ignores the filter. No sort. No bounds on `page` / `page_size`. | Page 1 is empty/offset; `?department=X` total is the whole collection; `page=0` gives a negative skip (500). | `(page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, `Query` bounds (R10). |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises (500). | Punch in for `EMP9999`. | 404. |
| 13 | `PunchInIn` | `status` is any string (ABSENT/LEAVE/garbage accepted). `punched_at` is an unbounded int (seconds, floats, huge values) and `if body.punched_at` treats 0 as "missing". | `status="ABSENT"` returns 201; `punched_at=1e18` raises OverflowError. | `Literal` status, strict bounded `EpochMs` (floats, strings, bools, seconds rejected), `is not None` checks. |
| 14 | `punch_in` | Check-then-insert race: two simultaneous requests can both pass `find_one`. | Parallel identical punch-ins give two 201s. | Unique `(emp_code, date)` index; insert and catch `DuplicateKeyError`, so exactly one 201. |
| 15 | `punch_in` response | Returns `id` (the `_id`) and raw datetimes (ISO strings, not epoch ms). | Response contains `"id": "66f..."`. | `record_out()` never emits `_id`, converts instants to ms. |
| 16 | `list_attendance` | Loads the **whole collection** into memory, sorts and paginates in Python. Sort has no `emp_code` tiebreak. No indexes. | 100k docs: slow and memory-heavy; ties come back in arbitrary order. | Indexed query with DB-side sort/skip/limit and hinted index. |
| 17 | `list_attendance` | No validation of `status`, dates, `date_from > date_to`, page bounds. Legacy documents missing `half_day` / `history` / `late_minutes` come back incomplete. | `?status=foo` returns 200 empty; legacy docs lack fields. | Enum and date validation (422); `record_out()` fills defaults. |
| 18 | no indexes at all | Every query is a collection scan; uniqueness unenforced. | `explain` shows `COLLSCAN`. | `ensure_indexes()` at startup (see DECISIONS.md). |

**Looked at and judged fine:**
- The `def` (sync) endpoints with a shared `MongoClient`: FastAPI runs them in a thread pool and the client is thread-safe, so concurrency is fine.
- Reading `MONGO_URI` / `MONGO_DB` from the environment with `load_dotenv()`: `load_dotenv` does not override real environment variables, which is the required priority. (I added a server selection timeout so startup never hangs.)
- Ignoring client-sent derived fields: Pydantic drops unknown fields by default, as the contract requires.

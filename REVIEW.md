# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` (line 93) | `skip = page * page_size` skips the first page entirely. For page 1 with page_size 20, it skips 20 records. | Requesting `page=1` returns records 21-40; records 1-20 are unreachable. | Changed offset calculation to `skip = (page - 1) * page_size`. |
| 2 | `list_employees` (line 94) | `total = db.employees.count_documents({})` ignores the active department filter. | Calling `GET /employees?department=Engineering` filters items but `total` reports the entire company headcount. | Passed the query filter `q` into `count_documents(q)`. |
| 3 | `health` (lines 72-74) | Returns hardcoded `{"status": "ok"}` without testing MongoDB connectivity. | If MongoDB is down or misconfigured, it falsely returns 200 OK instead of 503 Service Unavailable. | Added `client.admin.command('ping')` inside a try/except block returning 503 on failure. |
| 4 | `compute_late_minutes` (lines 33-37) | Floors minutes before comparing `> 10`. For `09:40:01` (601s late), `int(601/60)` is 10, failing `10 > 10` and returning 0. | Punching in at `09:40:01` on a 09:30 shift returned 0 instead of 10 minutes late (violates R2). | Checked elapsed seconds directly (`diff_sec > 600`) before integer division. |
| 5 | `compute_work_hours` (line 41) | Uses Python built-in `round()`, which applies banker's rounding (round-half-to-even) instead of round-half-up. | Work times with `.005` round down to `.00` instead of `.01` (violates R4). | Used Python `Decimal` with `ROUND_HALF_UP` to enforce standard payroll rounding. |
| 6 | `compute_overtime` (lines 44-47) | Missing 30-minute qualification check (R3) and assumes shift end is on the start date (fails overnight shifts). | Punching out 5 minutes late gave 5m overtime (should be 0); night shifts gave 20+ hours overtime. | Added `>= 1800` seconds check and shifted `shift_end` to next day when `shift_end <= shift_start`. |
| 7 | `punch_in` (lines 104-105) | Check-then-insert race condition (`find_one` followed by `insert_one`). | Concurrent identical punch-in requests both pass `find_one` and create duplicate logs for the same day. | Enforced database unique compound index on `(emp_code, date)` and caught `DuplicateKeyError` (409). |
| 8 | `punch_in` (line 120) | Injects internal database ID as `doc["id"] = str(res.inserted_id)`. | Response validation fails against OpenAPI contract because AttendanceRecord has no `id` field. | Removed `id` field injection; attendance records are identified solely by `(emp_code, date)`. |
| 9 | `list_attendance` (lines 144-147) | Loads the entire collection into memory with `list()` and sorts with Python `sort()`. | Severe memory consumption and slow latency on a 100k-record database. | Offloaded sorting and pagination directly to MongoDB via `.sort()`, `.skip()`, and `.limit()`. |
| 10 | `list_attendance` (lines 127-128) | Missing validation for inverted date ranges (`date_from > date_to`). | Queries like `date_from=2026-07-10&date_to=2026-07-01` returned 200 with empty items instead of 422. | Added validation raising `HTTPException(422)` when `date_from > date_to`. |

### Items Reviewed and Decided NOT to be Defects:
1. **Storing dates and shift times as plain strings (`YYYY-MM-DD`, `HH:MM`)**:
   - Considered converting them to BSON date objects in MongoDB.
   - Decided this is correct as designed: `DATA_MODEL.md` explicitly specifies calendar dates and times of day as strings to prevent timezone shifts (IST calendar dates are not timestamps).
2. **Omitting `_id` from JSON responses**:
   - Considered exposing MongoDB `_id`.
   - Decided this is correct: the assignment specifies `_id` is purely internal to MongoDB; business identifiers (`emp_code` and `(emp_code, date)`) are authoritative.

# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` (unique): Enforces customer business key uniqueness.
   - `employees.department`: Accelerates employee listing and filtering by department.
   - `attendance_logs(emp_code, date)` (compound unique): Guarantees at most one log per employee per day and eliminates punch-in race conditions.
   - `attendance_logs(date, emp_code)`: Enables indexed sorting and pagination for attendance lists without loading collections into memory.
   - *Rejected:* An index on `history.at` because `history` is embedded, appended sequentially, and never queried standalone.

2. **Punch-in race.**
   Both concurrent requests pass employee validation and invoke `insert_one`. MongoDB's unique compound index on `(emp_code, date)` ensures atomicity at the database level: the first insert succeeds (201 Created), while the second immediately triggers a `DuplicateKeyError`, which my code intercepts and translates into HTTP 409 Conflict.

3. **Ties.**
   The leaderboard applies standard competition ranking (1, 2, 2, 4) so tied employees share the same rank and subsequent ranks skip. Filtering applies after ranking (`rank <= limit`), returning every employee matching the qualifying rank threshold even if the total row count exceeds `limit`.

4. **Headcount.**
   The aggregation starts from `employees` (filtering `joined_on <= month_end`) and runs a `$lookup` to join `attendance_logs`. Using `$unwind` with `preserveNullAndEmptyArrays: True` ensures employees with zero logs in that month are preserved in the stream, keeping department headcount accurate.

5. **100x Data Scaling.**
   I would pre-aggregate monthly and daily statistics into dedicated summary collections using nightly scheduled jobs or Change Streams, avoiding real-time multi-stage aggregations over multi-million row collections.

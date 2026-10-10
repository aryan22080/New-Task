# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, 409 on duplicates); `(department, emp_code)` for filtered listing and department pipelines; `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, punch lookup, monthly stats); `(date desc, emp_code)` for the default list sort and month scans; `(status, date desc, emp_code)` for status filters. Rejected: a separate `(emp_code, punch_in)` index, because walking the unique index backwards by date does the same job without another write cost.

2. **Punch-in race.** Both requests compute the record and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-write gap.

3. **Ties.** `$rank` gives standard competition ranking, and I filter `rank <= limit` after ranking. A tie at the cutoff therefore returns every tied employee, so there can be more than `limit` rows, ordered by minutes then `emp_code`.

4. **Headcount.** The department summary starts from `employees` (joined on or before month end) and uses `$lookup` to attach logs. Employees without logs still produce one row, so they count; late joiners are filtered out first.

5. **At 100x data.** I would pre-aggregate monthly totals per employee, updated on each write, instead of recomputing from raw logs.

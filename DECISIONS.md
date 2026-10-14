# DECISIONS.md

### 1. Indexes
`employees`: `{ emp_code: 1 }` (unique) enforces identity and race-safe creation; `{ department: 1, emp_code: 1 }` covers filtered listing. `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique) guarantees at most one record per employee-day and safe concurrent punches; `{ date: -1, emp_code: 1 }` powers pagination; `{ date: 1, late_minutes: -1 }` accelerates leaderboard queries without collection scans.

### 2. Punch-in race
Both requests validate the employee and calculate the date. At commit time, MongoDB's unique index on `(emp_code, date)` serializes writes: exactly one insert succeeds (`201 Created`), while the second triggers a `DuplicateKeyError`, caught and returned as `409 Conflict`. No check-then-insert race is possible.

### 3. Ties (Part B)
Leaderboard ranking uses MongoDB's aggregation `$setWindowFields` with `$rank` for standard competition ranking (`1, 2, 2, 4`), sorted deterministically by `emp_code` for ties. Filtering `rank <= limit` ensures all employees tied at the cutoff boundary are returned rather than arbitrarily truncated.

### 4. Headcount (Part B)
Headcount is queried from `employees` filtered by `joined_on <= period_end`, not from attendance logs. A cross-collection `$lookup` gathers attendance metrics, ensuring employees with zero logs are counted in headcount while post-period joiners are excluded. Department averages are calculated over raw logs.

### 5. One thing you would change for 100x data (Part B/C)
At ~10M records, we would maintain pre-aggregated daily rollups updated via MongoDB Change Streams or nightly `$merge` jobs to avoid expensive on-demand multi-stage aggregation, and shard `attendance_logs` on `{ emp_code: "hashed", date: 1 }` to distribute high-concurrency writes.

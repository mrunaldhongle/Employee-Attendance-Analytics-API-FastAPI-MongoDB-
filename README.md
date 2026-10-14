# Employee Attendance & Analytics API (FastAPI + MongoDB)

HROne Software Engineer Training assignment implementation covering **Part A (Review & Starter Fixes)**, **Part B (CRUD & Analytics Endpoints)**, and **Part C (Explainability & Architecture Decisions)**.

## How to Run Locally

### 1. Prerequisites
- Python 3.11+
- MongoDB 6.0+ (running locally on port 27017 or reachable via URI)

### 2. Setup & Environment
```bash
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env    # Configure MONGO_URI and MONGO_DB if not using defaults
python sample_seed.py   # Seeds base dataset
```

### 3. Start the Application
```bash
uvicorn app.main:app --port 8000
```
Interactive API docs are available at: `http://127.0.0.1:8000/docs`

---

## Implemented Endpoints & Features

### Part A: Starter Review & Core Endpoints
- **`GET /health`**: Readiness probe pinging MongoDB (returns 200 or 503).
- **`POST /employees`**: Creates employee with Pydantic validation, database unique index on `emp_code` (409 on duplicate), server-set epoch ms `created_at`.
- **`GET /employees`**: Paginated `(page - 1) * page_size`, department filter updating `total`, sorted by `emp_code` ASC.
- **`POST /attendance/punch-in`**: Creates attendance record according to Rules R1 & R2 (overnight shift date attribution, late minutes calculation), compound unique index on `(emp_code, date)` for concurrency safety (409 on duplicate), 404 on unknown employee, 422 on invalid timestamps.
- **`GET /attendance`**: Database-level filtering, pagination, compound sorting `[("date", DESC), ("emp_code", ASC)]`, date range validation, and history instant formatting in epoch ms.
- **`REVIEW.md`**: Complete table detailing all 23 starter code defects with reproduction symptoms and fixes.

### Part B: Advanced Attendance & Analytics Endpoints
- **`POST /attendance/punch-out`**: Atomically closes attendance session, computes work hours with `ROUND_HALF_UP` (R4), overtime (R3 $\ge 30$ mins), and half-day status ($< 4.5$h). Supports overnight shifts.
- **`PATCH /attendance/{emp_code}/{date}`**: Regularizes attendance, recomputing derived fields and appending audit history entries. Race-safe via CAS check. Status transition to `ABSENT`/`LEAVE` clears punch times.
- **`GET /analytics/employees/{emp_code}/monthly`**: Pure MongoDB aggregation computing weekday working days, mid-month joiner proration from `joined_on`, half-day weighting (0.5), and attendance percentage.
- **`GET /analytics/departments/summary`**: Cross-collection `$lookup` aggregation computing headcount strictly from `employees` (joined on or before period end, including employees with no logs), and true average work hours over qualifying records.
- **`GET /analytics/leaderboard/late`**: MongoDB aggregation implementing standard competition ranking (`1, 2, 2, 4`) via `$setWindowFields` with `$rank`, deterministic `emp_code` sorting, and limit applied after ranking to retain cutoff ties.
- **`GET /analytics/departments/{department}/trend`**: Full date range densification via `$densify` ensuring exactly one row per calendar day (including missing weekdays and weekends), combined with a 7-day rolling moving average via `$setWindowFields`.

### Part C: Explainability & Technical Decisions
- **`GET /admin/explain/{endpoint}`**: Runs MongoDB `explain("executionStats")` on genuine query and aggregation pipelines for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. Confirms index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).
- **`DECISIONS.md`**: Detailed answers to all five architectural questions covering indexing strategy, race safety, leaderboard ties, headcount aggregation, and 100x scalability architecture.

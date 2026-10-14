"""Employee Attendance & Analytics API - Part A implementation.

Run: uvicorn app.main:app --port 8000
Required environment: MONGO_URI, MONGO_DB (a local .env is loaded for convenience).
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv(override=False)

mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")

# Explicit environment configuration; no silent defaults.
client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1500) if mongo_uri else None
db = client[mongo_db] if client is not None and mongo_db else None
current_uri = mongo_uri
current_dbname = mongo_db


def get_db():
    global client, db, current_uri, current_dbname
    uri = os.getenv("MONGO_URI")
    dbname = os.getenv("MONGO_DB")
    if not uri or not dbname:
        return None
    if client is None or db is None or current_uri != uri or current_dbname != dbname:
        client = MongoClient(uri, serverSelectionTimeoutMS=1500)
        db = client[dbname]
        current_uri = uri
        current_dbname = dbname
    return db


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
ExplainEndpointName = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



def _parse_hhmm(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must be in HH:MM format")
    h, m = value.split(":")
    return int(h), int(m)


def _truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        # MongoDB datetimes are stored as UTC; interpret naive values read from BSON as UTC.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def _as_utc_naive(value: datetime) -> datetime:
    """Convert an instant to the naive-UTC datetime representation used by BSON."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return _truncate_seconds(value.astimezone(UTC)).replace(tzinfo=None)


def _epoch_ms_to_ist(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be an epoch-millisecond integer between 100000000000 and 4102444800000",
        )
    try:
        sec = value // 1000
        return datetime.fromtimestamp(sec, tz=UTC).astimezone(IST)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="punched_at is outside the supported timestamp range")


def _now_ist() -> datetime:
    return datetime.now(UTC).replace(microsecond=0).astimezone(IST)


def _to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _format_history(history: list) -> list:
    res = []
    for h in history:
        item = {
            "at": _to_epoch_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at"),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            if isinstance(v, dict):
                c = {}
                for field in ("from", "to"):
                    val = v.get(field)
                    if isinstance(val, datetime):
                        c[field] = _to_epoch_ms(val)
                    else:
                        c[field] = val
                item["changes"][k] = c
            else:
                item["changes"][k] = v
        res.append(item)
    return res


def _api_attendance(doc: dict) -> dict:
    """Return only contract fields, with instants represented as epoch milliseconds."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch_ms(doc.get("punch_in")),
        "punch_out": _to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": _format_history(doc.get("history", [])),
    }


def _shift_start_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    clean_punch = _truncate_seconds(punch_in)
    local = _as_ist(clean_punch)
    sh, sm = _parse_hhmm(shift_start)
    eh, em = _parse_hhmm(shift_end)
    shift_start_time = time(sh, sm)
    shift_end_time = time(eh, em)
    attendance_day = local.date()
    if shift_end_time <= shift_start_time and local.time().replace(tzinfo=None) < shift_end_time:
        attendance_day -= timedelta(days=1)
    return datetime.combine(attendance_day, shift_start_time, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    """Return whole minutes late, applying the strict 10-minute grace period (R1/R2)."""
    clean_punch = _truncate_seconds(punch_in)
    start = _shift_start_for_punch(clean_punch, shift_start, shift_end)
    local_punch = _as_ist(clean_punch)
    elapsed_seconds = (local_punch - start).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return max(0, int(elapsed_seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate hours using decimal half-up rounding (R4)."""
    p_in = _truncate_seconds(punch_in)
    p_out = _truncate_seconds(punch_out)
    seconds = Decimal(str(int((p_out - p_in).total_seconds())))
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None
) -> int:
    """Return overtime whole minutes, only when at least 30 minutes (R1/R3)."""
    h, m = _parse_hhmm(shift_end)
    attendance_day = date.fromisoformat(date_str)
    if shift_start is not None:
        sh, sm = _parse_hhmm(shift_start)
        if time(h, m) <= time(sh, sm):
            attendance_day += timedelta(days=1)
    end = datetime.combine(attendance_day, time(h, m), tzinfo=IST)
    clean_punch = _truncate_seconds(punch_out)
    elapsed = (_as_ist(clean_punch) - end).total_seconds()
    whole_minutes = int(elapsed // 60)
    return whole_minutes if whole_minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        _parse_hhmm(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be in YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )
    punch_out: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_required_indexes() -> None:
    database = get_db()
    if database is None:
        raise RuntimeError("Set MONGO_URI and MONGO_DB in the environment or .env file")

    def _safe_index(coll, keys, **kwargs):
        try:
            coll.create_index(keys, **kwargs)
        except OperationFailure as exc:
            if exc.code != 85:  # 85 = IndexOptionsConflict (already exists with different options/name)
                raise

    # Unique indexes provide race-safe concurrency guarantees.
    _safe_index(database.employees, [("emp_code", ASCENDING)], unique=True)
    _safe_index(database.employees, [("department", ASCENDING), ("emp_code", ASCENDING)])
    _safe_index(database.employees, [("joined_on", ASCENDING)])
    _safe_index(database.employees, [("department", ASCENDING), ("joined_on", ASCENDING)])
    _safe_index(database.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    _safe_index(database.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)])
    _safe_index(database.attendance_logs, [("emp_code", ASCENDING), ("date", DESCENDING)])
    _safe_index(database.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    _safe_index(database.attendance_logs, [("date", ASCENDING), ("late_minutes", DESCENDING)])
    _safe_index(database.attendance_logs, [("date", ASCENDING), ("status", ASCENDING)])


# --------------------------------------------------------------------------- #
# Existing endpoints corrected for Part A
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="MONGO_URI and MONGO_DB must be configured")
    try:
        database.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    doc = body.model_dump()
    created_instant = datetime.now(UTC).replace(microsecond=0)
    doc["created_at"] = created_instant.replace(tzinfo=None)
    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    doc.pop("_id", None)
    doc["created_at"] = _to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    q = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    try:
        total = database.employees.count_documents(q)
        docs = database.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = list(docs)
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = _to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    try:
        emp = database.employees.find_one({"emp_code": body.emp_code})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = _epoch_ms_to_ist(body.punched_at) if body.punched_at is not None else _now_ist()
    attendance_day = ts.date()
    # A punch-in before shift_end on an overnight shift belongs to the previous shift-start date.
    start_h, start_m = _parse_hhmm(emp["shift_start"])
    end_h, end_m = _parse_hhmm(emp["shift_end"])
    if (time(end_h, end_m) <= time(start_h, start_m)
            and ts.time().replace(tzinfo=None) < time(end_h, end_m)):
        attendance_day -= timedelta(days=1)
    d = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": _as_utc_naive(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return _api_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        emp = database.employees.find_one({"emp_code": body.emp_code})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    out_ist = _epoch_ms_to_ist(body.punched_at) if body.punched_at is not None else _now_ist()
    out_utc = _as_utc_naive(out_ist)

    # Find the employee's most recent record whose punch_in is at or before punched_at
    try:
        candidate = database.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": out_utc},
            },
            sort=[("punch_in", DESCENDING)],
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if candidate is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    in_utc = candidate["punch_in"]
    in_ist = _as_ist(in_utc)
    diff_seconds = (out_ist - in_ist).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(in_ist, out_ist)
    overtime_minutes = compute_overtime(out_ist, emp["shift_end"], candidate["date"], emp["shift_start"])
    half_day = bool(work_hours < 4.50)

    try:
        updated = database.attendance_logs.find_one_and_update(
            {
                "emp_code": candidate["emp_code"],
                "date": candidate["date"],
                "punch_out": None,
            },
            {
                "$set": {
                    "punch_out": out_utc,
                    "work_hours": work_hours,
                    "overtime_minutes": overtime_minutes,
                    "half_day": half_day,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return _api_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Validate date parameter format YYYY-MM-DD and real calendar date
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    try:
        emp = database.employees.find_one({"emp_code": emp_code})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    try:
        old_doc = database.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    if old_doc is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    new_status = body.status if body.status is not None else old_doc.get("status")

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="supplying punch_in or punch_out when setting status ABSENT or LEAVE is not allowed",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            new_punch_in = _as_utc_naive(_epoch_ms_to_ist(body.punch_in))
        else:
            new_punch_in = old_doc.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires a punch_in")

        if isinstance(new_punch_in, (int, float)):
            new_punch_in = _as_utc_naive(_epoch_ms_to_ist(int(new_punch_in)))
        elif isinstance(new_punch_in, str):
            new_punch_in = _as_utc_naive(datetime.fromisoformat(new_punch_in))

        # Verify punch_in remains on the record's attendance date per R1
        shift_start_dt = _shift_start_for_punch(new_punch_in, emp["shift_start"], emp["shift_end"])
        if shift_start_dt.date().isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in does not match the record's attendance date")

        if body.punch_out is not None:
            new_punch_out = _as_utc_naive(_epoch_ms_to_ist(body.punch_out))
        else:
            new_punch_out = old_doc.get("punch_out")

        if new_punch_out is not None:
            if isinstance(new_punch_out, (int, float)):
                new_punch_out = _as_utc_naive(_epoch_ms_to_ist(int(new_punch_out)))
            elif isinstance(new_punch_out, str):
                new_punch_out = _as_utc_naive(datetime.fromisoformat(new_punch_out))

            diff_seconds = (new_punch_out - new_punch_in).total_seconds()
            if diff_seconds <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_seconds > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"])
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"])
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Collect actual changes for the audit trail
    changes: dict = {}
    if old_doc.get("status") != new_status:
        changes["status"] = {"from": old_doc.get("status"), "to": new_status}

    old_in = old_doc.get("punch_in")
    if old_in != new_punch_in:
        changes["punch_in"] = {"from": old_in, "to": new_punch_in}

    old_out = old_doc.get("punch_out")
    if old_out != new_punch_out:
        changes["punch_out"] = {"from": old_out, "to": new_punch_out}

    old_wh = old_doc.get("work_hours")
    if old_wh != new_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_lm = int(old_doc.get("late_minutes", 0))
    if old_lm != new_late_minutes:
        changes["late_minutes"] = {"from": old_lm, "to": new_late_minutes}

    old_ot = int(old_doc.get("overtime_minutes", 0))
    if old_ot != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = bool(old_doc.get("half_day", False))
    if old_hd != new_half_day:
        changes["half_day"] = {"from": old_hd, "to": new_half_day}

    if len(changes) == 0:
        raise HTTPException(status_code=422, detail="request makes no changes")

    now_utc = _as_utc_naive(_now_ist())
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    old_history = old_doc.get("history")
    if not isinstance(old_history, list):
        old_history = []

    if len(old_history) == 0:
        cas_filter = {
            "emp_code": emp_code,
            "date": date,
            "$or": [
                {"history": {"$size": 0}},
                {"history": {"$exists": False}},
                {"history": None},
            ],
        }
    else:
        cas_filter = {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": len(old_history)},
        }

    new_history = list(old_history) + [history_entry]

    try:
        updated = database.attendance_logs.find_one_and_update(
            cas_filter,
            {
                "$set": {
                    "status": new_status,
                    "punch_in": new_punch_in,
                    "punch_out": new_punch_out,
                    "work_hours": new_work_hours,
                    "late_minutes": new_late_minutes,
                    "overtime_minutes": new_overtime_minutes,
                    "half_day": new_half_day,
                    "history": new_history,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if updated is None:
        raise HTTPException(status_code=409, detail="concurrent edit conflict")

    return _api_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    # Validate calendar dates instead of accepting arbitrary strings as date bounds.
    for field_name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                date.fromisoformat(value)
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    skip = (page - 1) * page_size
    try:
        total = database.attendance_logs.count_documents(q)
        docs = database.attendance_logs.find(q, {"_id": 0}).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size)
        items = [_api_attendance(doc) for doc in docs]
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def compute_working_days(joined_on_str: str, month_start: date, month_end: date) -> int:
    """Count Monday-Friday days between start_date and month_end (inclusive) per R7."""
    try:
        joined_on = date.fromisoformat(joined_on_str)
    except ValueError:
        joined_on = month_start

    start_date = max(month_start, joined_on)
    if start_date > month_end:
        return 0

    curr = start_date
    working_days = 0
    while curr <= month_end:
        if curr.weekday() < 5:  # Mon=0, Fri=4, Sat=5, Sun=6
            working_days += 1
        curr += timedelta(days=1)
    return working_days


@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        emp = database.employees.find_one({"emp_code": emp_code})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    year = int(month[:4])
    m = int(month[5:7])
    month_start = date(year, m, 1)
    next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
    month_end = next_month_start - timedelta(days=1)

    working_days = compute_working_days(emp["joined_on"], month_start, month_end)

    start_str = month_start.isoformat()
    next_str = next_month_start.isoformat()

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lt": next_str},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": ["$day_of_week", 1]},
                                    {"$lt": ["$day_of_week", 7]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    try:
        agg_result = list(database.attendance_logs.aggregate(pipeline))
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if agg_result:
        row = agg_result[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", status_code=200)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    year = int(month[:4])
    m = int(month[5:7])
    month_start = date(year, m, 1)
    next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
    month_end = next_month_start - timedelta(days=1)

    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    match_stage: dict = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emps"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$gt": ["$day_of_week", 1]},
                                                {"$lt": ["$day_of_week", 7]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "wh_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "wh_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    try:
        results = list(database.employees.aggregate(pipeline))
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    items = []
    for doc in results:
        headcount = int(doc["headcount"])
        stats_list = doc.get("stats", [])
        if stats_list:
            s = stats_list[0]
            present_days = float(s.get("present_days", 0.0))
            wh_sum = float(s.get("wh_sum", 0.0))
            wh_count = int(s.get("wh_count", 0))
            late_count = int(s.get("late_count", 0))
            total_late_minutes = int(s.get("total_late_minutes", 0))
            leave_count = int(s.get("leave_count", 0))
            on_duty_count = int(s.get("on_duty_count", 0))
        else:
            present_days = 0.0
            wh_sum = 0.0
            wh_count = 0
            late_count = 0
            total_late_minutes = 0
            leave_count = 0
            on_duty_count = 0

        if wh_count > 0:
            raw_avg = Decimal(str(wh_sum)) / Decimal(str(wh_count))
            avg_work_hours = float(raw_avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", status_code=200)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    year = int(month[:4])
    m = int(month[5:7])
    month_start = date(year, m, 1)
    next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
    month_end = next_month_start - timedelta(days=1)

    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {
            "$unwind": "$emp_info",
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    try:
        items = list(database.attendance_logs.aggregate(pipeline))
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    return {
        "month": month,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend", status_code=200)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    for field_name, val in (("from", from_date), ("to", to_date)):
        try:
            if not isinstance(val, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                raise ValueError
            date.fromisoformat(val)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid {field_name} date format, expected YYYY-MM-DD")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    try:
        dept_emps = list(
            database.employees.find(
                {"department": department},
                {"emp_code": 1, "joined_on": 1, "_id": 0},
            )
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_emps]
    from_str = d_from.isoformat()
    to_str = d_to.isoformat()
    dt_from = datetime.combine(d_from, datetime.min.time())
    dt_to_plus_1 = datetime.combine(d_to + timedelta(days=1), datetime.min.time())

    pipeline: list = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_plus_1],
                },
            }
        },
        {
            "$project": {
                "dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$dt"},
            }
        },
        {
            "$project": {
                "dt": 1,
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$and": [
                        {"$gt": ["$day_of_week", 1]},
                        {"$lt": ["$day_of_week", 7]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_emps,
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "dt": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$sort": {"date": 1},
        },
    ]

    try:
        raw_items = list(database.attendance_logs.aggregate(pipeline))
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    items = []
    for r in raw_items:
        ar = r.get("attendance_rate")
        if ar is not None:
            ar_rounded = round(float(Decimal(str(ar)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)), 4)
        else:
            ar_rounded = None

        ma = r.get("moving_avg_7d")
        if ma is not None:
            ma_rounded = round(float(Decimal(str(ma)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)), 4)
        else:
            ma_rounded = None

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": ar_rounded,
            "moving_avg_7d": ma_rounded,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", status_code=200)
def explain_endpoint(
    endpoint: ExplainEndpointName,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(default=10),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1),
    page_size: int = Query(default=20),
):
    database = get_db()
    if database is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    if endpoint == "attendance_list":
        if page < 1:
            raise HTTPException(status_code=422, detail="page must be >= 1")
        if page_size < 1 or page_size > 100:
            raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")
        for field_name, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None:
                try:
                    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError
                    date.fromisoformat(value)
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

        q: dict = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status

        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month or not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required in YYYY-MM format")

        year = int(month[:4])
        m = int(month[5:7])
        month_start = date(year, m, 1)
        next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
        start_str = month_start.isoformat()
        next_str = next_month_start.isoformat()

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_str, "$lt": next_str},
                }
            },
            {
                "$project": {
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gt": ["$day_of_week", 1]},
                                        {"$lt": ["$day_of_week", 7]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month or not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required in YYYY-MM format")

        year = int(month[:4])
        m = int(month[5:7])
        month_start = date(year, m, 1)
        next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
        month_end = next_month_start - timedelta(days=1)

        month_start_str = month_start.isoformat()
        month_end_str = month_end.isoformat()

        match_stage: dict = {"joined_on": {"$lte": month_end_str}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emps": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$emps"]},
                                        {"$gte": ["$date", month_start_str]},
                                        {"$lte": ["$date", month_end_str]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "status": 1,
                                "work_hours": 1,
                                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                                "half_day": {"$ifNull": ["$half_day", False]},
                                "day_of_week": {
                                    "$dayOfWeek": {
                                        "$dateFromString": {"dateString": "$date"}
                                    }
                                },
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$gt": ["$day_of_week", 1]},
                                                    {"$lt": ["$day_of_week", 7]},
                                                ]
                                            },
                                            {"$cond": ["$half_day", 0.5, 1.0]},
                                            0.0,
                                        ]
                                    }
                                },
                                "wh_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            "$work_hours",
                                            0.0,
                                        ]
                                    }
                                },
                                "wh_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                                },
                                "total_late_minutes": {"$sum": "$late_minutes"},
                                "leave_count": {
                                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                                },
                                "on_duty_count": {
                                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                                },
                            }
                        },
                    ],
                    "as": "stats",
                }
            },
            {"$sort": {"_id": ASCENDING}},
        ]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month or not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required in YYYY-MM format")
        if limit is None or limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")

        year = int(month[:4])
        m = int(month[5:7])
        month_start = date(year, m, 1)
        next_month_start = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
        month_end = next_month_start - timedelta(days=1)

        month_start_str = month_start.isoformat()
        month_end_str = month_end.isoformat()

        pipeline: list = [
            {
                "$match": {
                    "date": {"$gte": month_start_str, "$lte": month_end_str},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {
                "$unwind": "$emp_info",
            },
        ]

        if department is not None:
            pipeline.append({"$match": {"emp_info.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp_info.name",
                    "department": "$emp_info.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])

        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' date parameters are required for department_trend")

        for field_name, val in (("from", from_date), ("to", to_date)):
            try:
                if not isinstance(val, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                    raise ValueError
                date.fromisoformat(val)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid {field_name} date format, expected YYYY-MM-DD")

        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        try:
            dept_emps = list(
                database.employees.find(
                    {"department": department},
                    {"emp_code": 1, "joined_on": 1, "_id": 0},
                )
            )
        except PyMongoError:
            raise HTTPException(status_code=503, detail="Database operation failed")

        emp_codes = [e["emp_code"] for e in dept_emps]
        from_str = d_from.isoformat()
        to_str = d_to.isoformat()
        dt_from = datetime.combine(d_from, datetime.min.time())
        dt_to_plus_1 = datetime.combine(d_to + timedelta(days=1), datetime.min.time())

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_str, "$lte": to_str},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "dt": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [dt_from, dt_to_plus_1],
                    },
                }
            },
            {
                "$project": {
                    "dt": 1,
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "day_of_week": {"$dayOfWeek": "$dt"},
                }
            },
            {
                "$project": {
                    "dt": 1,
                    "date": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "is_working_day": {
                        "$and": [
                            {"$gt": ["$day_of_week", 1]},
                            {"$lt": ["$day_of_week", 7]},
                        ]
                    },
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": dept_emps,
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    },
                }
            },
            {
                "$project": {
                    "dt": 1,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"dt": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"],
                            },
                        }
                    },
                }
            },
            {
                "$sort": {"date": 1},
            },
        ]

        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"

    try:
        raw_explain = database.command(cmd)
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": jsonable_encoder(raw_explain),
    }





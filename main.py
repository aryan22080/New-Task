"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Everything lives in this one file:
  1. configuration and the Mongo client
  2. time / rule helpers (R1-R5, R8), all in IST and whole seconds
  3. request validation models
  4. serialisation (BSON datetime <-> epoch milliseconds)
  5. endpoints: health, employees, attendance, analytics, explain
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones, so we
# never have to guess which zone a datetime is in.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE = ["ABSENT", "LEAVE"]
MAX_PAGE = 10_000_000

# Index names are fixed so queries can hint them (and so create_index is idempotent).
IX_EMP_CODE = "emp_code_unique"
IX_EMP_DEPT = "dept_emp_code"
IX_EMP_JOINED = "joined_on_1"
IX_LOG_UNIQUE = "emp_code_date_unique"
IX_LOG_DATE = "date_desc_emp_code"
IX_LOG_STATUS = "status_date_desc_emp_code"

_indexes_ready = False


def ensure_indexes() -> bool:
    """Create every index the endpoints rely on. Safe to call repeatedly."""
    global _indexes_ready
    specs = [
        (employees, [("emp_code", ASCENDING)], IX_EMP_CODE, True),
        (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], IX_EMP_DEPT, False),
        (employees, [("joined_on", ASCENDING)], IX_EMP_JOINED, False),
        (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], IX_LOG_UNIQUE, True),
        (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], IX_LOG_DATE, False),
        (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], IX_LOG_STATUS, False),
    ]
    ok = True
    for coll, keys, name, unique in specs:
        try:
            coll.create_index(keys, name=name, unique=unique)
        except PyMongoError as exc:  # keep the app up; /health will say whether Mongo is reachable
            log.error("index %s failed: %s", name, exc)
            ok = False
    _indexes_ready = ok
    return ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding and the business rules
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return as_utc(dt).replace(microsecond=0)


def now_utc() -> datetime:
    return trunc_seconds(datetime.now(UTC))


def ms_to_dt(ms: int) -> datetime:
    return trunc_seconds(EPOCH + timedelta(milliseconds=ms))


def dt_to_ms(dt: datetime) -> int:
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value: Decimal | int | float | str, places: int) -> Decimal:
    """R8 / R4: half-up rounding done on Decimals, never on binary floats."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight shift
    belong to the shift that began the previous day."""
    local = trunc_seconds(punch_in).astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_dt(day: date, shift_start: str) -> datetime:
    return datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(day: date, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor(minutes since start)."""
    delta = int((trunc_seconds(punch_in) - shift_start_dt(day, shift_start)).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_overtime(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes) after shift end, counted only when it is at least 30."""
    delta = int((trunc_seconds(punch_out) - shift_end_dt(day, shift_start, shift_end)).total_seconds())
    minutes = delta // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return float(round_half_up(Decimal(secs) / Decimal(3600), 2))


def compute_half_day(work_hours: float) -> bool:
    """R5: judged on the already rounded work_hours."""
    return Decimal(str(work_hours)) < Decimal("4.50")


def derive(emp: dict, day: date, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record in one place (used by punch-in/out and PATCH)."""
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# 3. Validation helpers and request models
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def invalid(where: str, field: str, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body shape."""
    return RequestValidationError([{"type": "value_error", "loc": (where, field), "msg": msg, "input": None}])


def parse_date(value: str, field: str, where: str = "query") -> date:
    if not DATE_RE.match(value):
        raise invalid(where, field, "must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid(where, field, "not a real calendar date")


def parse_month(value: str) -> tuple[str, str]:
    """-> (first day, last day) of the month as YYYY-MM-DD strings."""
    if not MONTH_RE.match(value):
        raise invalid("query", "month", "must be YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    if year < 1:
        raise invalid("query", "month", "year out of range")
    last = calendar.monthrange(year, month)[1]
    return f"{value}-01", f"{value}-{last:02d}"


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=HHMM)
    shift_end: StrictStr = Field(default="18:30", pattern=HHMM)
    joined_on: StrictStr

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        if not DATE_RE.match(self.joined_on):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a real calendar date")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


PUNCH_FIELDS = ("punch_in", "punch_out")


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in PUNCH_FIELDS:  # dates nested inside history are datetimes too
            changes[field] = {"from": ms_or_none(change.get("from")), "to": ms_or_none(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": ms_or_none(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Never exposes _id; fills the gaps legacy documents may have."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5a. Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:  # Mongo came up after the app did
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # unique index on emp_code is the real guard
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in <= ts (newest date first; walks the unique index backwards)
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("date", DESCENDING)],
        hint=IX_LOG_UNIQUE,
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = trunc_seconds(rec["punch_in"])
    if ts <= p_in or ts > p_in + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    fields = derive(emp, date.fromisoformat(rec["date"]), rec["status"], p_in, ts)
    fields["punch_out"] = ts
    # Compare-and-set: only succeeds while punch_out is still null (None also matches a missing field).
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_list_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
    status: Optional[str], page: int, page_size: int,
) -> dict:
    """Single place that builds the list query; the explain endpoint reuses it."""
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    if emp_code:
        hint = IX_LOG_UNIQUE
    elif status:
        hint = IX_LOG_STATUS
    else:
        hint = IX_LOG_DATE
    return {
        "filter": q,
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


def check_list_filters(date_from: Optional[str], date_to: Optional[str]) -> None:
    d_from = parse_date(date_from, "date_from") if date_from else None
    d_to = parse_date(date_to, "date_to") if date_to else None
    if d_from and d_to and d_from > d_to:
        raise invalid("query", "date_from", "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    check_list_filters(date_from, date_to)
    p = attendance_list_query(emp_code, date_from, date_to, status.value if status else None, page, page_size)
    if p["filter"]:
        total = logs.count_documents(p["filter"], hint=p["hint"])
    else:
        total = logs.estimated_document_count()
    cursor = (
        logs.find(p["filter"]).sort(p["sort"]).skip(p["skip"]).limit(p["limit"]).hint(p["hint"])
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):  # noqa: A002 - name fixed by the contract
    parse_date(date, "date", where="path")
    emp = get_employee_or_404(emp_code)

    for _attempt in range(5):  # optimistic concurrency: retry if another writer got in between
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no record for that date")

        old_status = rec["status"]
        old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
        new_status = body.status.value if body.status else old_status
        new_in, new_out = old_in, old_out

        if new_status in NON_PRESENCE:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, f"{new_status} clears punch times; do not supply punch_in/punch_out")
            new_in = new_out = None
        else:
            if body.punch_in is not None:
                new_in = ms_to_dt(body.punch_in)
            if body.punch_out is not None:
                new_out = ms_to_dt(body.punch_out)
            if new_in is None:
                raise HTTPException(422, "a presence status requires a punch_in")
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
                raise HTTPException(422, "punch_in must fall on the record's attendance date")
            if new_out is not None:
                p_in = trunc_seconds(new_in)
                if trunc_seconds(new_out) <= p_in or trunc_seconds(new_out) > p_in + timedelta(hours=24):
                    raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        if (new_status, new_in, new_out) == (old_status, old_in, old_out):
            raise HTTPException(422, "request changes nothing")

        derived = derive(emp, date_from_str(date), new_status, new_in, new_out)
        new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
        old_values = {
            "status": old_status, "punch_in": old_in, "punch_out": old_out,
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day", False)),
        }
        changes = {
            f: {"from": old_values[f], "to": new_values[f]} for f in new_values if new_values[f] != old_values[f]
        }
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Guard = the state we read. If anyone appended history or punched in/out meanwhile, no match -> retry.
        guard: dict[str, Any] = {
            "emp_code": emp_code, "date": date, "status": old_status, "punch_in": old_in, "punch_out": old_out,
        }
        guard["history"] = {"$size": len(rec["history"])} if rec.get("history") is not None else {"$exists": False}
        updated = logs.find_one_and_update(
            guard, {"$set": new_values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record is being modified concurrently; retry")


def date_from_str(value: str) -> date:
    return date.fromisoformat(value)


# --------------------------------------------------------------------------- #
# 5c. Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
# Expressions over one attendance document. Stored derived values are read, never recomputed.
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}  # Mon-Fri
HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # a day is 2 halves
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}


def halves_expr(weekdays_only: bool) -> dict:
    cond = {"$and": [IS_PRESENT, IS_WEEKDAY]} if weekdays_only else IS_PRESENT
    return {"$cond": [cond, HALVES, 0]}


def count_weekdays(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] (R7). Arithmetic on two dates, not a scan of data."""
    n, d = 0, first
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> tuple[list, str]:
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "halves": {"$sum": halves_expr(True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return pipeline, IX_LOG_UNIQUE


def summary_pipeline(month_start: str, month_end: str, department: Optional[str]) -> tuple[list, str]:
    """employees (joined by month end) -> per-employee month totals via $lookup -> $group by department."""
    match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    hint = IX_EMP_JOINED
    if department:
        match["department"] = department
        hint = IX_EMP_DEPT
    has_work_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
                {"$group": {
                    "_id": None,
                    "halves": {"$sum": halves_expr(True)},
                    # work hours in whole hundredths so the average can be rounded exactly later
                    "work_cents": {"$sum": {"$cond": [has_work_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "work_n": {"$sum": {"$cond": [has_work_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                    "late_minutes": {"$sum": LATE_MIN},
                    "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "m",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},  # one row per employee, with or without logs (R9)
            **{k: {"$sum": {"$ifNull": [{"$arrayElemAt": [f"$m.{k}", 0]}, 0]}}
               for k in ("halves", "work_cents", "work_n", "late_count", "late_minutes", "leave", "on_duty")},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, hint


def leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str]) -> tuple[list, str]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline, IX_LOG_DATE


def trend_pipeline(department: str, d_from: date, d_to: date) -> tuple[list, str]:
    n_days = (d_to - d_from).days + 1
    start_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    f, t = d_from.isoformat(), d_to.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {  # per employee: their records in range, collapsed to one row per date
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$group": {
                    "_id": "$date",
                    "halves": {"$sum": halves_expr(False)},  # weekends count as present too
                    "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                }},
            ],
            "as": "d",
        }},
        {"$facet": {  # headcount per join date, and the department's totals per date
            "hc": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            "dl": [
                {"$unwind": "$d"},
                {"$group": {"_id": "$d._id", "halves": {"$sum": "$d.halves"}, "late": {"$sum": "$d.late"}}},
            ],
        }},
        # gap filling: one generated row per calendar day, whether or not anything was recorded
        {"$project": {"hc": 1, "dl": 1, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"dt": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}},
                "in": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}, "dow": {"$dayOfWeek": "$$dt"}},
            }},
        }}}},
        {"$unwind": "$days"},
        {"$project": {
            "date": "$days.date",
            "working": {"$and": [{"$gte": ["$days.dow", 2]}, {"$lte": ["$days.dow", 6]}]},
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$hc", "as": "h", "cond": {"$lte": ["$$h._id", "$days.date"]}}},
                "as": "x", "in": "$$x.n",
            }}},
            "row": {"$arrayElemAt": [{"$filter": {"input": "$dl", "as": "r", "cond": {"$eq": ["$$r._id", "$days.date"]}}}, 0]},
        }},
        {"$project": {
            "date": 1, "working": 1, "headcount": 1,
            "halves": {"$ifNull": ["$row.halves", 0]},
            "late_count": {"$ifNull": ["$row.late", 0]},
        }},
        {"$addFields": {  # rate in 1/10000ths, half-up using integer arithmetic only
            "rate_int": {"$cond": [
                {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]}, {"$multiply": [2, "$headcount"]}]}},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "win_sum": {"$sum": "$rate_int", "window": {"documents": [-6, "current"]}},
                "win_n": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": {"documents": [-6, "current"]}},
            },
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$working", "headcount": 1,
            "present_count": {"$divide": ["$halves", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_int"}, {"$divide": ["$rate_int", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$win_n", 0]}, None,
                {"$divide": [{"$floor": {"$divide": [
                    {"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]}, {"$multiply": [2, "$win_n"]}]}}, 10000]},
            ]},
        }},
        {"$sort": {"date": 1}},
    ]
    return pipeline, IX_EMP_DEPT


def run_pipeline(coll, pipeline: list, hint: str) -> list:
    return list(coll.aggregate(pipeline, hint=hint, allowDiskUse=True))


# --------------------------------------------------------------------------- #
# 5d. Analytics endpoints
# --------------------------------------------------------------------------- #
def dec_to_float(d: Decimal) -> float:
    return float(d)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    month_start, month_end = parse_month(month)
    emp = get_employee_or_404(emp_code)

    first = max(date.fromisoformat(month_start), date.fromisoformat(emp["joined_on"]))
    last = date.fromisoformat(month_end)
    working_days = count_weekdays(first, last) if first <= last else 0

    pipeline, hint = monthly_pipeline(emp_code, month_start, month_end)
    rows = run_pipeline(logs, pipeline, hint)
    r = rows[0] if rows else {}
    halves = int(r.get("halves", 0))
    pct = None
    if working_days > 0:
        pct = dec_to_float(round_half_up(Decimal(halves) * 50 / Decimal(working_days), 2))  # halves/2/wd*100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    month_start, month_end = parse_month(month)
    pipeline, hint = summary_pipeline(month_start, month_end, department)
    items = []
    for r in run_pipeline(employees, pipeline, hint):
        avg = None
        if r["work_n"] > 0:  # exact: sum of hundredths / count / 100, rounded half-up
            avg = dec_to_float(round_half_up(Decimal(int(r["work_cents"])) / Decimal(int(r["work_n"])) / 100, 2))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["halves"]) / 2,
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_minutes"]),
            "leave_count": int(r["leave"]),
            "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month_start, month_end = parse_month(month)
    pipeline, hint = leaderboard_pipeline(month_start, month_end, limit, department)
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
        }
        for r in run_pipeline(logs, pipeline, hint)
    ]
    return {"month": month, "items": items}


def trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise invalid("query", "to", "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise invalid("query", "to", "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    pipeline, hint = trend_pipeline(department, d_from, d_to)
    return {"department": department, "items": run_pipeline(employees, pipeline, hint)}


# --------------------------------------------------------------------------- #
# 5e. Explain
# --------------------------------------------------------------------------- #
class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def need(**params) -> None:
    for name, value in params.items():
        if value is None or value == "":
            raise invalid("query", name, "field required for this endpoint")


def to_plain_json(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc))  # ObjectId / datetime / Int64 -> JSON-safe


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint is ExplainTarget.attendance_list:
        check_list_filters(date_from, date_to)
        p = attendance_list_query(emp_code, date_from, date_to, status.value if status else None, page, page_size)
        command = {
            "find": logs.name, "filter": p["filter"], "sort": dict(p["sort"]),
            "skip": p["skip"], "limit": p["limit"], "hint": p["hint"],
        }
        coll = logs
    else:
        if endpoint is ExplainTarget.employee_monthly:
            need(emp_code=emp_code, month=month)
            m_start, m_end = parse_month(month)
            pipeline, hint = monthly_pipeline(emp_code, m_start, m_end)
            coll = logs
        elif endpoint is ExplainTarget.department_summary:
            need(month=month)
            m_start, m_end = parse_month(month)
            pipeline, hint = summary_pipeline(m_start, m_end, department)
            coll = employees
        elif endpoint is ExplainTarget.late_leaderboard:
            need(month=month)
            m_start, m_end = parse_month(month)
            pipeline, hint = leaderboard_pipeline(m_start, m_end, limit, department)
            coll = logs
        else:
            need(department=department, **{"from": from_, "to": to})
            d_from, d_to = trend_range(from_, to)
            pipeline, hint = trend_pipeline(department, d_from, d_to)
            coll = employees
        command = {"aggregate": coll.name, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint.value, "collection": coll.name, "explain": to_plain_json(result)}

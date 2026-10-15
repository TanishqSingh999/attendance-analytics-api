"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Implemented following openapi.yaml contract and DATA_MODEL.md specification.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes exist at startup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    return float(d.quantize(Decimal("1." + "0" * decimals), rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_time_str(t_str: str) -> tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def determine_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> date:
    punch_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = parse_time_str(shift_start_str)
    end_h, end_m = parse_time_str(shift_end_str)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight and (punch_ist.hour, punch_ist.minute, punch_ist.second) < (end_h, end_m, 0):
        return (punch_ist - timedelta(days=1)).date()
    return punch_ist.date()


def compute_late_minutes(punch_in_dt: datetime, shift_start_str: str, attendance_date: date) -> int:
    start_h, start_m = parse_time_str(shift_start_str)
    shift_start_dt = datetime(
        attendance_date.year, attendance_date.month, attendance_date.day,
        start_h, start_m, 0, tzinfo=IST
    )
    diff_sec = (punch_in_dt.astimezone(IST) - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(sec / 3600.0, 2)


def is_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


def compute_overtime(punch_out_dt: datetime, shift_start_str: str, shift_end_str: str, attendance_date: date) -> int:
    start_h, start_m = parse_time_str(shift_start_str)
    end_h, end_m = parse_time_str(shift_end_str)
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    target_date = attendance_date + timedelta(days=1) if is_overnight else attendance_date
    shift_end_dt = datetime(target_date.year, target_date.month, target_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = (punch_out_dt.astimezone(IST) - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def count_working_days_in_month(year: int, month: int, joined_on_str: str) -> int:
    _, last_day = calendar.monthrange(year, month)
    m_start = date(year, month, 1)
    m_end = date(year, month, last_day)
    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(m_start, joined_date)
    if start_date > m_end:
        return 0
    work_days = 0
    curr = start_date
    while curr <= m_end:
        if curr.weekday() < 5:
            work_days += 1
        curr += timedelta(days=1)
    return work_days


def format_attendance_doc(doc: dict) -> dict:
    history = []
    for h in doc.get("history", []):
        entry = {
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            entry["changes"][k] = {
                "from": dt_to_epoch_ms(v["from"]) if k in ("punch_in", "punch_out") and isinstance(v["from"], datetime) else v["from"],
                "to": dt_to_epoch_ms(v["to"]) if k in ("punch_in", "punch_out") and isinstance(v["to"], datetime) else v["to"],
            }
        history.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_end")
    @classmethod
    def validate_shift(cls, v, info):
        start = info.data.get("shift_start", "09:30")
        if v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {str(e)}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    created_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(created_dt)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for it in items:
        it["created_at"] = dt_to_epoch_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
    att_date = determine_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    date_str = att_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_out_dt = epoch_ms_to_dt(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No corresponding punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    att_date = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], att_date)
    half_day = is_half_day(work_hours)

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=True,
    )
    if not res:
        raise HTTPException(409, "Record already punched out by concurrent request")

    return format_attendance_doc(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date_str"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found for date")

    target_status = body.status or record["status"]
    is_absent_leave = target_status in ("ABSENT", "LEAVE")

    if is_absent_leave and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")

    new_punch_in = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_punch_out = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if is_absent_leave:
        new_punch_in = None
        new_punch_out = None
    elif new_punch_in is None:
        raise HTTPException(422, "Presence status requires punch_in")

    att_date = date.fromisoformat(date_str)
    if new_punch_in is not None:
        calc_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != att_date:
            raise HTTPException(422, "punch_in must stay on the record attendance date")

    if new_punch_in and new_punch_out:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if is_absent_leave:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], att_date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], att_date)
            new_half_day = is_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    field_changes = {}
    def check_change(field, old_v, new_v):
        if old_v != new_v:
            field_changes[field] = {"from": old_v, "to": new_v}

    check_change("status", record.get("status"), target_status)
    check_change("punch_in", record.get("punch_in"), new_punch_in)
    check_change("punch_out", record.get("punch_out"), new_punch_out)
    check_change("work_hours", record.get("work_hours"), new_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), new_overtime)
    check_change("half_day", record.get("half_day", False), new_half_day)

    if not field_changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": field_changes,
    }

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if not res:
        raise HTTPException(409, "Concurrent modification detected")

    return format_attendance_doc(res)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    working_days = count_working_days_in_month(year, m, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "records": {"$push": "$$ROOT"},
            }
        },
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    stat = res[0] if res else {
        "leave_days": 0, "late_count": 0, "total_late_minutes": 0,
        "total_overtime_minutes": 0, "records": []
    }

    present_days = 0.0
    for r in stat.get("records", []):
        r_date = date.fromisoformat(r["date"])
        if r_date.weekday() < 5 and r.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
            present_days += 0.5 if r.get("half_day") else 1.0

    att_pct = None
    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100.0, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": stat["leave_days"],
        "late_count": stat["late_count"],
        "total_late_minutes": stat["total_late_minutes"],
        "total_overtime_minutes": stat["total_overtime_minutes"],
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    end_of_month_str = f"{month}-{last_day:02d}"

    emp_match = {"joined_on": {"$lte": end_of_month_str}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$code"]},
                                {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}}
                            ]
                        }
                    }}
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                }
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]

    dept_docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in dept_docs:
        wh_list = d.get("work_hours_list", [])
        avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2) if wh_list else None
        items.append({
            "department": d["_id"],
            "headcount": len(d.get("employees_set", [])),
            "present_days": round_half_up(d.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": d.get("late_count", 0),
            "total_late_minutes": d.get("total_late_minutes", 0),
            "leave_count": d.get("leave_count", 0),
            "on_duty_count": d.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    match_stage = {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}
    pipeline = [
        {"$match": match_stage},
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp": {"$ne": None}, "emp.department": department}})

    pipeline.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})
    results = list(db.attendance_logs.aggregate(pipeline))

    ranked_items = []
    for idx, item in enumerate(results):
        if idx > 0 and item["total_late_minutes"] == results[idx - 1]["total_late_minutes"]:
            rank = ranked_items[-1]["rank"]
        else:
            rank = idx + 1
        ranked_items.append({
            "rank": rank,
            "emp_code": item["_id"],
            "name": item["emp"]["name"],
            "department": item["emp"]["department"],
            "total_late_minutes": item["total_late_minutes"],
            "late_count": item["late_count"],
        })

    filtered = [it for it in ranked_items if it["rank"] <= limit]
    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(422, "'to' date must be >= 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1, "emp_code": 1}))
    if not dept_employees:
        raise HTTPException(404, "Department not found or has no employees")

    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": [e["emp_code"] for e in dept_employees]},
        "date": {"$gte": from_date, "$lte": to_date},
    }))

    logs_by_date = {}
    for lg in logs:
        logs_by_date.setdefault(lg["date"], []).append(lg)

    days_trend = []
    curr = d_from
    while curr <= d_to:
        d_str = curr.isoformat()
        is_working = curr.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= d_str)
        day_logs = logs_by_date.get(d_str, [])

        pres_count = 0.0
        late_count = 0
        for l in day_logs:
            if l.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                pres_count += 0.5 if l.get("half_day") else 1.0
            if l.get("late_minutes", 0) > 0:
                late_count += 1

        att_rate = None
        if is_working and hc > 0:
            att_rate = round_half_up(pres_count / hc, 4)

        days_trend.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": round_half_up(pres_count, 2),
            "late_count": late_count,
            "attendance_rate": att_rate,
        })
        curr += timedelta(days=1)

    for i, day in enumerate(days_trend):
        window_start = max(0, i - 6)
        rates = [
            days_trend[j]["attendance_rate"]
            for j in range(window_start, i + 1)
            if days_trend[j]["attendance_rate"] is not None
        ]
        day["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4) if rates else None

    return {"department": department, "items": days_trend}


@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint(
    endpoint_name: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint_name == "attendance_list":
        q = {}
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        plan = db.command("explain", cursor._Cursor__cmd, verbosity="executionStats")
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": plan}

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}}
        ]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": plan}

    elif endpoint_name == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        pipeline = [{"$match": {"joined_on": {"$lte": f"{month}-31"}}}]
        plan = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint_name, "collection": "employees", "explain": plan}

    elif endpoint_name == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}
        ]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": plan}

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to required")
        plan = db.command(
            "explain",
            {"find": "attendance_logs", "filter": {"date": {"$gte": from_date, "$lte": to_date}}},
            verbosity="executionStats"
        )
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": plan}

    raise HTTPException(422, "Unknown endpoint")

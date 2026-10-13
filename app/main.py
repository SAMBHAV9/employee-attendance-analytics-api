"""Employee Attendance & Analytics API."""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENCE + ("ABSENT", "LEAVE")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_uq")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="employee_date_uq")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_late_date")


def trunc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise ValueError("timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def ms(dt):
    return int(trunc_dt(dt).timestamp() * 1000) if dt else None


def round_half(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def hm(s):
    h, m = map(int, s.split(":")); return h, m


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = trunc_dt(punch_in).astimezone(IST)
    sh, smm = hm(shift_start); eh, emm = hm(shift_end)
    if (eh, emm) <= (sh, smm) and (local.hour, local.minute) < (eh, emm):
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_boundary(day: str, hhmm: str, overnight=False):
    d = date.fromisoformat(day) + (timedelta(days=1) if overnight else timedelta())
    h, m = hm(hhmm)
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def late_minutes(pin, shift_start, day):
    boundary = shift_boundary(day, shift_start)
    elapsed = int((pin - boundary).total_seconds() // 60)
    return elapsed if (pin - boundary).total_seconds() > 600 else 0


def derived(emp, day, pin, pout):
    if pin is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    pin = trunc_dt(pin); pout = trunc_dt(pout) if pout else None
    late = late_minutes(pin, emp["shift_start"], day)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = round_half((pout-pin).total_seconds()/3600)
    overnight = hm(emp["shift_end"]) <= hm(emp["shift_start"])
    end = shift_boundary(day, emp["shift_end"], overnight)
    ot = int((pout-end).total_seconds() // 60) if pout > end else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": ot if ot >= 30 else 0, "half_day": hours < 4.5}


def valid_date(v):
    try:
        parsed = date.fromisoformat(v)
        if parsed.isoformat() != v:
            raise ValueError("date must be YYYY-MM-DD")
        return v
    except Exception: raise ValueError("date must be YYYY-MM-DD")


def api_doc(d):
    d = dict(d); d.pop("_id", None)
    if "id" in d: pass
    else: d.pop("id", None)
    for k in ("punch_in", "punch_out", "created_at", "at"):
        if k in d: d[k] = ms(d[k])
    for h in d.get("history", []) or []:
        h["at"] = ms(h.get("at"))
        for k, change in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                change["from"] = ms(change.get("from")); change["to"] = ms(change.get("to"))
    d.setdefault("history", []); d.setdefault("half_day", False); d.setdefault("late_minutes", 0); d.setdefault("overtime_minutes", 0)
    d.setdefault("punch_in", None); d.setdefault("punch_out", None); d.setdefault("work_hours", None)
    return d


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_check(cls, v): return valid_date(v)
    @field_validator("shift_end")
    @classmethod
    def end_check(cls, v, info):
        if info.data.get("shift_start") == v: raise ValueError("shift_start and shift_end must differ")
        return v


class PunchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at", mode="before")
    @classmethod
    def timestamp(cls, v): return None if v is None else from_ms(v) and v


class PunchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    @field_validator("punched_at", mode="before")
    @classmethod
    def timestamp(cls, v): return None if v is None else from_ms(v) and v


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def timestamp(cls, v): return None if v is None else from_ms(v) and v


def page_args(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100: raise HTTPException(422, "invalid pagination")


def month_range(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""): raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(month+"-01"); end = date(start.year+(start.month==12), start.month%12+1, 1)-timedelta(days=1)
    return start.isoformat(), end.isoformat()


def working_days(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return sum((a+timedelta(days=i)).weekday() < 5 for i in range(max(0, (b-a).days+1)))


@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return api_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str]=None, page: int=1, page_size: int=20):
    page_args(page,page_size); q = {"department":department} if department is not None else {}
    total=db.employees.count_documents(q); items=list(db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items":[api_doc(x) for x in items],"total":total,"page":page,"page_size":page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=trunc_dt(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC)); day=attendance_day(ts,emp["shift_start"],emp["shift_end"])
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,**derived(emp,day,ts,None),"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return api_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=trunc_dt(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    pin=trunc_dt(rec["punch_in"])
    if ts<=pin or ts-pin>timedelta(hours=24): raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    vals=derived(emp,rec["date"],pin,ts)
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,**vals}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"already punched out")
    return api_doc(updated)


@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=1,page_size:int=20):
    page_args(page,page_size); q={}
    if emp_code: q["emp_code"]=emp_code
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        try:
            if date_from: valid_date(date_from)
            if date_to: valid_date(date_to)
        except ValueError as e: raise HTTPException(422,str(e))
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
        if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must not exceed date_to")
    total=db.attendance_logs.count_documents(q)
    items=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[api_doc(x) for x in items],"total":total,"page":page,"page_size":page_size}


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code:str,day:str,body:Correction):
    try: valid_date(day)
    except ValueError as e: raise HTTPException(422,str(e))
    emp=db.employees.find_one({"emp_code":emp_code}); rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":day})
    if not emp or not rec: raise HTTPException(404,"employee or attendance record not found")
    new_status=body.status or rec["status"]
    if new_status in ("ABSENT","LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422,"absent/leave cannot have punch times")
        pin=pout=None
    else:
        pin=from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout=from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if not pin: raise HTTPException(422,"presence status requires punch_in")
        pin=trunc_dt(pin); pout=trunc_dt(pout) if pout else None
        if attendance_day(pin,emp["shift_start"],emp["shift_end"])!=day: raise HTTPException(422,"punch_in must belong to attendance date")
        if pout and (pout<=pin or pout-pin>timedelta(hours=24)): raise HTTPException(422,"invalid punch_out range")
    vals=derived(emp,day,pin,pout)
    final={"status":new_status,"punch_in":pin,"punch_out":pout,**vals}
    fields=("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")
    changes={}
    for k in fields:
        old=rec.get(k, {"late_minutes":0,"overtime_minutes":0,"half_day":False,"history":[]}.get(k))
        new=final[k]
        if k in ("punch_in","punch_out"):
            old=trunc_dt(old) if old else None
        if old!=new: changes[k]={"from":old,"to":new}
    if not changes: raise HTTPException(422,"correction makes no changes")
    entry={"at":datetime.now(UTC),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Compare-and-swap prevents a concurrent editor from overwriting history.
    compare_and_swap={"_id":rec["_id"]}
    if "history" in rec:
        compare_and_swap["history"]=rec["history"]
    else:
        compare_and_swap["$or"]=[{"history":{"$exists":False}},{"history":[]}]
    updated=db.attendance_logs.find_one_and_update(compare_and_swap,{"$set":final,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record changed concurrently")
    return api_doc(updated)


def daily_pipeline(start,end,extra_match=None):
    match={"date":{"$gte":start,"$lte":end}}
    if extra_match: match.update(extra_match)
    return [{"$match":match},{"$group":{"_id":"$date","present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},{"$project":{"_id":0,"date":"$_id","present_count":1,"late_count":1}},{"$sort":{"date":1}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    start,end=month_range(month); emp=db.employees.find_one({"emp_code":emp_code},{"_id":0,"joined_on":1})
    if not emp: raise HTTPException(404,"employee not found")
    work=working_days(max(start,emp["joined_on"]),end) if emp["joined_on"]<=end else 0
    pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":start,"$lte":end}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]},{"$ne":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},7]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    row=next(db.attendance_logs.aggregate(pipe),{})
    p=row.get("present_days",0); return {"emp_code":emp_code,"month":month,"working_days":work,"present_days":round_half(p),"leave_days":row.get("leave_days",0),"late_count":row.get("late_count",0),"total_late_minutes":row.get("total_late_minutes",0),"total_overtime_minutes":row.get("total_overtime_minutes",0),"attendance_pct":round_half(p/work,4)*100 if work else None}


@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    start,end=month_range(month)
    pipe=[{"$match":{"joined_on":{"$lte":end},**({"department":department} if department else {})}},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start]},{"$lte":["$date",end]}]}}}],"as":"logs"}},{"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},{"$group":{"_id":"$department","headcount":{"$addToSet":"$emp_code"},"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$not":[{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$logs.date"}}},[1,7]]}]}]},{"$cond":[{"$eq":["$logs.half_day",True]},0.5,1]},0]}},"work_sum":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":["$logs.work_hours",None]}]},"$logs.work_hours",0]}},"work_n":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":["$logs.work_hours",None]}]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$logs.late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$logs.late_minutes",0]}},"leave_count":{"$sum":{"$cond":[{"$eq":["$logs.status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$logs.status","ON_DUTY"]},1,0]}}}},{"$project":{"_id":0,"department":"$_id","headcount":{"$size":"$headcount"},"present_days":{"$round":["$present_days",2]},"avg_work_hours":{"$cond":[{"$gt":["$work_n",0]},{"$round":[{"$divide":["$work_sum","$work_n"]},2]},None]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},{"$sort":{"department":1}}]
    return {"month":month,"items":list(db.employees.aggregate(pipe))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=month_range(month)
    pipe=[{"$match":{"date":{"$gte":start,"$lte":end}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},{"$match":{"total_late_minutes":{"$gt":0}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None: pipe.append({"$match":{"employee.department":department}})
    pipe += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},{"$project":{"_id":0,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1,"rank":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipe))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    try: valid_date(from_);valid_date(to)
    except ValueError as e: raise HTTPException(422,str(e))
    a,b=date.fromisoformat(from_),date.fromisoformat(to)
    if b<a or (b-a).days+1>92: raise HTTPException(422,"invalid trend range")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"department not found")
    begin=datetime.combine(a,time(),UTC)
    # $documents gives a calendar spine even when the date range has no logs.
    pipe=[{"$documents":[{"start":begin,"days":(b-a).days+1}]},{"$project":{"days":{"$map":{"input":{"$range":[0,"$days"]},"as":"n","in":{"$dateAdd":{"startDate":"$start","unit":"day","amount":"$$n"}}}}}},{"$unwind":"$days"},{"$set":{"date":{"$dateToString":{"date":"$days","format":"%Y-%m-%d"}}}},{"$lookup":{"from":"employees","let":{"d":"$date"},"pipeline":[{"$match":{"department":department,"$expr":{"$lte":["$joined_on","$$d"]}}},{"$project":{"_id":0,"emp_code":1}}],"as":"people"}},{"$lookup":{"from":"attendance_logs","let":{"d":"$date","codes":"$people.emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$date","$$d"]},{"$in":["$emp_code","$$codes"]}]}}},{"$project":{"status":1,"half_day":1,"late_minutes":1}}],"as":"logs"}},{"$set":{"headcount":{"$size":"$people"},"present_count":{"$reduce":{"input":"$logs","initialValue":0,"in":{"$add":["$$value",{"$cond":[{"$in":["$$this.status",list(PRESENCE)]},{"$cond":[{"$eq":["$$this.half_day",True]},0.5,1]},0]}]}}},"late_count":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$gt":[{"$ifNull":["$$l.late_minutes",0]},0]}}}},"is_working_day":{"$not":[{"$in":[{"$dayOfWeek":"$days"},[1,7]]}]}}},{"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},{"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},{"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}]
    cursor=db.command("aggregate",1,pipeline=pipe,cursor={})["cursor"]
    return {"department":department,"from":from_,"to":to,"items":cursor["firstBatch"]}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=10,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None,page:int=1,page_size:int=20):
    names={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in names: raise HTTPException(422,"unknown endpoint")
    page_args(page,page_size)
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=valid_date(date_from)
            if date_to:q["date"]["$lte"]=valid_date(date_to)
        cmd={"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        coll="attendance_logs"
    elif endpoint=="employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month required")
        start,end=month_range(month); pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":start,"$lte":end}}}]
        cmd={"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}};coll="attendance_logs"
    elif endpoint in ("department_summary","late_leaderboard"):
        if not month: raise HTTPException(422,"month required")
        start,end=month_range(month); q={"date":{"$gte":start,"$lte":end}}
        cmd={"aggregate":"attendance_logs","pipeline":[{"$match":q}],"cursor":{}};coll="attendance_logs"
    else:
        if not department or not from_ or not to: raise HTTPException(422,"department, from and to required")
        start,end=valid_date(from_),valid_date(to)
        cmd={"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":start,"$lte":end}}}],"cursor":{}};coll="attendance_logs"
    try: raw=db.command("explain",cmd,verbosity="executionStats")
    except Exception as e: raise HTTPException(422,str(e))
    return {"endpoint":endpoint,"collection":coll,"explain":json_util.loads(json_util.dumps(raw))}

from fastapi import FastAPI, APIRouter, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError
from passlib.hash import bcrypt
import jwt as pyjwt
import os
import io
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime, timezone, date, timedelta
import uuid

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

INITIAL_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "herisau2026")
JWT_SECRET = os.environ.get("JWT_SECRET", "herisau_schularzt_jwt_secret_2026")
JWT_ALG = "HS256"

# ---------------- Fixed time slots (16 individual child slots) ----------------
_TIME_PAIRS = [
    ("08:30", "08:45", "08:45", "09:06"),
    ("08:50", "09:05", "09:06", "09:27"),
    ("09:10", "09:25", "09:27", "09:48"),
    ("09:30", "09:45", "09:48", "10:09"),
    ("09:50", "10:05", "10:09", "10:30"),
    ("10:10", "10:25", "10:30", "10:51"),
    ("10:30", "10:45", "10:51", "11:12"),
    ("10:50", "11:05", "11:12", "11:33"),
]
TIME_SLOTS = []
for _pi, (_ms, _me, _as, _ae) in enumerate(_TIME_PAIRS):
    for _off in (0, 1):
        TIME_SLOTS.append({
            "index": _pi * 2 + _off,
            "kid_number": _pi * 2 + _off + 1,
            "pair_index": _pi,
            "pair_label": f"{_pi * 2 + 1}+{_pi * 2 + 2}",
            "mpa_start": _ms, "mpa_end": _me,
            "arzt_start": _as, "arzt_end": _ae,
        })

SLOTS_PER_DAY = 16

DEFAULT_HOLIDAYS = [
    {"name": "Sommerferien 2026", "start_date": "2026-07-06", "end_date": "2026-08-14"},
    {"name": "Herbstferien 2026", "start_date": "2026-10-05", "end_date": "2026-10-16"},
    {"name": "Weihnachtsferien 2026/27", "start_date": "2026-12-21", "end_date": "2027-01-08"},
    {"name": "Sportferien 2027", "start_date": "2027-01-25", "end_date": "2027-02-05"},
    {"name": "Frühlingsferien 2027", "start_date": "2027-04-06", "end_date": "2027-04-23"},
    {"name": "Sommerferien 2027", "start_date": "2027-07-05", "end_date": "2027-07-31"},
    {"name": "Auffahrt", "start_date": "2027-05-06", "end_date": "2027-05-07"},
    {"name": "Pfingstmontag", "start_date": "2027-05-17", "end_date": "2027-05-17"},
    {"name": "Tag der Arbeit", "start_date": "2027-05-01", "end_date": "2027-05-01"},
]

SCHOOL_YEAR_START = date(2026, 8, 17)
SCHOOL_YEAR_END = date(2027, 7, 9)


# ---------------- Models ----------------
class Booking(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    date: str
    slot_index: int
    municipality: str
    gemeinde_id: Optional[str] = None
    contact_person: str = ""
    note: Optional[str] = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BookingCreate(BaseModel):
    date: str
    slot_index: int
    municipality: Optional[str] = None
    contact_person: str
    note: Optional[str] = ""


class BatchBookingCreate(BaseModel):
    date: str
    slot_indices: List[int]
    municipality: Optional[str] = None
    contact_person: str
    note: Optional[str] = ""


class BookingUpdate(BaseModel):
    municipality: Optional[str] = None
    contact_person: Optional[str] = None
    note: Optional[str] = None


class Holiday(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    start_date: str
    end_date: str


class HolidayCreate(BaseModel):
    name: str
    start_date: str
    end_date: str


class DayBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    date: str
    reason: Optional[str] = ""


class DayBlockCreate(BaseModel):
    date: str
    reason: Optional[str] = ""


class AdminLoginRequest(BaseModel):
    password: str


class AdminChangePassword(BaseModel):
    current_password: str
    new_password: str


class Gemeinde(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    username: str
    password_hash: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class GemeindeCreate(BaseModel):
    name: str
    username: str
    password: str


class GemeindeUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None


class GemeindeLoginRequest(BaseModel):
    username: str
    password: str


# ---------------- Admin password storage ----------------
async def get_admin_hash() -> Optional[str]:
    doc = await db.settings.find_one({"key": "admin_password"})
    return doc["value"] if doc else None


async def set_admin_hash(hashed: str):
    await db.settings.update_one({"key": "admin_password"}, {"$set": {"value": hashed}}, upsert=True)


def create_admin_token() -> str:
    return pyjwt.encode(
        {"role": "admin", "iat": int(datetime.now(timezone.utc).timestamp())},
        JWT_SECRET, algorithm=JWT_ALG,
    )


def verify_admin_token(token: str) -> bool:
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        return payload.get("role") == "admin"
    except Exception:
        return False


def require_admin(x_admin_password: Optional[str] = Header(None)):
    if not x_admin_password or not verify_admin_token(x_admin_password):
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    return True


# ---------------- Gemeinde auth ----------------
def create_gemeinde_token(gemeinde_id: str, name: str) -> str:
    return pyjwt.encode(
        {"gid": gemeinde_id, "name": name, "iat": int(datetime.now(timezone.utc).timestamp())},
        JWT_SECRET, algorithm=JWT_ALG,
    )


async def require_gemeinde(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    token = authorization.replace("Bearer ", "", 1)
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(status_code=401, detail="Ungültiges Token")
    doc = await db.gemeinden.find_one({"id": payload.get("gid")}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=401, detail="Gemeinde nicht gefunden")
    return doc


# ---------------- Helpers ----------------
async def get_holidays_list():
    return await db.holidays.find({}, {"_id": 0}).to_list(1000)


async def get_blocks_list():
    return await db.day_blocks.find({}, {"_id": 0}).to_list(1000)


def date_in_holidays(d: str, holidays: List[dict]) -> Optional[str]:
    for h in holidays:
        if h["start_date"] <= d <= h["end_date"]:
            return h["name"]
    return None


def compute_day_status(d: date, holidays: List[dict], blocks: List[dict]) -> dict:
    d_str = d.isoformat()
    weekday = d.weekday()
    if weekday >= 5:
        return {"status": "weekend", "label": "Wochenende", "bookable": False}
    hol = date_in_holidays(d_str, holidays)
    if hol:
        return {"status": "holiday", "label": hol, "bookable": False}
    block = next((b for b in blocks if b["date"] == d_str), None)
    if block:
        return {"status": "blocked", "label": block.get("reason") or "Gesperrt", "bookable": False}
    return {"status": "school", "label": "Schulbetrieb", "bookable": True}


def slot_by_index(idx: int) -> Optional[dict]:
    for s in TIME_SLOTS:
        if s["index"] == idx:
            return s
    return None


# ---------------- Public routes ----------------
@api_router.get("/")
async def root():
    return {"message": "Schularzt Herisau API"}


@api_router.get("/config")
async def get_config():
    return {
        "time_slots": TIME_SLOTS,
        "school_year_start": SCHOOL_YEAR_START.isoformat(),
        "school_year_end": SCHOOL_YEAR_END.isoformat(),
    }


@api_router.get("/days")
async def get_days():
    holidays = await get_holidays_list()
    blocks = await get_blocks_list()
    bookings = await db.bookings.find({}, {"_id": 0}).to_list(10000)
    by_date = {}
    for b in bookings:
        by_date.setdefault(b["date"], []).append(b)

    days = []
    d = SCHOOL_YEAR_START
    while d <= SCHOOL_YEAR_END:
        d_str = d.isoformat()
        st = compute_day_status(d, holidays, blocks)
        n = len(by_date.get(d_str, []))
        days.append({
            "date": d_str, "weekday": d.weekday(),
            "status": st["status"], "label": st["label"], "bookable": st["bookable"],
            "booked_count": n,
            "free_count": (SLOTS_PER_DAY - n) if st["bookable"] else 0,
            "total_slots": SLOTS_PER_DAY if st["bookable"] else 0,
        })
        d += timedelta(days=1)
    return {"days": days}


@api_router.get("/days/{d}")
async def get_day_detail(d: str):
    try:
        day_obj = date.fromisoformat(d)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungültiges Datum")
    holidays = await get_holidays_list()
    blocks = await get_blocks_list()
    st = compute_day_status(day_obj, holidays, blocks)
    bookings = await db.bookings.find({"date": d}, {"_id": 0}).to_list(100)
    by_slot = {b["slot_index"]: b for b in bookings}
    slots = [{**s, "booked": by_slot.get(s["index"]) is not None, "booking": by_slot.get(s["index"])} for s in TIME_SLOTS]
    return {
        "date": d, "weekday": day_obj.weekday(),
        "status": st["status"], "label": st["label"], "bookable": st["bookable"],
        "slots": slots,
        "booked_count": len(bookings),
        "free_count": (SLOTS_PER_DAY - len(bookings)) if st["bookable"] else 0,
        "total_slots": SLOTS_PER_DAY if st["bookable"] else 0,
    }


async def _resolve_municipality(authorization: Optional[str], provided: Optional[str]) -> tuple:
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = pyjwt.decode(authorization.replace("Bearer ", "", 1), JWT_SECRET, algorithms=[JWT_ALG])
            g = await db.gemeinden.find_one({"id": payload.get("gid")}, {"_id": 0})
            if g:
                return g["name"], g["id"]
        except Exception:
            pass
    if provided and provided.strip():
        return provided.strip(), None
    raise HTTPException(status_code=400, detail="Gemeinde erforderlich")


@api_router.post("/bookings", response_model=Booking)
async def create_booking(payload: BookingCreate, authorization: Optional[str] = Header(None)):
    try:
        day_obj = date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungültiges Datum")
    if payload.slot_index < 0 or payload.slot_index >= SLOTS_PER_DAY:
        raise HTTPException(status_code=400, detail="Ungültiger Slot")

    holidays = await get_holidays_list()
    blocks = await get_blocks_list()
    st = compute_day_status(day_obj, holidays, blocks)
    if not st["bookable"]:
        raise HTTPException(status_code=400, detail=f"Tag ist nicht buchbar: {st['label']}")

    muni, gid = await _resolve_municipality(authorization, payload.municipality)
    if not payload.contact_person or not payload.contact_person.strip():
        raise HTTPException(status_code=400, detail="Kontaktperson erforderlich")
    booking = Booking(date=payload.date, slot_index=payload.slot_index, municipality=muni, gemeinde_id=gid, contact_person=payload.contact_person.strip(), note=(payload.note or "").strip())
    try:
        await db.bookings.insert_one(booking.model_dump())
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Dieser Platz wurde soeben von einer anderen Gemeinde gebucht.")
    return booking


@api_router.post("/bookings/batch")
async def create_bookings_batch(payload: BatchBookingCreate, authorization: Optional[str] = Header(None)):
    try:
        day_obj = date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungültiges Datum")
    if not payload.slot_indices:
        raise HTTPException(status_code=400, detail="Keine Plätze ausgewählt")
    for idx in payload.slot_indices:
        if idx < 0 or idx >= SLOTS_PER_DAY:
            raise HTTPException(status_code=400, detail=f"Ungültiger Slot: {idx}")

    holidays = await get_holidays_list()
    blocks = await get_blocks_list()
    st = compute_day_status(day_obj, holidays, blocks)
    if not st["bookable"]:
        raise HTTPException(status_code=400, detail=f"Tag ist nicht buchbar: {st['label']}")

    muni, gid = await _resolve_municipality(authorization, payload.municipality)
    if not payload.contact_person or not payload.contact_person.strip():
        raise HTTPException(status_code=400, detail="Kontaktperson erforderlich")
    contact = payload.contact_person.strip()
    created, failed = [], []
    for idx in payload.slot_indices:
        b = Booking(date=payload.date, slot_index=idx, municipality=muni, gemeinde_id=gid, contact_person=contact, note=(payload.note or "").strip())
        try:
            await db.bookings.insert_one(b.model_dump())
            created.append(b.model_dump())
        except DuplicateKeyError:
            failed.append({"slot_index": idx, "reason": "Bereits gebucht"})
    return {"created": created, "failed": failed}


@api_router.get("/bookings")
async def list_bookings(_: bool = Depends(require_admin)):
    bookings = await db.bookings.find({}, {"_id": 0}).sort("date", ASCENDING).to_list(10000)
    return {"bookings": bookings}


@api_router.put("/bookings/{booking_id}")
async def update_booking(booking_id: str, payload: BookingUpdate, _: bool = Depends(require_admin)):
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Keine Änderungen")
    res = await db.bookings.update_one({"id": booking_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    return await db.bookings.find_one({"id": booking_id}, {"_id": 0})


@api_router.delete("/bookings/{booking_id}")
async def delete_booking(booking_id: str, _: bool = Depends(require_admin)):
    res = await db.bookings.delete_one({"id": booking_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    return {"ok": True}


# ---------------- Holidays ----------------
@api_router.get("/holidays")
async def list_holidays():
    docs = await db.holidays.find({}, {"_id": 0}).sort("start_date", ASCENDING).to_list(1000)
    return {"holidays": docs}


@api_router.post("/holidays", response_model=Holiday)
async def add_holiday(payload: HolidayCreate, _: bool = Depends(require_admin)):
    h = Holiday(**payload.model_dump())
    await db.holidays.insert_one(h.model_dump())
    return h


@api_router.delete("/holidays/{holiday_id}")
async def delete_holiday(holiday_id: str, _: bool = Depends(require_admin)):
    res = await db.holidays.delete_one({"id": holiday_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Ferien nicht gefunden")
    return {"ok": True}


# ---------------- Day blocks ----------------
@api_router.post("/day-blocks")
async def add_day_block(payload: DayBlockCreate, _: bool = Depends(require_admin)):
    existing = await db.day_blocks.find_one({"date": payload.date})
    if existing:
        return {"ok": True, "already": True}
    b = DayBlock(**payload.model_dump())
    await db.day_blocks.insert_one(b.model_dump())
    return b


@api_router.delete("/day-blocks/{d}")
async def remove_day_block(d: str, _: bool = Depends(require_admin)):
    await db.day_blocks.delete_one({"date": d})
    return {"ok": True}


# ---------------- Admin auth & password ----------------
@api_router.post("/admin/login")
async def admin_login(payload: AdminLoginRequest):
    stored = await get_admin_hash()
    if not stored or not bcrypt.verify(payload.password, stored):
        raise HTTPException(status_code=401, detail="Falsches Passwort")
    return {"ok": True, "token": create_admin_token()}


@api_router.post("/admin/change-password")
async def admin_change_password(payload: AdminChangePassword, _: bool = Depends(require_admin)):
    stored = await get_admin_hash()
    if not stored or not bcrypt.verify(payload.current_password, stored):
        raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Neues Passwort mindestens 6 Zeichen")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="Neues Passwort muss sich unterscheiden")
    await set_admin_hash(bcrypt.hash(payload.new_password))
    return {"ok": True, "token": create_admin_token()}


# ---------------- Gemeinden (Admin CRUD) ----------------
@api_router.get("/gemeinden")
async def list_gemeinden(_: bool = Depends(require_admin)):
    docs = await db.gemeinden.find({}, {"_id": 0, "password_hash": 0}).sort("name", ASCENDING).to_list(1000)
    return {"gemeinden": docs}


@api_router.post("/gemeinden")
async def create_gemeinde(payload: GemeindeCreate, _: bool = Depends(require_admin)):
    name = payload.name.strip()
    username = payload.username.strip().lower()
    if not name or not username or not payload.password:
        raise HTTPException(status_code=400, detail="Name, Benutzername und Passwort erforderlich")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Passwort mindestens 4 Zeichen")
    if await db.gemeinden.find_one({"username": username}):
        raise HTTPException(status_code=409, detail="Benutzername existiert bereits")
    g = Gemeinde(name=name, username=username, password_hash=bcrypt.hash(payload.password))
    await db.gemeinden.insert_one(g.model_dump())
    d = g.model_dump()
    d.pop("password_hash", None)
    return d


@api_router.put("/gemeinden/{gid}")
async def update_gemeinde(gid: str, payload: GemeindeUpdate, _: bool = Depends(require_admin)):
    update = {}
    if payload.name is not None and payload.name.strip():
        update["name"] = payload.name.strip()
    if payload.password:
        if len(payload.password) < 4:
            raise HTTPException(status_code=400, detail="Passwort mindestens 4 Zeichen")
        update["password_hash"] = bcrypt.hash(payload.password)
    if not update:
        raise HTTPException(status_code=400, detail="Keine Änderungen")
    res = await db.gemeinden.update_one({"id": gid}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Gemeinde nicht gefunden")
    if "name" in update:
        await db.bookings.update_many({"gemeinde_id": gid}, {"$set": {"municipality": update["name"]}})
    return {"ok": True}


@api_router.delete("/gemeinden/{gid}")
async def delete_gemeinde(gid: str, _: bool = Depends(require_admin)):
    res = await db.gemeinden.delete_one({"id": gid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gemeinde nicht gefunden")
    return {"ok": True}


# ---------------- Gemeinde login & data ----------------
@api_router.post("/gemeinde/login")
async def gemeinde_login(payload: GemeindeLoginRequest):
    doc = await db.gemeinden.find_one({"username": payload.username.strip().lower()})
    if not doc or not bcrypt.verify(payload.password, doc.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Benutzername oder Passwort falsch")
    token = create_gemeinde_token(doc["id"], doc["name"])
    return {"token": token, "gemeinde": {"id": doc["id"], "name": doc["name"], "username": doc["username"]}}


@api_router.get("/gemeinde/me")
async def gemeinde_me(g: dict = Depends(require_gemeinde)):
    return {"id": g["id"], "name": g["name"], "username": g["username"]}


@api_router.get("/gemeinde/bookings")
async def gemeinde_bookings(g: dict = Depends(require_gemeinde)):
    bookings = await db.bookings.find({"gemeinde_id": g["id"]}, {"_id": 0}).sort("date", ASCENDING).to_list(2000)
    for b in bookings:
        s = slot_by_index(b["slot_index"])
        if s:
            b["mpa_start"] = s["mpa_start"]; b["mpa_end"] = s["mpa_end"]
            b["arzt_start"] = s["arzt_start"]; b["arzt_end"] = s["arzt_end"]
            b["kid_number"] = s["kid_number"]
    return {"bookings": bookings}


def _format_ch(iso_date: str) -> str:
    try:
        y, m, d = iso_date.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return iso_date


@api_router.delete("/gemeinde/bookings/{booking_id}")
async def gemeinde_delete_booking(booking_id: str, g: dict = Depends(require_gemeinde)):
    booking = await db.bookings.find_one({"id": booking_id}, {"_id": 0})
    if not booking:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    if booking.get("gemeinde_id") != g["id"]:
        raise HTTPException(status_code=403, detail="Diese Buchung gehört nicht zu Ihrer Gemeinde")
    await db.bookings.delete_one({"id": booking_id})
    return {"ok": True}


@api_router.get("/gemeinde/bookings/pdf")
async def gemeinde_bookings_pdf(g: dict = Depends(require_gemeinde)):
    bookings = await db.bookings.find({"gemeinde_id": g["id"]}, {"_id": 0}).sort([("date", ASCENDING), ("slot_index", ASCENDING)]).to_list(2000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=6)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=12)
    normal = styles["Normal"]

    story = []
    story.append(Paragraph("Schulärztliche Untersuchungen – Herisau", h1))
    story.append(Paragraph(f"Buchungsübersicht für <b>{g['name']}</b>", sub))
    story.append(Paragraph(f"Erstellt am {datetime.now().strftime('%d.%m.%Y %H:%M')} · {len(bookings)} Buchung(en)", sub))
    story.append(Spacer(1, 6))

    if not bookings:
        story.append(Paragraph("Keine Buchungen vorhanden.", normal))
    else:
        rows = [["Datum", "Kind Nr.", "MPA", "Arzt", "Kontaktperson", "Bemerkung"]]
        for b in bookings:
            s = slot_by_index(b["slot_index"])
            mpa = f"{s['mpa_start']}–{s['mpa_end']}" if s else ""
            arzt = f"{s['arzt_start']}–{s['arzt_end']}" if s else ""
            kid = s["kid_number"] if s else b["slot_index"] + 1
            rows.append([_format_ch(b["date"]), str(kid), mpa, arzt, b.get("contact_person") or "-", b.get("note") or "-"])

        tbl = Table(rows, colWidths=[24 * mm, 16 * mm, 28 * mm, 28 * mm, 34 * mm, 44 * mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)

    story.append(Spacer(1, 18))
    story.append(Paragraph("Bei Fragen wenden Sie sich bitte an das Schularztsekretariat Herisau.", ParagraphStyle("f", parent=normal, fontSize=9, textColor=colors.grey)))

    doc.build(story)
    buf.seek(0)
    filename = f"buchungen_{g['username']}_{datetime.now().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------- Startup ----------------
@app.on_event("startup")
async def on_startup():
    await db.bookings.create_index([("date", ASCENDING), ("slot_index", ASCENDING)], unique=True)
    await db.holidays.create_index([("start_date", ASCENDING)])
    await db.day_blocks.create_index([("date", ASCENDING)], unique=True)
    await db.gemeinden.create_index([("username", ASCENDING)], unique=True)
    await db.settings.create_index([("key", ASCENDING)], unique=True)

    # Seed admin password hash if not present
    if not await get_admin_hash():
        await set_admin_hash(bcrypt.hash(INITIAL_ADMIN_PASSWORD))

    if await db.holidays.count_documents({}) == 0:
        for h in DEFAULT_HOLIDAYS:
            await db.holidays.insert_one(Holiday(**h).model_dump())

    if await db.bookings.count_documents({}) == 0:
        samples = [
            {"date": "2026-08-24", "slot_index": 1, "municipality": "Gemeinde Musterwil", "note": "Klasse 1a"},
            {"date": "2026-08-24", "slot_index": 3, "municipality": "Schule Waldstatt", "note": ""},
            {"date": "2026-08-25", "slot_index": 0, "municipality": "Schule Urnäsch", "note": ""},
            {"date": "2026-08-26", "slot_index": 6, "municipality": "Gemeinde Herisau", "note": "Klasse 2b"},
            {"date": "2026-09-01", "slot_index": 2, "municipality": "Schule Stein", "note": ""},
            {"date": "2026-09-01", "slot_index": 4, "municipality": "Schule Stein", "note": ""},
        ]
        for s in samples:
            try:
                await db.bookings.insert_one(Booking(**s).model_dump())
            except DuplicateKeyError:
                pass

    if await db.gemeinden.count_documents({}) == 0:
        demo = Gemeinde(name="Schule Waldstatt", username="waldstatt", password_hash=bcrypt.hash("waldstatt2026"))
        await db.gemeinden.insert_one(demo.model_dump())


@app.on_event("shutdown")
async def on_shutdown():
    client.close()


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

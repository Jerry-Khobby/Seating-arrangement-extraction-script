"""
extract_seating_v3.py
=====================
Parses SharePoint seating CSVs for Ghana offices (Kumasi, Takoradi, Accra)
and produces one CSV per schema table that relates to seating arrangement.

Seating-related tables covered (remote / OOO excluded):
    1.  organisations.csv        – ORGANISATION
    2.  offices.csv              – OFFICE
    3.  buildings.csv            – OFFICE_BUILDING
    4.  floors.csv               – FLOOR
    5.  rooms.csv                – ROOM
    6.  seats.csv                – SEAT
    7.  employees_stub.csv       – EMPLOYEE  (name stub; enrich from HR)
    8.  seat_bookings.csv        – SEAT_BOOKING
    9.  seat_occupancy_log.csv   – SEAT_OCCUPANCY_LOG
    10. work_schedules.csv       – WORK_SCHEDULE  (day-of-week seat assignment)
    11. floor_leads.csv          – FLOOR_LEAD

Usage:
    python extract_seating_v3.py
    python extract_seating_v3.py --week-start 2025-01-13
    python extract_seating_v3.py --kumasi path/to/file.csv --takoradi ... --accra ...
"""

import re
import uuid
import argparse
import datetime
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Enum values  (match schema definitions)
# ---------------------------------------------------------------------------

class SeatType:
    ASSIGNED  = "assigned"
    HOT_DESK  = "hot_desk"

class SeatStatus:
    AVAILABLE = "available"
    OCCUPIED  = "occupied"

class RoomType:
    OPEN_PLAN = "open_plan"
    HOT_DESK  = "hot_desk"

class BookingStatus:
    CONFIRMED = "confirmed"

DAY_ENUM = {
    "Monday":    "monday",
    "Tuesday":   "tuesday",
    "Wednesday": "wednesday",
    "Thursday":  "thursday",
    "Friday":    "friday",
    "Saturday":  "saturday",
    "Sunday":    "sunday",
}
DAYS_OF_WEEK = set(DAY_ENUM.keys())


# ---------------------------------------------------------------------------
# Registry  – single source of truth for all dimension UUIDs
# ---------------------------------------------------------------------------

class Registry:
    """
    Creates and caches every dimension row (org → office → building → floor →
    room → seat → employee) so all parsers share the same UUIDs and FK chains
    resolve correctly at load time.
    """

    def __init__(self, week_start: datetime.date):
        self.week_start = week_start
        self._now       = datetime.datetime.utcnow().isoformat() + "Z"

        self._orgs:      dict[str, dict] = {}   # org_name  → row
        self._offices:   dict[str, dict] = {}   # city_name → row
        self._buildings: dict[str, dict] = {}   # building_name → row
        self._floors:    dict[tuple, dict] = {} # (building_id, floor_number) → row
        self._rooms:     dict[tuple, dict] = {} # (building_id, floor_id, room_name) → row
        self._seats:     dict[tuple, dict] = {} # (room_id, seat_number) → row
        self._employees: dict[str, dict]   = {} # lower(full_name) → row

        self._seed()

    # ── Seed Ghana hierarchy upfront ────────────────────────────────────────

    def _seed(self):
        org = self._get_or_create_org("Ghana", "GH", "Africa/Accra")
        for city, building, floors in [
            ("Kumasi",   "Kumasi Office",   3),
            ("Takoradi", "Takoradi Office", 3),
            ("Accra",    "Accra Office",    5),
        ]:
            office = self._get_or_create_office(city, org["organisation_id"])
            self._get_or_create_building(building, office["office_id"], floors)

    # ── ORGANISATION ────────────────────────────────────────────────────────

    def _get_or_create_org(self, country: str, code: str, tz: str) -> dict:
        if country not in self._orgs:
            self._orgs[country] = {
                "organisation_id": _uid(),
                "country_name":    country,
                "country_code":    code,
                "timezone":        tz,
                "created_at":      self._now,
                "updated_at":      self._now,
            }
        return self._orgs[country]

    # ── OFFICE ───────────────────────────────────────────────────────────────

    def _get_or_create_office(self, city: str, org_id: str) -> dict:
        if city not in self._offices:
            self._offices[city] = {
                "office_id":       _uid(),
                "organisation_id": org_id,
                "city_name":       city,
                "created_at":      self._now,
                "updated_at":      self._now,
            }
        return self._offices[city]

    # ── OFFICE_BUILDING ──────────────────────────────────────────────────────

    def _get_or_create_building(self, name: str, office_id: str,
                                total_floors: int = 1) -> dict:
        if name not in self._buildings:
            self._buildings[name] = {
                "building_id":  _uid(),
                "office_id":    office_id,
                "building_name": name,
                "address":      None,
                "total_floors": total_floors,
                "is_active":    True,
                "created_at":   self._now,
                "updated_at":   self._now,
            }
        return self._buildings[name]

    def building_id(self, name: str) -> str:
        return self._buildings[name]["building_id"]

    # ── FLOOR ────────────────────────────────────────────────────────────────

    def get_or_create_floor(self, building_name: str, floor_number: int,
                            floor_name: str | None = None) -> dict:
        bid = self.building_id(building_name)
        key = (bid, floor_number)
        if key not in self._floors:
            self._floors[key] = {
                "floor_id":     _uid(),
                "building_id":  bid,
                "floor_name":   floor_name or f"Floor {floor_number}",
                "floor_number": floor_number,
                "is_active":    True,
                "created_at":   self._now,
                "updated_at":   self._now,
            }
        return self._floors[key]

    def floor_id(self, building_name: str, floor_number: int) -> str:
        return self.get_or_create_floor(building_name, floor_number)["floor_id"]

    # ── ROOM ─────────────────────────────────────────────────────────────────

    def get_or_create_room(self, building_name: str, floor_number: int,
                           room_name: str, total_seats: int | None,
                           room_type: str = RoomType.OPEN_PLAN) -> dict:
        bid = self.building_id(building_name)
        fid = self.floor_id(building_name, floor_number)
        key = (bid, fid, room_name)
        if key not in self._rooms:
            self._rooms[key] = {
                "room_id":     _uid(),
                "floor_id":    fid,
                "building_id": bid,
                "room_name":   room_name,
                "total_seats": total_seats,
                "room_type":   room_type,
                "is_active":   True,
                "created_at":  self._now,
                "updated_at":  self._now,
            }
        return self._rooms[key]

    # ── SEAT ─────────────────────────────────────────────────────────────────

    def get_or_create_seat(self, room: dict, seat_number: str,
                           is_hot_desk: bool = False) -> dict:
        key = (room["room_id"], seat_number)
        if key not in self._seats:
            self._seats[key] = {
                "seat_id":              _uid(),
                "room_id":              room["room_id"],
                "floor_id":             room["floor_id"],
                "building_id":          room["building_id"],
                "seat_number":          seat_number,
                "seat_type":            SeatType.HOT_DESK if is_hot_desk else SeatType.ASSIGNED,
                "status":               SeatStatus.AVAILABLE,
                "assigned_employee_id": None,   # populated after employee enrichment
                "x_position":           None,
                "y_position":           None,
                "created_at":           self._now,
                "updated_at":           self._now,
                "deleted_at":           None,
            }
        return self._seats[key]

    # ── EMPLOYEE ─────────────────────────────────────────────────────────────

    def get_or_create_employee(self, full_name: str, building_name: str) -> dict:
        key = full_name.strip().lower()
        if key not in self._employees:
            parts = full_name.strip().split(None, 1)
            self._employees[key] = {
                "employee_id":           _uid(),
                "employee_code":         None,   # enrich from HR
                "sso_user_id":           None,
                "first_name":            parts[0] if parts else full_name,
                "last_name":             parts[1] if len(parts) > 1 else None,
                "email":                 None,
                "primary_building_id":   self.building_id(building_name),
                "manager_id":            None,
                "employee_type":         None,
                "department":            None,
                "job_title":             None,
                "project":               None,
                "language_preference":   None,
                "employment_start_date": None,
                "employment_end_date":   None,
                "is_active":             True,
                "created_at":            self._now,
                "updated_at":            self._now,
                "deleted_at":            None,
            }
        return self._employees[key]

    def employee_id(self, full_name: str, building_name: str) -> str:
        return self.get_or_create_employee(full_name, building_name)["employee_id"]

    # ── Date helper ──────────────────────────────────────────────────────────

    def resolve_date(self, day_name: str) -> datetime.date:
        offsets = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6,
        }
        return self.week_start + datetime.timedelta(days=offsets.get(day_name, 0))

    # ── Export ───────────────────────────────────────────────────────────────

    def rows_organisations(self): return list(self._orgs.values())
    def rows_offices(self):       return list(self._offices.values())
    def rows_buildings(self):     return list(self._buildings.values())
    def rows_floors(self):        return list(self._floors.values())
    def rows_rooms(self):         return list(self._rooms.values())
    def rows_seats(self):         return list(self._seats.values())
    def rows_employees(self):     return list(self._employees.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4())

def _clean(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() != "nan" else None

def _is_day(val) -> bool:
    return _clean(val) in DAYS_OF_WEEK

def _is_number(val) -> bool:
    v = _clean(val)
    if v is None:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Shared record builders  (called by every parser)
# ---------------------------------------------------------------------------

def _make_booking(seat: dict, emp_id: str, emp_name: str,
                  booking_date: datetime.date, now: str) -> dict:
    """One SEAT_BOOKING row: one employee confirmed on one seat for one date."""
    return {
        "booking_id":           _uid(),
        "seat_id":              seat["seat_id"],
        "employee_id":          emp_id,
        "employee_name":        emp_name,           # denormalised for readability
        "building_id":          seat["building_id"],
        "booking_date":         booking_date.isoformat(),
        "status":               BookingStatus.CONFIRMED,
        "block_reservation_id": None,
        "cancelled_at":         None,
        "cancellation_reason":  None,
        "auto_released_at":     None,
        "created_at":           now,
        "updated_at":           now,
    }


def _make_occupancy_log(seat: dict, emp_id: str | None, emp_name: str | None,
                        occupancy_date: datetime.date, is_occupied: bool,
                        now: str, notes: str | None = None) -> dict:
    """One SEAT_OCCUPANCY_LOG row: the occupancy state of a seat on a given date."""
    return {
        "log_id":        _uid(),
        "seat_id":       seat["seat_id"],
        "seat_number":   seat["seat_number"],       # denormalised for readability
        "room_id":       seat["room_id"],
        "building_id":   seat["building_id"],
        "employee_id":   emp_id,
        "employee_name": emp_name,                  # denormalised for readability
        "occupancy_date": occupancy_date.isoformat(),
        "is_occupied":   is_occupied,
        "is_remote_day": False,                     # remote logic excluded
        "notes":         notes,
        "created_at":    now,
    }


def _make_work_schedule(emp_id: str, emp_name: str, seat: dict,
                        day_enum: str, booking_date: datetime.date,
                        now: str) -> dict:
    """
    One WORK_SCHEDULE row: employee is assigned to this seat on this day of week.
    effective_from / effective_to both set to the booking date for now;
    collapse to a single weekly schedule row after HR enrichment.
    """
    return {
        "schedule_id":   _uid(),
        "employee_id":   emp_id,
        "employee_name": emp_name,
        "seat_id":       seat["seat_id"],
        "seat_number":   seat["seat_number"],
        "building_id":   seat["building_id"],
        "day_of_week":   day_enum,
        "is_remote":     False,
        "effective_from": booking_date.isoformat(),
        "effective_to":   booking_date.isoformat(),
        "created_at":    now,
        "updated_at":    now,
    }


# ---------------------------------------------------------------------------
# Parser: Kumasi
# ---------------------------------------------------------------------------
# Layout:
#   Row 0  : <ROOM_NAME>  <total_seats>  FLOOR LEADS  <lead1>  <lead2>  …
#   Row N  : "# of unoccupied Seats"  "Vacant Seat/Day"  "Seat 1" "Seat 2" …
#   Row N+1…: <Day>  <vacant_count>  <name|blank> …

def parse_kumasi(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    BUILDING     = "Kumasi Office"
    FLOOR_NUMBER = 1        # Kumasi CSV has no explicit floor info
    SKIP         = {"# of unoccupied Seats"}
    now          = reg._now

    seat_bookings  = []
    occupancy_log  = []
    work_schedules = []
    floor_leads    = []

    # Identify room-header rows: non-empty col-0, not a day, not a number
    room_starts = []
    for i, row in df.iterrows():
        c0 = _clean(row[0])
        if c0 and c0 not in DAYS_OF_WEEK and c0 not in SKIP and not _is_number(c0):
            room_starts.append(i)

    for idx, start in enumerate(room_starts):
        end   = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        room_row    = block.iloc[0]
        room_name   = _clean(room_row[0]) or f"Room_{start}"
        total_seats_raw = _clean(room_row[1])
        total_seats = int(total_seats_raw) if _is_number(total_seats_raw) else None

        # ── Floor leads ──────────────────────────────────────────────────────
        # Appear after the "FLOOR LEADS" marker in the room header row
        lead_names: list[str] = []
        reading_leads = False
        for v in room_row[2:]:
            cv = _clean(v)
            if cv == "FLOOR LEADS":
                reading_leads = True
                continue
            if reading_leads and cv:
                lead_names.append(cv)

        room = reg.get_or_create_room(BUILDING, FLOOR_NUMBER, room_name, total_seats)

        for lead_name in lead_names:
            floor_leads.append({
                "lead_id":      _uid(),
                "employee_id":  reg.employee_id(lead_name, BUILDING),
                "employee_name": lead_name,
                "room_id":      room["room_id"],
                "room_name":    room_name,
                "building_id":  room["building_id"],
                "assigned_from": None,   # not in source data
                "assigned_to":   None,
                "created_at":   now,
                "updated_at":   now,
            })

        # ── Find column-header row ("Seat 1", "Seat 2", …) ──────────────────
        header_idx = None
        for r in range(1, len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "# of unoccupied Seats" or c1 == "# of unoccupied Seats":
                header_idx = r
                break
        if header_idx is None:
            continue

        seat_cols: dict[int, str] = {
            ci: _clean(val)
            for ci, val in enumerate(block.iloc[header_idx])
            if _clean(val) and _clean(val).startswith("Seat")
        }

        # ── Day rows ─────────────────────────────────────────────────────────
        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue

            booking_date = reg.resolve_date(day)
            day_enum     = DAY_ENUM[day]

            for ci, seat_number in seat_cols.items():
                name    = _clean(row[ci]) if ci < len(row) else None
                is_occ  = bool(name)
                seat    = reg.get_or_create_seat(room, seat_number)

                # SEAT_OCCUPANCY_LOG – one row per physical seat per day
                occupancy_log.append(
                    _make_occupancy_log(seat, 
                                        reg.employee_id(name, BUILDING) if name else None,
                                        name, booking_date, is_occ, now))

                if name:
                    emp_id = reg.employee_id(name, BUILDING)

                    # SEAT_BOOKING – confirmed booking for this employee+seat+date
                    seat_bookings.append(
                        _make_booking(seat, emp_id, name, booking_date, now))

                    # WORK_SCHEDULE – employee's recurring seat on this day-of-week
                    work_schedules.append(
                        _make_work_schedule(emp_id, name, seat, day_enum, booking_date, now))

    return {
        "seat_bookings":  seat_bookings,
        "occupancy_log":  occupancy_log,
        "work_schedules": work_schedules,
        "floor_leads":    floor_leads,
    }


# ---------------------------------------------------------------------------
# Parser: Takoradi
# ---------------------------------------------------------------------------
# Layout:
#   Row N  : col-0 empty, col-1 = <ROOM_NAME>
#   Row N+1: "Days" | empty  "Daily Empty Seats" | "Vacant Seat/Day"  "Seat X" …
#   Row N+2…: <Day>  <vacant_count>  <name> …
# A room may span multiple seat-column sub-blocks (col-groups).

def parse_takoradi(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    BUILDING     = "Takoradi Office"
    FLOOR_NUMBER = 1
    SKIP_C1      = {"Daily Empty Seats", "Vacant Seat/Day"}
    now          = reg._now

    seat_bookings  = []
    occupancy_log  = []
    work_schedules = []

    def _is_room_header(row):
        c0 = _clean(row[0])
        c1 = _clean(row[1]) if len(row) > 1 else None
        return (c0 is None and c1 and c1 not in DAYS_OF_WEEK
                and not c1.startswith("Seat") and not _is_number(c1)
                and c1 not in SKIP_C1)

    room_starts = [i for i, row in df.iterrows() if _is_room_header(row)]

    for idx, start in enumerate(room_starts):
        end       = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block     = df.iloc[start:end].reset_index(drop=True)
        room_name = _clean(block.iloc[0, 1]) or f"Room_{start}"
        room      = reg.get_or_create_room(BUILDING, FLOOR_NUMBER, room_name, None)

        # Sub-header rows that start a new seat-column group within the room
        sub_headers = []
        for r in range(len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "Days" or (c0 is None and c1 in SKIP_C1):
                sub_headers.append(r)

        for si, hr in enumerate(sub_headers):
            end_sub  = sub_headers[si + 1] if si + 1 < len(sub_headers) else len(block)
            sub      = block.iloc[hr:end_sub].reset_index(drop=True)
            seat_cols: dict[int, str] = {
                ci: _clean(val)
                for ci, val in enumerate(sub.iloc[0])
                if _clean(val) and _clean(val).startswith("Seat")
            }
            if not seat_cols:
                continue

            for r in range(1, len(sub)):
                row = sub.iloc[r]
                day = _clean(row[0])
                if not _is_day(day):
                    continue

                booking_date = reg.resolve_date(day)
                day_enum     = DAY_ENUM[day]

                for ci, seat_number in seat_cols.items():
                    name   = _clean(row[ci]) if ci < len(row) else None
                    is_occ = bool(name)
                    seat   = reg.get_or_create_seat(room, seat_number)

                    occupancy_log.append(
                        _make_occupancy_log(seat,
                                            reg.employee_id(name, BUILDING) if name else None,
                                            name, booking_date, is_occ, now))

                    if name:
                        emp_id = reg.employee_id(name, BUILDING)
                        seat_bookings.append(
                            _make_booking(seat, emp_id, name, booking_date, now))
                        work_schedules.append(
                            _make_work_schedule(emp_id, name, seat, day_enum, booking_date, now))

    return {
        "seat_bookings":  seat_bookings,
        "occupancy_log":  occupancy_log,
        "work_schedules": work_schedules,
        "floor_leads":    [],
    }


# ---------------------------------------------------------------------------
# Parser: Accra
# ---------------------------------------------------------------------------
# Layout (semicolon-delimited):
#   Row N  : "BLOCK X FLOOR Y"      <- room header, encodes floor number
#   Row N+1: "Day"  "Seat 1" …      <- column header
#   Row N+2…: <Day>  <name|"Book before Use"> …

def parse_accra(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, sep=";", header=None, dtype=str)

    BUILDING = "Accra Office"
    BLOCK_RE = re.compile(r"BLOCK\s+(\w+)\s+FLOOR\s+(\d+)", re.I)
    now      = reg._now

    seat_bookings  = []
    occupancy_log  = []
    work_schedules = []

    room_starts = [
        i for i, row in df.iterrows()
        if _clean(row[0]) and BLOCK_RE.match(_clean(row[0]))
    ]

    for idx, start in enumerate(room_starts):
        end        = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block      = df.iloc[start:end].reset_index(drop=True)
        raw_header = _clean(block.iloc[0, 0])
        m          = BLOCK_RE.match(raw_header)
        floor_num  = int(m.group(2)) if m else 1
        room_name  = raw_header
        room       = reg.get_or_create_room(BUILDING, floor_num, room_name, None)

        # Find the "Day" column-header row
        header_idx = None
        for r in range(1, min(5, len(block))):
            if _clean(block.iloc[r, 0]) == "Day":
                header_idx = r
                break
        if header_idx is None:
            continue

        seat_cols: dict[int, str] = {
            ci: _clean(val)
            for ci, val in enumerate(block.iloc[header_idx])
            if _clean(val) and _clean(val).lower().startswith("seat")
        }

        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue

            booking_date = reg.resolve_date(day)
            day_enum     = DAY_ENUM[day]

            for ci, seat_number in seat_cols.items():
                raw         = _clean(row[ci]) if ci < len(row) else None
                is_hot_desk = bool(raw and "book" in raw.lower())
                name        = raw if raw and not is_hot_desk else None
                is_occ      = bool(name)
                seat        = reg.get_or_create_seat(room, seat_number, is_hot_desk=is_hot_desk)

                occupancy_log.append(
                    _make_occupancy_log(seat,
                                        reg.employee_id(name, BUILDING) if name else None,
                                        name, booking_date, is_occ, now,
                                        notes="hot desk – bookable" if is_hot_desk else None))

                if name:
                    emp_id = reg.employee_id(name, BUILDING)
                    seat_bookings.append(
                        _make_booking(seat, emp_id, name, booking_date, now))
                    work_schedules.append(
                        _make_work_schedule(emp_id, name, seat, day_enum, booking_date, now))

    return {
        "seat_bookings":  seat_bookings,
        "occupancy_log":  occupancy_log,
        "work_schedules": work_schedules,
        "floor_leads":    [],
    }


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _merge(*results: dict) -> dict:
    out: dict[str, list] = {
        "seat_bookings": [], "occupancy_log": [],
        "work_schedules": [], "floor_leads": [],
    }
    for r in results:
        for key in out:
            out[key].extend(r.get(key, []))
    return out


def _dedup_work_schedules(rows: list[dict]) -> list[dict]:
    """
    Collapse to one WORK_SCHEDULE per (employee_id, seat_id, day_of_week).
    Keep the earliest effective_from as the anchor date.
    """
    index: dict[tuple, dict] = {}
    for row in rows:
        key = (row["employee_id"], row["seat_id"], row["day_of_week"])
        if key not in index:
            index[key] = row
        else:
            # Extend effective range if this date is earlier
            if row["effective_from"] < index[key]["effective_from"]:
                index[key]["effective_from"] = row["effective_from"]
    return list(index.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract OMS seating data from SharePoint CSVs")
    ap.add_argument("--kumasi",     default="/mnt/user-data/uploads/Kumasi_Seating_Arrangement_In_office___Remote_.csv")
    ap.add_argument("--takoradi",   default="/mnt/user-data/uploads/New_Seating_Seating_Arrangment_-_Update__________Takoradi.csv")
    ap.add_argument("--accra",      default="/mnt/user-data/uploads/Arrangements_Final-_Accra.csv")
    ap.add_argument("--out",        default="/mnt/user-data/outputs")
    ap.add_argument("--week-start", default=None,
                    help="ISO date of the Monday for this sheet, e.g. 2025-01-13")
    args = ap.parse_args()

    if args.week_start:
        week_start = datetime.date.fromisoformat(args.week_start)
    else:
        today      = datetime.date.today()
        week_start = today - datetime.timedelta(days=today.weekday())
        print(f"[info] --week-start not set. Using {week_start} (this Monday).")
        print(f"       Pass --week-start YYYY-MM-DD to anchor to a specific week.\n")

    reg = Registry(week_start)

    print("Parsing Kumasi …")
    kumasi = parse_kumasi(args.kumasi, reg)

    print("Parsing Takoradi …")
    takoradi = parse_takoradi(args.takoradi, reg)

    print("Parsing Accra …")
    accra = parse_accra(args.accra, reg)

    print("Merging …")
    merged = _merge(kumasi, takoradi, accra)
    merged["work_schedules"] = _dedup_work_schedules(merged["work_schedules"])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    tables = {
        # ── Dimension / reference ──────────────────────────────────────────
        "organisations.csv":      reg.rows_organisations(),
        "offices.csv":            reg.rows_offices(),
        "buildings.csv":          reg.rows_buildings(),
        "floors.csv":             reg.rows_floors(),
        "rooms.csv":              reg.rows_rooms(),
        "seats.csv":              reg.rows_seats(),
        "employees_stub.csv":     reg.rows_employees(),
        # ── Transactional ─────────────────────────────────────────────────
        "seat_bookings.csv":      merged["seat_bookings"],
        "seat_occupancy_log.csv": merged["occupancy_log"],
        "work_schedules.csv":     merged["work_schedules"],
        "floor_leads.csv":        merged["floor_leads"],
    }

    print()
    for filename, rows in tables.items():
        if not rows:
            print(f"  [skip]  {filename}")
            continue
        df = pd.DataFrame(rows)
        df.to_csv(out_dir / filename, index=False)
        print(f"  ✓  {filename:<30s}  {len(df):>5,} rows")

    print(f"\nWeek anchor : {week_start} (Mon) → {week_start + datetime.timedelta(days=4)} (Fri)")
    print(f"Output dir  : {out_dir}")
    print("\nNext steps before DB load:")
    print("  1. Enrich employees_stub.csv with employee_code, email, job_title from HR.")
    print("  2. Back-fill seats.assigned_employee_id once employee UUIDs are confirmed.")
    print("  3. Add assigned_from / assigned_to dates to floor_leads.csv.")


if __name__ == "__main__":
    main()
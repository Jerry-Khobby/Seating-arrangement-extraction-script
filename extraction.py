"""
extract_seating_v2.py
=====================
ETL script to parse seating-arrangement CSVs exported from SharePoint
for the Ghana offices (Kumasi, Takoradi, Accra) and emit a clean,
normalised dataset aligned with the OMS data model.

Output CSVs (one per schema table):
  Dimension / reference tables (seed data):
    1.  countries.csv             – COUNTRY
    2.  cities.csv                – CITY
    3.  buildings.csv             – OFFICE_BUILDING
    4.  floors.csv                – FLOOR
    5.  rooms.csv                 – ROOM
    6.  seats.csv                 – SEAT

  Transactional / log tables:
    7.  employees_stub.csv        – EMPLOYEE  (name-only stub; enrich with HR feed)
    8.  seat_bookings.csv         – SEAT_BOOKING
    9.  seat_occupancy_log.csv    – SEAT_OCCUPANCY_LOG
    10. work_schedules.csv        – WORK_SCHEDULE
    11. recurring_remote.csv      – RECURRING_REMOTE_SCHEDULE
    12. floor_leads.csv           – FLOOR_LEAD

  Convenience / reporting:
    13. remote_true.csv           – employees confirmed remote, keyed by (employee_name, building, day_of_week)

Usage:
    python extract_seating_v2.py
    python extract_seating_v2.py --week-start 2025-01-13   # anchor Mon date
    python extract_seating_v2.py --kumasi path/to/file.csv ...
"""

import re
import uuid
import argparse
import datetime
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants – enum values used in the schema
# ---------------------------------------------------------------------------

class RoomType:
    OPEN_PLAN  = "open_plan"
    PRIVATE    = "private"
    HOT_DESK   = "hot_desk"
    MEETING    = "meeting"

class SeatType:
    ASSIGNED   = "assigned"
    HOT_DESK   = "hot_desk"
    FLEX       = "flex"

class SeatStatus:
    AVAILABLE  = "available"
    OCCUPIED   = "occupied"
    RESERVED   = "reserved"
    INACTIVE   = "inactive"

class BookingStatus:
    CONFIRMED  = "confirmed"
    CANCELLED  = "cancelled"
    AUTO_RELEASED = "auto_released"

class RemoteStatus:
    PENDING    = "pending"
    APPROVED   = "approved"

class ScheduleStatus:
    ACTIVE     = "active"
    INACTIVE   = "inactive"

# Days of week enum values (matches WORK_SCHEDULE.day_of_week)
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
# Seed registry  (built once, shared across all parsers)
# ---------------------------------------------------------------------------

class Registry:
    """
    Holds UUIDs for all dimension rows so every parser can look them up
    by human-readable name and get back a stable UUID.
    """

    def __init__(self, week_start: datetime.date):
        self.week_start = week_start          # Monday anchor for date resolution

        # dimension stores  key -> {id, ...row fields}
        self._countries: dict[str, dict] = {}
        self._cities:    dict[str, dict] = {}
        self._buildings: dict[str, dict] = {}
        self._floors:    dict[tuple, dict] = {}   # (building_id, floor_number)
        self._rooms:     dict[tuple, dict] = {}   # (building_id, floor_id, room_name)
        self._seats:     dict[tuple, dict] = {}   # (room_id, seat_number)
        self._employees: dict[str, dict]   = {}   # lower(full_name) -> row

        # seed Ghana / offices upfront
        self._seed()

    def _seed(self):
        gh = self.get_or_create_country("Ghana", "GH", "Africa/Accra")
        for city_name, building_name, total_floors in [
            ("Kumasi",   "Kumasi Office",   3),
            ("Takoradi", "Takoradi Office", 3),
            ("Accra",    "Accra Office",    5),
        ]:
            city = self.get_or_create_city(city_name, gh["country_id"])
            self.get_or_create_building(building_name, city["city_id"], total_floors)

    # ── Country ──────────────────────────────────────────────────────────────

    def get_or_create_country(self, name: str, code: str, tz: str) -> dict:
        if name not in self._countries:
            self._countries[name] = {
                "country_id":   _uid(),
                "country_name": name,
                "country_code": code,
                "timezone":     tz,
            }
        return self._countries[name]

    # ── City ─────────────────────────────────────────────────────────────────

    def get_or_create_city(self, name: str, country_id: str) -> dict:
        if name not in self._cities:
            self._cities[name] = {
                "city_id":    _uid(),
                "country_id": country_id,
                "city_name":  name,
            }
        return self._cities[name]

    # ── Building ─────────────────────────────────────────────────────────────

    def get_or_create_building(self, name: str, city_id: str, total_floors: int = 1) -> dict:
        if name not in self._buildings:
            self._buildings[name] = {
                "building_id":  _uid(),
                "city_id":      city_id,
                "building_name": name,
                "address":      None,
                "total_floors": total_floors,
            }
        return self._buildings[name]

    def building_id(self, building_name: str) -> str:
        return self._buildings[building_name]["building_id"]

    # ── Floor ────────────────────────────────────────────────────────────────

    def get_or_create_floor(self, building_name: str, floor_number: int,
                            floor_name: str | None = None) -> dict:
        bid = self.building_id(building_name)
        key = (bid, floor_number)
        if key not in self._floors:
            self._floors[key] = {
                "floor_id":      _uid(),
                "building_id":   bid,
                "floor_name":    floor_name or f"Floor {floor_number}",
                "floor_number":  floor_number,
                "is_active":     True,
            }
        return self._floors[key]

    def floor_id(self, building_name: str, floor_number: int) -> str:
        return self.get_or_create_floor(building_name, floor_number)["floor_id"]

    # ── Room ─────────────────────────────────────────────────────────────────

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
            }
        return self._rooms[key]

    # ── Seat ─────────────────────────────────────────────────────────────────

    def get_or_create_seat(self, room: dict, seat_number: str,
                           is_hot_desk: bool = False) -> dict:
        key = (room["room_id"], seat_number)
        if key not in self._seats:
            self._seats[key] = {
                "seat_id":          _uid(),
                "room_id":          room["room_id"],
                "floor_id":         room["floor_id"],
                "building_id":      room["building_id"],
                "seat_number":      seat_number,
                "seat_type":        SeatType.HOT_DESK if is_hot_desk else SeatType.ASSIGNED,
                "status":           SeatStatus.AVAILABLE,
                "assigned_user_id": None,
                "x_position":       None,
                "y_position":       None,
            }
        return self._seats[key]

    # ── Employee ─────────────────────────────────────────────────────────────

    def get_or_create_employee(self, full_name: str, building_name: str) -> dict:
        key = full_name.strip().lower()
        if key not in self._employees:
            parts = full_name.strip().split(None, 1)
            self._employees[key] = {
                "employee_id":         _uid(),
                "employee_code":       None,          # requires HR feed
                "sso_user_id":         None,
                "first_name":          parts[0] if parts else full_name,
                "last_name":           parts[1] if len(parts) > 1 else None,
                "email":               None,
                "primary_building_id": self.building_id(building_name),
                "manager_id":          None,
                "employee_type":       None,
                "department":          None,
                "job_title":           None,
                "project":             None,
                "employment_start_date": None,
                "employment_end_date": None,
                "is_active":           True,
                "deleted_at":          None,
                # convenience – used during extraction, not a schema field
                "_full_name":          full_name.strip(),
            }
        return self._employees[key]

    def employee_id(self, full_name: str, building_name: str) -> str:
        return self.get_or_create_employee(full_name, building_name)["employee_id"]

    # ── Date resolution ───────────────────────────────────────────────────────

    def resolve_date(self, day_name: str) -> datetime.date:
        """Map a weekday name to a real ISO date relative to week_start (Mon)."""
        offsets = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2,
            "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6,
        }
        return self.week_start + datetime.timedelta(days=offsets.get(day_name, 0))

    # ── Export helpers ────────────────────────────────────────────────────────

    def rows_countries(self):  return list(self._countries.values())
    def rows_cities(self):     return list(self._cities.values())
    def rows_buildings(self):  return list(self._buildings.values())
    def rows_floors(self):     return list(self._floors.values())
    def rows_rooms(self):      return list(self._rooms.values())
    def rows_seats(self):      return list(self._seats.values())

    def rows_employees(self):
        # strip internal convenience key before export
        return [{k: v for k, v in row.items() if not k.startswith("_")}
                for row in self._employees.values()]


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
# Parser: Kumasi
# ---------------------------------------------------------------------------

def parse_kumasi(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    BUILDING     = "Kumasi Office"
    FLOOR_NUMBER = 1          # Kumasi CSV has no floor info – default to floor 1
    SKIP_FIRST   = {"# of unoccupied Seats"}

    seat_bookings     = []
    occupancy_log     = []
    work_schedules    = []
    recurring_remote  = []
    floor_leads_rows  = []

    # Find room-header rows
    room_starts = []
    for i, row in df.iterrows():
        c0 = _clean(row[0])
        if (c0 and c0 not in DAYS_OF_WEEK and c0 not in SKIP_FIRST
                and not _is_number(c0)):
            room_starts.append(i)

    for idx, start in enumerate(room_starts):
        end   = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        room_row    = block.iloc[0]
        room_name   = _clean(room_row[0]) or f"Room_{start}"
        total_seats_raw = _clean(room_row[1])
        total_seats = int(total_seats_raw) if _is_number(total_seats_raw) else None

        # Floor leads (come after "FLOOR LEADS" marker in the header row)
        lead_names: list[str] = []
        fl_start = False
        for v in room_row[2:]:
            cv = _clean(v)
            if cv == "FLOOR LEADS":
                fl_start = True
                continue
            if fl_start and cv:
                lead_names.append(cv)

        room = reg.get_or_create_room(BUILDING, FLOOR_NUMBER, room_name, total_seats)

        # Emit FLOOR_LEAD rows
        for lead_name in lead_names:
            emp_id = reg.employee_id(lead_name, BUILDING)
            floor_leads_rows.append({
                "lead_id":      _uid(),
                "employee_id":  emp_id,
                "room_id":      room["room_id"],
                "assigned_from": None,   # not available in source
                "assigned_to":   None,
            })

        # Find the column-header row that contains "Seat X" labels
        header_idx = None
        for r in range(1, len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "# of unoccupied Seats" or c1 == "# of unoccupied Seats":
                header_idx = r
                break
        if header_idx is None:
            continue

        header_row = block.iloc[header_idx]
        seat_cols: dict[int, str] = {}
        for ci, val in enumerate(header_row):
            cv = _clean(val)
            if cv and cv.startswith("Seat"):
                seat_cols[ci] = cv

        # Union of all employee names ever appearing in this room (to detect remote)
        all_employees: set[str] = set()
        day_rows: dict[str, object] = {}
        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue
            day_rows[day] = row
            for ci in seat_cols:
                name = _clean(row[ci]) if ci < len(row) else None
                if name:
                    all_employees.add(name)

        for day, row in day_rows.items():
            booking_date = reg.resolve_date(day)
            day_enum     = DAY_ENUM[day]
            present: set[str] = set()

            for ci, seat_number in seat_cols.items():
                name      = _clean(row[ci]) if ci < len(row) else None
                is_occ    = bool(name)
                seat      = reg.get_or_create_seat(room, seat_number, is_hot_desk=False)
                seat_id   = seat["seat_id"]

                # SEAT_BOOKING – one confirmed booking per occupied seat per day
                if name:
                    emp_id = reg.employee_id(name, BUILDING)
                    seat_bookings.append({
                        "booking_id":          _uid(),
                        "seat_id":             seat_id,
                        "employee_id":         emp_id,
                        "employee_name":       name,
                        "building_id":         room["building_id"],
                        "booking_date":        booking_date.isoformat(),
                        "status":              BookingStatus.CONFIRMED,
                        "block_reservation_id": None,
                        "cancelled_at":        None,
                        "cancellation_reason": None,
                        "auto_released_at":    None,
                    })
                    present.add(name)

                # SEAT_OCCUPANCY_LOG – one row per seat per day
                occupancy_log.append({
                    "log_id":        _uid(),
                    "seat_id":       seat_id,
                    "seat_number":   seat_number,
                    "employee_id":   reg.employee_id(name, BUILDING) if name else None,
                    "employee_name": name,
                    "room_id":       room["room_id"],
                    "room_name":     room_name,
                    "building_id":   room["building_id"],
                    "building_name": BUILDING,
                    "occupancy_date": booking_date.isoformat(),
                    "day_of_week":   day_enum,
                    "is_occupied":   is_occ,
                    "is_remote_day": False,   # updated below for absent employees
                    "notes":         None,
                })

            # WORK_SCHEDULE + RECURRING_REMOTE_SCHEDULE for absent employees
            for emp_name in all_employees:
                emp_id = reg.employee_id(emp_name, BUILDING)
                is_remote = emp_name not in present

                # WORK_SCHEDULE row (one per employee × day_of_week)
                work_schedules.append({
                    "schedule_id":   _uid(),
                    "employee_id":   emp_id,
                    "employee_name": emp_name,
                    "seat_id":       None,      # seat-level assignment requires enrichment
                    "building_id":   room["building_id"],
                    "day_of_week":   day_enum,
                    "is_remote":     is_remote,
                    "effective_from": booking_date.isoformat(),
                    "effective_to":   booking_date.isoformat(),
                })

                if is_remote:
                    recurring_remote.append({
                        "schedule_id":   _uid(),
                        "employee_id":   emp_id,
                        "employee_name": emp_name,
                        "building_id":   room["building_id"],
                        "building_name": BUILDING,
                        "day_of_week":   day_enum,
                        "effective_from": booking_date.isoformat(),
                        "effective_to":   booking_date.isoformat(),
                        "status":        RemoteStatus.APPROVED,
                        "reviewed_by":   None,
                        "reviewed_at":   None,
                    })

            # Back-patch is_remote_day on occupancy rows for employees absent today
            for log_row in occupancy_log:
                if (log_row["room_id"] == room["room_id"]
                        and log_row["occupancy_date"] == booking_date.isoformat()
                        and log_row["employee_name"] is None):
                    # Unoccupied seat – check if the seat's usual owner is remote
                    pass  # handled via WORK_SCHEDULE; seat row stays is_remote_day=False

        # Emit a SEAT_OCCUPANCY_LOG row for each remote employee (no seat assigned that day)
        # This captures "employee was expected in office but was remote" at room level
        for day, row in day_rows.items():
            booking_date = reg.resolve_date(day)
            day_enum     = DAY_ENUM[day]
            present_this_day = {
                _clean(row[ci]) for ci in seat_cols
                if ci < len(row) and _clean(row[ci])
            }
            for emp_name in all_employees:
                if emp_name not in present_this_day:
                    occupancy_log.append({
                        "log_id":        _uid(),
                        "seat_id":       None,
                        "seat_number":   None,
                        "employee_id":   reg.employee_id(emp_name, BUILDING),
                        "employee_name": emp_name,
                        "room_id":       room["room_id"],
                        "room_name":     room_name,
                        "building_id":   room["building_id"],
                        "building_name": BUILDING,
                        "occupancy_date": booking_date.isoformat(),
                        "day_of_week":   day_enum,
                        "is_occupied":   False,
                        "is_remote_day": True,
                        "notes":         "inferred remote – absent from seating sheet",
                    })

    return {
        "seat_bookings":    seat_bookings,
        "occupancy_log":    occupancy_log,
        "work_schedules":   work_schedules,
        "recurring_remote": recurring_remote,
        "floor_leads":      floor_leads_rows,
    }


# ---------------------------------------------------------------------------
# Parser: Takoradi
# ---------------------------------------------------------------------------

def parse_takoradi(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    BUILDING     = "Takoradi Office"
    FLOOR_NUMBER = 1
    SKIP_C1      = {"Daily Empty Seats", "Vacant Seat/Day"}

    seat_bookings    = []
    occupancy_log    = []
    work_schedules   = []
    recurring_remote = []

    def _is_room_header(row):
        c0 = _clean(row[0])
        c1 = _clean(row[1]) if len(row) > 1 else None
        return (c0 is None and c1 and c1 not in DAYS_OF_WEEK
                and not c1.startswith("Seat") and not _is_number(c1)
                and c1 not in SKIP_C1)

    room_starts = [i for i, row in df.iterrows() if _is_room_header(row)]

    for idx, start in enumerate(room_starts):
        end   = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        room_name = _clean(block.iloc[0, 1]) or f"Room_{start}"
        room = reg.get_or_create_room(BUILDING, FLOOR_NUMBER, room_name, None)

        # Find "Days" sub-header rows that start seat-column sections
        days_rows_idx = []
        for r in range(len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "Days" or (c0 is None and c1 in SKIP_C1):
                days_rows_idx.append(r)

        for si, hr in enumerate(days_rows_idx):
            end_sub = days_rows_idx[si + 1] if si + 1 < len(days_rows_idx) else len(block)
            sub     = block.iloc[hr:end_sub].reset_index(drop=True)

            header   = sub.iloc[0]
            seat_cols: dict[int, str] = {}
            for ci, val in enumerate(header):
                cv = _clean(val)
                if cv and cv.startswith("Seat"):
                    seat_cols[ci] = cv

            if not seat_cols:
                continue

            all_employees: set[str] = set()
            day_rows: dict[str, object] = {}
            for r in range(1, len(sub)):
                row = sub.iloc[r]
                day = _clean(row[0])
                if not _is_day(day):
                    continue
                day_rows[day] = row
                for ci in seat_cols:
                    name = _clean(row[ci]) if ci < len(row) else None
                    if name:
                        all_employees.add(name)

            for day, row in day_rows.items():
                booking_date = reg.resolve_date(day)
                day_enum     = DAY_ENUM[day]
                present: set[str] = set()

                for ci, seat_number in seat_cols.items():
                    name    = _clean(row[ci]) if ci < len(row) else None
                    is_occ  = bool(name)
                    seat    = reg.get_or_create_seat(room, seat_number)
                    seat_id = seat["seat_id"]

                    if name:
                        emp_id = reg.employee_id(name, BUILDING)
                        seat_bookings.append({
                            "booking_id":          _uid(),
                            "seat_id":             seat_id,
                            "employee_id":         emp_id,
                            "employee_name":       name,
                            "building_id":         room["building_id"],
                            "booking_date":        booking_date.isoformat(),
                            "status":              BookingStatus.CONFIRMED,
                            "block_reservation_id": None,
                            "cancelled_at":        None,
                            "cancellation_reason": None,
                            "auto_released_at":    None,
                        })
                        present.add(name)

                    occupancy_log.append({
                        "log_id":        _uid(),
                        "seat_id":       seat_id,
                        "seat_number":   seat_number,
                        "employee_id":   reg.employee_id(name, BUILDING) if name else None,
                        "employee_name": name,
                        "room_id":       room["room_id"],
                        "room_name":     room_name,
                        "building_id":   room["building_id"],
                        "building_name": BUILDING,
                        "occupancy_date": booking_date.isoformat(),
                        "day_of_week":   day_enum,
                        "is_occupied":   is_occ,
                        "is_remote_day": False,
                        "notes":         None,
                    })

                for emp_name in all_employees:
                    emp_id    = reg.employee_id(emp_name, BUILDING)
                    is_remote = emp_name not in present

                    work_schedules.append({
                        "schedule_id":   _uid(),
                        "employee_id":   emp_id,
                        "employee_name": emp_name,
                        "seat_id":       None,
                        "building_id":   room["building_id"],
                        "day_of_week":   day_enum,
                        "is_remote":     is_remote,
                        "effective_from": booking_date.isoformat(),
                        "effective_to":   booking_date.isoformat(),
                    })

                    if is_remote:
                        recurring_remote.append({
                            "schedule_id":   _uid(),
                            "employee_id":   emp_id,
                            "employee_name": emp_name,
                            "building_id":   room["building_id"],
                            "building_name": BUILDING,
                            "day_of_week":   day_enum,
                            "effective_from": booking_date.isoformat(),
                            "effective_to":   booking_date.isoformat(),
                            "status":        RemoteStatus.APPROVED,
                            "reviewed_by":   None,
                            "reviewed_at":   None,
                        })
                        occupancy_log.append({
                            "log_id":        _uid(),
                            "seat_id":       None,
                            "seat_number":   None,
                            "employee_id":   emp_id,
                            "employee_name": emp_name,
                            "room_id":       room["room_id"],
                            "room_name":     room_name,
                            "building_id":   room["building_id"],
                            "building_name": BUILDING,
                            "occupancy_date": booking_date.isoformat(),
                            "day_of_week":   day_enum,
                            "is_occupied":   False,
                            "is_remote_day": True,
                            "notes":         "inferred remote – absent from seating sheet",
                        })

    return {
        "seat_bookings":    seat_bookings,
        "occupancy_log":    occupancy_log,
        "work_schedules":   work_schedules,
        "recurring_remote": recurring_remote,
        "floor_leads":      [],
    }


# ---------------------------------------------------------------------------
# Parser: Accra
# ---------------------------------------------------------------------------

def parse_accra(path: str, reg: Registry) -> dict:
    df = pd.read_csv(path, sep=";", header=None, dtype=str)

    BUILDING = "Accra Office"
    BLOCK_RE = re.compile(r"BLOCK\s+(\w+)\s+FLOOR\s+(\d+)", re.I)

    seat_bookings    = []
    occupancy_log    = []
    work_schedules   = []
    recurring_remote = []

    def _is_room_header(row):
        c0 = _clean(row[0])
        return bool(c0 and BLOCK_RE.match(c0))

    room_starts = [i for i, row in df.iterrows() if _is_room_header(row)]

    for idx, start in enumerate(room_starts):
        end   = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        raw_header = _clean(block.iloc[0, 0])
        m = BLOCK_RE.match(raw_header)
        floor_number = int(m.group(2)) if m else 1
        room_name    = raw_header
        room         = reg.get_or_create_room(BUILDING, floor_number, room_name, None)

        # Find "Day" header row
        header_idx = None
        for r in range(1, min(5, len(block))):
            if _clean(block.iloc[r, 0]) == "Day":
                header_idx = r
                break
        if header_idx is None:
            continue

        header_row = block.iloc[header_idx]
        seat_cols: dict[int, str] = {}
        for ci, val in enumerate(header_row):
            cv = _clean(val)
            if cv and cv.lower().startswith("seat"):
                seat_cols[ci] = cv

        all_employees: set[str] = set()
        day_rows: dict[str, object] = {}
        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue
            day_rows[day] = row
            for ci in seat_cols:
                raw = _clean(row[ci]) if ci < len(row) else None
                if raw and "book" not in raw.lower():
                    all_employees.add(raw)

        for day, row in day_rows.items():
            booking_date = reg.resolve_date(day)
            day_enum     = DAY_ENUM[day]
            present: set[str] = set()

            for ci, seat_number in seat_cols.items():
                raw_cell   = _clean(row[ci]) if ci < len(row) else None
                is_hot_desk = bool(raw_cell and "book" in raw_cell.lower())
                name        = raw_cell if raw_cell and not is_hot_desk else None
                is_occ      = bool(name)
                seat        = reg.get_or_create_seat(room, seat_number, is_hot_desk=is_hot_desk)
                seat_id     = seat["seat_id"]

                if name:
                    emp_id = reg.employee_id(name, BUILDING)
                    seat_bookings.append({
                        "booking_id":          _uid(),
                        "seat_id":             seat_id,
                        "employee_id":         emp_id,
                        "employee_name":       name,
                        "building_id":         room["building_id"],
                        "booking_date":        booking_date.isoformat(),
                        "status":              BookingStatus.CONFIRMED,
                        "block_reservation_id": None,
                        "cancelled_at":        None,
                        "cancellation_reason": None,
                        "auto_released_at":    None,
                    })
                    present.add(name)

                occupancy_log.append({
                    "log_id":        _uid(),
                    "seat_id":       seat_id,
                    "seat_number":   seat_number,
                    "employee_id":   reg.employee_id(name, BUILDING) if name else None,
                    "employee_name": name,
                    "room_id":       room["room_id"],
                    "room_name":     room_name,
                    "building_id":   room["building_id"],
                    "building_name": BUILDING,
                    "occupancy_date": booking_date.isoformat(),
                    "day_of_week":   day_enum,
                    "is_occupied":   is_occ,
                    "is_remote_day": False,
                    "is_hot_desk":   is_hot_desk,
                    "notes":         "hot desk – bookable" if is_hot_desk else None,
                })

            for emp_name in all_employees:
                emp_id    = reg.employee_id(emp_name, BUILDING)
                is_remote = emp_name not in present

                work_schedules.append({
                    "schedule_id":   _uid(),
                    "employee_id":   emp_id,
                    "employee_name": emp_name,
                    "seat_id":       None,
                    "building_id":   room["building_id"],
                    "day_of_week":   day_enum,
                    "is_remote":     is_remote,
                    "effective_from": booking_date.isoformat(),
                    "effective_to":   booking_date.isoformat(),
                })

                if is_remote:
                    recurring_remote.append({
                        "schedule_id":   _uid(),
                        "employee_id":   emp_id,
                        "employee_name": emp_name,
                        "building_id":   room["building_id"],
                        "building_name": BUILDING,
                        "day_of_week":   day_enum,
                        "effective_from": booking_date.isoformat(),
                        "effective_to":   booking_date.isoformat(),
                        "status":        RemoteStatus.APPROVED,
                        "reviewed_by":   None,
                        "reviewed_at":   None,
                    })
                    occupancy_log.append({
                        "log_id":        _uid(),
                        "seat_id":       None,
                        "seat_number":   None,
                        "employee_id":   emp_id,
                        "employee_name": emp_name,
                        "room_id":       room["room_id"],
                        "room_name":     room_name,
                        "building_id":   room["building_id"],
                        "building_name": BUILDING,
                        "occupancy_date": booking_date.isoformat(),
                        "day_of_week":   day_enum,
                        "is_occupied":   False,
                        "is_remote_day": True,
                        "is_hot_desk":   False,
                        "notes":         "inferred remote – absent from seating sheet",
                    })

    return {
        "seat_bookings":    seat_bookings,
        "occupancy_log":    occupancy_log,
        "work_schedules":   work_schedules,
        "recurring_remote": recurring_remote,
        "floor_leads":      [],
    }


# ---------------------------------------------------------------------------
# Merge & deduplicate
# ---------------------------------------------------------------------------

def merge_results(*results: dict) -> dict:
    combined: dict[str, list] = {
        "seat_bookings": [], "occupancy_log": [],
        "work_schedules": [], "recurring_remote": [], "floor_leads": [],
    }
    for r in results:
        for key in combined:
            combined[key].extend(r.get(key, []))
    return combined


def deduplicate_work_schedules(rows: list[dict]) -> list[dict]:
    """
    Keep one WORK_SCHEDULE row per (employee_id, day_of_week).
    If any row says is_remote=False (in-office), that wins.
    """
    index: dict[tuple, dict] = {}
    for row in rows:
        key = (row["employee_id"], row["day_of_week"])
        if key not in index or not row["is_remote"]:
            index[key] = row
    return list(index.values())


def deduplicate_recurring_remote(rows: list[dict]) -> list[dict]:
    """
    Keep one RECURRING_REMOTE_SCHEDULE per (employee_id, day_of_week).
    """
    seen: set[tuple] = set()
    out: list[dict]  = []
    for row in rows:
        key = (row["employee_id"], row["day_of_week"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract OMS seating data from SharePoint CSVs")
    ap.add_argument("--kumasi",     default="/mnt/user-data/uploads/Kumasi_Seating_Arrangement_In_office___Remote_.csv")
    ap.add_argument("--takoradi",   default="/mnt/user-data/uploads/New_Seating_Seating_Arrangment_-_Update__________Takoradi.csv")
    ap.add_argument("--accra",      default="/mnt/user-data/uploads/Arrangements_Final-_Accra.csv")
    ap.add_argument("--out",        default="/mnt/user-data/outputs", help="Output directory")
    ap.add_argument("--week-start", default=None,
                    help="ISO date of the Monday for this seating sheet, e.g. 2025-01-13")
    args = ap.parse_args()

    # Resolve week anchor
    if args.week_start:
        week_start = datetime.date.fromisoformat(args.week_start)
    else:
        # Default to the most recent Monday relative to today
        today      = datetime.date.today()
        week_start = today - datetime.timedelta(days=today.weekday())
        print(f"[info] --week-start not provided. Defaulting to {week_start} (this week's Monday).")
        print(f"       Pass --week-start YYYY-MM-DD to anchor to a specific week.\n")

    reg = Registry(week_start)

    print("Parsing Kumasi …")
    kumasi   = parse_kumasi(args.kumasi, reg)

    print("Parsing Takoradi …")
    takoradi = parse_takoradi(args.takoradi, reg)

    print("Parsing Accra …")
    accra    = parse_accra(args.accra, reg)

    print("Merging …")
    merged = merge_results(kumasi, takoradi, accra)

    # Deduplicate schedule-like tables
    merged["work_schedules"]   = deduplicate_work_schedules(merged["work_schedules"])
    merged["recurring_remote"] = deduplicate_recurring_remote(merged["recurring_remote"])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Schema-aligned output tables ─────────────────────────────────────────

    schema_tables = {
        # Dimension / reference
        "countries.csv":          reg.rows_countries(),
        "cities.csv":             reg.rows_cities(),
        "buildings.csv":          reg.rows_buildings(),
        "floors.csv":             reg.rows_floors(),
        "rooms.csv":              reg.rows_rooms(),
        "seats.csv":              reg.rows_seats(),
        # Employee stubs (enrich with HR feed before loading)
        "employees_stub.csv":     reg.rows_employees(),
        # Transactional
        "seat_bookings.csv":      merged["seat_bookings"],
        "seat_occupancy_log.csv": merged["occupancy_log"],
        "work_schedules.csv":     merged["work_schedules"],
        "recurring_remote.csv":   merged["recurring_remote"],
        "floor_leads.csv":        merged["floor_leads"],
    }

    for filename, rows in schema_tables.items():
        if not rows:
            print(f"  [skip] {filename} – no data")
            continue
        df = pd.DataFrame(rows)
        path = out_dir / filename
        df.to_csv(path, index=False)
        print(f"  ✓  {filename:<30s}  {len(df):>5,} rows  → {path}")

    # ── remote_true.csv  (convenience – only confirmed remote employees) ──────
    remote_df = pd.DataFrame(merged["recurring_remote"])
    if not remote_df.empty:
        remote_true = (
            remote_df[["employee_name", "building_name", "day_of_week",
                        "effective_from", "employee_id", "building_id"]]
            .drop_duplicates(subset=["employee_id", "day_of_week"])
            .sort_values(["building_name", "day_of_week", "employee_name"])
            .reset_index(drop=True)
        )
        remote_true.to_csv(out_dir / "remote_true.csv", index=False)
        print(f"  ✓  {'remote_true.csv':<30s}  {len(remote_true):>5,} rows  → {out_dir / 'remote_true.csv'}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    all_counts = {
        **{k: len(v) for k, v in schema_tables.items()},
        "remote_true.csv": len(remote_df) if not remote_df.empty else 0,
    }
    for name, count in all_counts.items():
        print(f"  {name:<32s}: {count:>6,} records")

    if not remote_df.empty:
        by_office = (
            remote_df.groupby("building_name")["employee_id"]
            .nunique()
            .rename("unique_remote_employees")
        )
        print("\nUnique remote employees by office:")
        for office, n in by_office.items():
            print(f"  {office}: {n}")

    print(f"\nWeek anchored to: {week_start} (Mon) – {week_start + datetime.timedelta(days=4)} (Fri)")
    print("Done.\n")
    print("Next steps before DB load:")
    print("  1. Enrich employees_stub.csv with employee_code, email, job_title from your HR system.")
    print("  2. Update seats.csv with assigned_user_id FKs after employee enrichment.")
    print("  3. Review floor_leads.csv and add assigned_from / assigned_to dates.")


if __name__ == "__main__":
    main()
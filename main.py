"""
extract_seating.py
==================
ETL script to parse seating-arrangement CSVs exported from SharePoint
for the Ghana offices (Kumasi, Takoradi, Accra) and emit a clean,
normalised dataset ready for loading into the OMS schema.

Each source file has a completely different layout:
  - Kumasi  : comma-delimited; room name in col-0 followed by day-rows
  - Takoradi: comma-delimited; room name in col-1 of a blank-col-0 row
  - Accra   : semicolon-delimited; "BLOCK X FLOOR Y" header rows

Output tables (CSV):
  1. rooms.csv           – every room/section found, with office & location
  2. seat_assignments.csv– one row per (room, day, seat, employee)
  3. occupancy.csv       – one row per (room, seat, day) with is_occupied flag
  4. remote_log.csv      – one row per (employee, office, room, day, is_remote)

Run:
    python extract_seating.py
    # or override paths:
    python extract_seating.py --kumasi path/to/kumasi.csv ...
"""

import re
import uuid
import argparse
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COUNTRY   = "Ghana"
CITY      = "Ghana"   # will be overridden per office

OFFICES = {
    "kumasi":   {"city": "Kumasi",   "building": "Kumasi Office"},
    "takoradi": {"city": "Takoradi", "building": "Takoradi Office"},
    "accra":    {"city": "Accra",    "building": "Accra Office"},
}

DAYS_OF_WEEK = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid():
    return str(uuid.uuid4())


def _clean(val):
    """Strip whitespace; return None for nan / empty strings."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() != "nan" else None


def _is_day(val):
    return _clean(val) in DAYS_OF_WEEK


def _is_number(val):
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
# Layout
#   Row 0  : <ROOM_NAME>  <total_seats>  FLOOR LEADS  <lead1>  <lead2>  …
#   Row 1  : "# of unoccupied Seats"  "Vacant Seat/Day"  "Seat 1"  "Seat 2" …
#   Row 2-6: <Day>  <vacant_count>  <name|blank> … (name present = IN OFFICE)
#   … repeated for each room
#
# A missing name in a seat column means that employee is REMOTE for that day.
# To know which employees "belong" to a room we take the union of all names
# that ever appear across all five days for that room.

def parse_kumasi(path: str) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    rooms, assignments, occupancy, remote = [], [], [], []

    # Identify room-header rows: first cell is non-empty, not a day, not numeric,
    # and not "# of unoccupied Seats"
    skip_first = {"# of unoccupied Seats"}

    room_starts = []
    for i, row in df.iterrows():
        c0 = _clean(row[0])
        if (c0 and c0 not in DAYS_OF_WEEK and c0 not in skip_first
                and not _is_number(c0)):
            room_starts.append(i)

    for idx, start in enumerate(room_starts):
        end = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        room_row   = block.iloc[0]
        room_name  = _clean(room_row[0]) or f"Room_{start}"
        total_seats_raw = _clean(room_row[1])
        total_seats = int(total_seats_raw) if _is_number(total_seats_raw) else None

        # Floor leads: after "FLOOR LEADS" marker in row 0
        floor_leads = []
        fl_start = False
        for v in room_row[2:]:
            cv = _clean(v)
            if cv == "FLOOR LEADS":
                fl_start = True
                continue
            if fl_start and cv:
                floor_leads.append(cv)

        room_id = _uid()
        rooms.append({
            "room_id":     room_id,
            "office":      "Kumasi Office",
            "city":        "Kumasi",
            "room_name":   room_name,
            "total_seats": total_seats,
            "floor_leads": "; ".join(floor_leads),
        })

        # Header row: col-1 = "# of unoccupied Seats", col-2 = "Vacant Seat/Day",
        # col-3 = "Seat 1", col-4 = "Seat 2", …
        # (the entire header row is shifted one column to the right vs. the data rows)
        header_idx = None
        for r in range(1, len(block)):
            # header marker can be in col-0 OR col-1
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "# of unoccupied Seats" or c1 == "# of unoccupied Seats":
                header_idx = r
                break
        if header_idx is None:
            continue

        header_row = block.iloc[header_idx]
        seat_cols  = {}   # col_index -> seat_label
        for ci, val in enumerate(header_row):
            cv = _clean(val)
            if cv and cv.startswith("Seat"):
                seat_cols[ci] = cv

        # Collect all employees ever assigned to this room (union over all days)
        all_employees_in_room = set()
        day_rows = {}
        for r in range(header_idx + 1, len(block)):
            row   = block.iloc[r]
            day   = _clean(row[0])
            if not _is_day(day):
                continue
            day_rows[day] = row
            for ci in seat_cols:
                name = _clean(row[ci]) if ci < len(row) else None
                if name:
                    all_employees_in_room.add(name)

        # Now emit per-day records
        for day, row in day_rows.items():
            # col-1 = reported vacant count (may be blank), col-2 = vacant per day
            vacant_raw = _clean(row[1]) if len(row) > 1 else None
            if not _is_number(vacant_raw):
                vacant_raw = _clean(row[2]) if len(row) > 2 else None
            vacant_count = int(float(vacant_raw)) if _is_number(vacant_raw) else None

            present_employees = set()
            for ci, seat_label in seat_cols.items():
                name = _clean(row[ci]) if ci < len(row) else None
                is_occupied = bool(name)

                assignments.append({
                    "room_id":    room_id,
                    "room_name":  room_name,
                    "office":     "Kumasi Office",
                    "day":        day,
                    "seat":       seat_label,
                    "employee":   name,
                    "is_occupied": is_occupied,
                })
                occupancy.append({
                    "room_id":    room_id,
                    "room_name":  room_name,
                    "office":     "Kumasi Office",
                    "day":        day,
                    "seat":       seat_label,
                    "employee":   name,
                    "is_occupied": is_occupied,
                    "vacant_count_reported": vacant_count,
                })
                if name:
                    present_employees.add(name)

            # Remote = in room's roster but not present today
            for emp in all_employees_in_room:
                remote.append({
                    "employee":  emp,
                    "office":    "Kumasi Office",
                    "room_name": room_name,
                    "day":       day,
                    "is_remote": emp not in present_employees,
                })

    return {"rooms": rooms, "assignments": assignments,
            "occupancy": occupancy, "remote": remote}


# ---------------------------------------------------------------------------
# Parser: Takoradi
# ---------------------------------------------------------------------------
# Layout
#   Row N  : col-0 empty, col-1 = <ROOM_NAME>,  col-2 = <vacant_count>
#   Row N+1: "Days" | empty  "Daily Empty Seats"|"Vacant Seat/Day"
#             "Seat X" "Seat Y" …
#   Row N+2…: <Day>  <vacant>  <name> …
#
# Sections continue until next room header (col-1 non-empty, not a seat/day).

def parse_takoradi(path: str) -> dict:
    df = pd.read_csv(path, header=None, dtype=str)

    rooms, assignments, occupancy, remote = [], [], [], []

    SKIP_C1 = {"Daily Empty Seats", "Vacant Seat/Day"}

    def _is_room_header(row):
        c0 = _clean(row[0])
        c1 = _clean(row[1]) if len(row) > 1 else None
        return (c0 is None and c1 and c1 not in DAYS_OF_WEEK
                and not c1.startswith("Seat") and not _is_number(c1)
                and c1 not in SKIP_C1)

    room_starts = [i for i, row in df.iterrows() if _is_room_header(row)]

    # Also detect seat-range sub-blocks inside a room (rows where col-0 is
    # empty but col-1 looks like "Seat 25" — continuation columns)
    for idx, start in enumerate(room_starts):
        end = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        room_name = _clean(block.iloc[0, 1]) or f"Room_{start}"
        room_id   = _uid()

        rooms.append({
            "room_id":     room_id,
            "office":      "Takoradi Office",
            "city":        "Takoradi",
            "room_name":   room_name,
            "total_seats": None,
            "floor_leads": "",
        })

        # Find all header sub-rows (rows whose first non-nan col-1 value starts
        # with "Seat" or equals "Days")
        sub_header_rows = []
        for r in range(1, len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 in ("Days", None) and c1 and (c1.startswith("Seat") or c1 in SKIP_C1):
                sub_header_rows.append(r)
            elif c0 == "Days":
                sub_header_rows.append(r)

        # Group block into sub-sections (each starting at a "Days" header row)
        days_rows_idx = []
        for r in range(len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "Days" or (c0 is None and c1 in SKIP_C1):
                days_rows_idx.append(r)

        for si, hr in enumerate(days_rows_idx):
            end_sub = days_rows_idx[si + 1] if si + 1 < len(days_rows_idx) else len(block)
            sub = block.iloc[hr:end_sub].reset_index(drop=True)

            # Build seat_cols from header row (row 0 of sub)
            header = sub.iloc[0]
            seat_cols = {}
            for ci, val in enumerate(header):
                cv = _clean(val)
                if cv and cv.startswith("Seat"):
                    seat_cols[ci] = cv

            if not seat_cols:
                continue

            all_employees_in_sub = set()
            day_rows = {}
            for r in range(1, len(sub)):
                row = sub.iloc[r]
                day = _clean(row[0])
                if not _is_day(day):
                    continue
                day_rows[day] = row
                for ci in seat_cols:
                    name = _clean(row[ci]) if ci < len(row) else None
                    if name:
                        all_employees_in_sub.add(name)

            for day, row in day_rows.items():
                # vacant count may be in col-1 (numeric)
                vc_raw = _clean(row[1]) if len(row) > 1 else None
                vacant_count = int(vc_raw) if _is_number(vc_raw) else None

                present = set()
                for ci, seat_label in seat_cols.items():
                    name = _clean(row[ci]) if ci < len(row) else None
                    is_occ = bool(name)
                    assignments.append({
                        "room_id":    room_id,
                        "room_name":  room_name,
                        "office":     "Takoradi Office",
                        "day":        day,
                        "seat":       seat_label,
                        "employee":   name,
                        "is_occupied": is_occ,
                    })
                    occupancy.append({
                        "room_id":    room_id,
                        "room_name":  room_name,
                        "office":     "Takoradi Office",
                        "day":        day,
                        "seat":       seat_label,
                        "employee":   name,
                        "is_occupied": is_occ,
                        "vacant_count_reported": vacant_count,
                    })
                    if name:
                        present.add(name)

                for emp in all_employees_in_sub:
                    remote.append({
                        "employee":  emp,
                        "office":    "Takoradi Office",
                        "room_name": room_name,
                        "day":       day,
                        "is_remote": emp not in present,
                    })

    return {"rooms": rooms, "assignments": assignments,
            "occupancy": occupancy, "remote": remote}


# ---------------------------------------------------------------------------
# Parser: Accra
# ---------------------------------------------------------------------------
# Layout (semicolon-delimited)
#   Row N  : "BLOCK X FLOOR Y"          <- room header
#   Row N+1: "Day"  "Seat 1"  "Seat 2" … <- column header
#   Row N+2…: <Day>  <name> …
#   (blank rows / count rows between sections)

def parse_accra(path: str) -> dict:
    df = pd.read_csv(path, sep=";", header=None, dtype=str)

    rooms, assignments, occupancy, remote = [], [], [], []

    BLOCK_RE = re.compile(r"BLOCK\s+\w+\s+FLOOR\s+\d+", re.I)

    def _is_room_header(row):
        c0 = _clean(row[0])
        return bool(c0 and BLOCK_RE.match(c0))

    room_starts = [i for i, row in df.iterrows() if _is_room_header(row)]

    for idx, start in enumerate(room_starts):
        end = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block = df.iloc[start:end].reset_index(drop=True)

        # Parse "BLOCK A FLOOR 1" -> block_label, floor_number
        raw_name = _clean(block.iloc[0, 0])
        m = re.match(r"BLOCK\s+(\w+)\s+FLOOR\s+(\d+)", raw_name, re.I)
        block_label  = m.group(1) if m else "?"
        floor_number = int(m.group(2)) if m else None
        room_name    = raw_name
        room_id      = _uid()

        rooms.append({
            "room_id":      room_id,
            "office":       "Accra Office",
            "city":         "Accra",
            "room_name":    room_name,
            "block":        block_label,
            "floor_number": floor_number,
            "total_seats":  None,
            "floor_leads":  "",
        })

        # Find "Day" header row
        header_idx = None
        for r in range(1, min(5, len(block))):
            c0 = _clean(block.iloc[r, 0])
            if c0 == "Day":
                header_idx = r
                break
        if header_idx is None:
            continue

        header_row = block.iloc[header_idx]
        seat_cols  = {}
        for ci, val in enumerate(header_row):
            cv = _clean(val)
            if cv and cv.lower().startswith("seat"):
                seat_cols[ci] = cv

        all_employees = set()
        day_rows = {}
        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue
            day_rows[day] = row
            for ci in seat_cols:
                name = _clean(row[ci]) if ci < len(row) else None
                if name and name.lower() not in ("book before use",):
                    all_employees.add(name)

        for day, row in day_rows.items():
            present = set()
            for ci, seat_label in seat_cols.items():
                raw_name_cell = _clean(row[ci]) if ci < len(row) else None
                # "Book before Use" = bookable/hot-desk, not a person
                name = (raw_name_cell
                        if raw_name_cell and raw_name_cell.lower() not in
                           ("book before use",)
                        else None)
                is_occ = bool(name)
                is_hot_desk = raw_name_cell and "book" in raw_name_cell.lower()

                assignments.append({
                    "room_id":    room_id,
                    "room_name":  room_name,
                    "office":     "Accra Office",
                    "day":        day,
                    "seat":       seat_label,
                    "employee":   name,
                    "is_occupied": is_occ,
                    "is_hot_desk": bool(is_hot_desk),
                })
                occupancy.append({
                    "room_id":    room_id,
                    "room_name":  room_name,
                    "office":     "Accra Office",
                    "day":        day,
                    "seat":       seat_label,
                    "employee":   name,
                    "is_occupied": is_occ,
                    "is_hot_desk": bool(is_hot_desk),
                    "vacant_count_reported": None,
                })
                if name:
                    present.add(name)

            for emp in all_employees:
                remote.append({
                    "employee":  emp,
                    "office":    "Accra Office",
                    "room_name": room_name,
                    "day":       day,
                    "is_remote": emp not in present,
                })

    return {"rooms": rooms, "assignments": assignments,
            "occupancy": occupancy, "remote": remote}


# ---------------------------------------------------------------------------
# Merge & deduplicate
# ---------------------------------------------------------------------------

def merge_results(*results):
    combined = {"rooms": [], "assignments": [], "occupancy": [], "remote": []}
    for r in results:
        for key in combined:
            combined[key].extend(r[key])
    return combined


def deduplicate_remote(remote_rows):
    """
    An employee may appear in multiple seat-columns within the same room/day.
    Keep only one remote record per (employee, office, room, day) — marking
    is_remote=False (i.e. present) if ANY record says they were present.
    """
    index = {}
    for row in remote_rows:
        key = (row["employee"], row["office"], row["room_name"], row["day"])
        if key not in index:
            index[key] = row
        else:
            # Present wins over remote
            if not row["is_remote"]:
                index[key] = row
    return list(index.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract OMS seating data from SharePoint CSVs")
    parser.add_argument("--kumasi",   default="/mnt/user-data/uploads/Kumasi_Seating_Arrangement_In_office___Remote_.csv")
    parser.add_argument("--takoradi", default="/mnt/user-data/uploads/New_Seating_Seating_Arrangment_-_Update__________Takoradi.csv")
    parser.add_argument("--accra",    default="/mnt/user-data/uploads/Arrangements_Final-_Accra.csv")
    parser.add_argument("--out",      default="/mnt/user-data/outputs", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Parsing Kumasi …")
    kumasi   = parse_kumasi(args.kumasi)

    print("Parsing Takoradi …")
    takoradi = parse_takoradi(args.takoradi)

    print("Parsing Accra …")
    accra    = parse_accra(args.accra)

    print("Merging …")
    merged = merge_results(kumasi, takoradi, accra)
    merged["remote"] = deduplicate_remote(merged["remote"])

    # ── Write output CSVs ──────────────────────────────────────────────────
    tables = {
        "rooms.csv":            merged["rooms"],
        "seat_assignments.csv": merged["assignments"],
        "occupancy.csv":        merged["occupancy"],
        "remote_log.csv":       merged["remote"],
    }

    for filename, rows in tables.items():
        if not rows:
            print(f"  [skip] {filename} — no data")
            continue
        df = pd.DataFrame(rows)
        path = out_dir / filename
        df.to_csv(path, index=False)
        print(f"  ✓  {filename}  ({len(df):,} rows)  → {path}")

    # remote_true.csv — only employees who ARE on remote that day
    remote_df = pd.DataFrame(merged["remote"])
    if not remote_df.empty:
        remote_true_df = (
            remote_df[remote_df["is_remote"] == True]
            [["employee", "office", "room_name", "day"]]
            .sort_values(["office", "room_name", "day", "employee"])
            .reset_index(drop=True)
        )
        remote_true_path = out_dir / "remote_true.csv"
        remote_true_df.to_csv(remote_true_path, index=False)
        print(f"  ✓  remote_true.csv  ({len(remote_true_df):,} rows)  → {remote_true_path}")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    for key, rows in merged.items():
        print(f"  {key:20s}: {len(rows):>5,} records")

    # Quick remote stats
    remote_df = pd.DataFrame(merged["remote"])
    if not remote_df.empty:
        by_office = remote_df.groupby("office")["is_remote"].mean().mul(100).round(1)
        print("\nRemote rate by office (%):")
        for office, pct in by_office.items():
            print(f"  {office}: {pct}%")

    print("\nDone.")


if __name__ == "__main__":
    main()
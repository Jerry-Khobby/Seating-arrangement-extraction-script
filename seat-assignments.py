"""
extract_seating_simple.py
=========================
Extracts seat assignments from SharePoint CSVs for all three Ghana offices.
Outputs one clean CSV showing: office, floor, room, seat, day, employee name.

Output:
    seat_assignments.csv  –  who is sitting where, per day

Usage:
    python extract_seating_simple.py
    python extract_seating_simple.py --kumasi path/to/file.csv
"""

import re
import argparse
import pandas as pd
from pathlib import Path

DAYS_OF_WEEK = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _clean(val):
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
# Kumasi
# ---------------------------------------------------------------------------

def parse_kumasi(path: str) -> list[dict]:
    df = pd.read_csv(path, header=None, dtype=str)
    rows = []
    SKIP = {"# of unoccupied Seats"}

    room_starts = []
    for i, row in df.iterrows():
        c0 = _clean(row[0])
        if c0 and c0 not in DAYS_OF_WEEK and c0 not in SKIP and not _is_number(c0):
            room_starts.append(i)

    for idx, start in enumerate(room_starts):
        end       = room_starts[idx + 1] if idx + 1 < len(room_starts) else len(df)
        block     = df.iloc[start:end].reset_index(drop=True)
        room_name = _clean(block.iloc[0, 0]) or f"Room_{start}"

        # Find the header row containing "Seat X" labels
        header_idx = None
        for r in range(1, len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "# of unoccupied Seats" or c1 == "# of unoccupied Seats":
                header_idx = r
                break
        if header_idx is None:
            continue

        seat_cols = {
            ci: _clean(val)
            for ci, val in enumerate(block.iloc[header_idx])
            if _clean(val) and _clean(val).startswith("Seat")
        }

        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue
            for ci, seat_label in seat_cols.items():
                name = _clean(row[ci]) if ci < len(row) else None
                rows.append({
                    "office":    "Kumasi Office",
                    "floor":     "Floor 1",
                    "room":      room_name,
                    "seat":      seat_label,
                    "day":       day,
                    "employee":  name,
                    "occupied":  bool(name),
                })

    return rows


# ---------------------------------------------------------------------------
# Takoradi
# ---------------------------------------------------------------------------

def parse_takoradi(path: str) -> list[dict]:
    df = pd.read_csv(path, header=None, dtype=str)
    rows = []
    SKIP_C1 = {"Daily Empty Seats", "Vacant Seat/Day"}

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

        # Find all "Days" sub-header rows (room may span multiple seat-column groups)
        sub_headers = []
        for r in range(len(block)):
            c0 = _clean(block.iloc[r, 0])
            c1 = _clean(block.iloc[r, 1]) if block.shape[1] > 1 else None
            if c0 == "Days" or (c0 is None and c1 in SKIP_C1):
                sub_headers.append(r)

        for si, hr in enumerate(sub_headers):
            end_sub  = sub_headers[si + 1] if si + 1 < len(sub_headers) else len(block)
            sub      = block.iloc[hr:end_sub].reset_index(drop=True)
            seat_cols = {
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
                for ci, seat_label in seat_cols.items():
                    name = _clean(row[ci]) if ci < len(row) else None
                    rows.append({
                        "office":   "Takoradi Office",
                        "floor":    "Floor 1",
                        "room":     room_name,
                        "seat":     seat_label,
                        "day":      day,
                        "employee": name,
                        "occupied": bool(name),
                    })

    return rows


# ---------------------------------------------------------------------------
# Accra
# ---------------------------------------------------------------------------

def parse_accra(path: str) -> list[dict]:
    df = pd.read_csv(path, sep=";", header=None, dtype=str)
    rows = []
    BLOCK_RE = re.compile(r"BLOCK\s+(\w+)\s+FLOOR\s+(\d+)", re.I)

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

        header_idx = None
        for r in range(1, min(5, len(block))):
            if _clean(block.iloc[r, 0]) == "Day":
                header_idx = r
                break
        if header_idx is None:
            continue

        seat_cols = {
            ci: _clean(val)
            for ci, val in enumerate(block.iloc[header_idx])
            if _clean(val) and _clean(val).lower().startswith("seat")
        }

        for r in range(header_idx + 1, len(block)):
            row = block.iloc[r]
            day = _clean(row[0])
            if not _is_day(day):
                continue
            for ci, seat_label in seat_cols.items():
                raw  = _clean(row[ci]) if ci < len(row) else None
                # "Book before Use" = hot desk, not a person
                is_hot_desk = bool(raw and "book" in raw.lower())
                name = raw if raw and not is_hot_desk else None
                rows.append({
                    "office":      "Accra Office",
                    "floor":       f"Floor {floor_num}",
                    "room":        room_name,
                    "seat":        seat_label,
                    "day":         day,
                    "employee":    name,
                    "occupied":    bool(name),
                    "is_hot_desk": is_hot_desk,
                })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract seat assignments from SharePoint CSVs")
    ap.add_argument("--kumasi",   default="/mnt/user-data/uploads/Kumasi_Seating_Arrangement_In_office___Remote_.csv")
    ap.add_argument("--takoradi", default="/mnt/user-data/uploads/New_Seating_Seating_Arrangment_-_Update__________Takoradi.csv")
    ap.add_argument("--accra",    default="/mnt/user-data/uploads/Arrangements_Final-_Accra.csv")
    ap.add_argument("--out",      default="/mnt/user-data/outputs")
    args = ap.parse_args()

    all_rows = []

    print("Parsing Kumasi …")
    all_rows.extend(parse_kumasi(args.kumasi))

    print("Parsing Takoradi …")
    all_rows.extend(parse_takoradi(args.takoradi))

    print("Parsing Accra …")
    all_rows.extend(parse_accra(args.accra))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(all_rows)
    out_path = out_dir / "seat_assignments.csv"
    df.to_csv(out_path, index=False)

    print(f"\n✓  seat_assignments.csv  ({len(df):,} rows)  → {out_path}")
    print(f"\n=== Summary ===")
    for office, grp in df.groupby("office"):
        total   = len(grp)
        filled  = grp["occupied"].sum()
        print(f"  {office}: {filled:,} occupied / {total:,} total seat-days")


if __name__ == "__main__":
    main()
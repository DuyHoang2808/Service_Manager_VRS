#!/usr/bin/env python3
"""AOI Ingest Service - single-file version.

Watches the folder the AOI machine writes defect data into, and ingests it
into `autovrs.db` (the same SQLite file the Flutter app uses). This is the
missing "Nhanh A - API nap du lieu AOI" step from KE_HOACH_TRIEN_KHAI_PIPELINE.md,
adapted for how the AOI machine actually delivers data: it writes files
directly into a shared folder rather than calling an HTTP API - so this is a
folder-polling watcher, not a FastAPI service.

Run:
    python aoi_ingest_service.py --config aoi_ingest_config.json          # loop forever
    python aoi_ingest_service.py --config aoi_ingest_config.json --once   # single scan pass

======================================================================
1. INPUT DATA LAYOUT (confirmed manually against real AOI output)
======================================================================

    AOI_Output/<board_id>/<layer l1..l8>/<side A hoac B>.vrs
                                          <side>.txt   (khong dung o day)
                                          <side>1.jpg .. <side>N.jpg

`.vrs` (text, CRLF), 9-line header:

    Line 1: version
    Line 2: header line count (observed: always 9)
    Line 3: board bbox "xmin,ymin,xmax,ymax"
    Line 4: unknown (reserved, observed value: 0)
    Line 5: declared defect count
    Line 6: alignment point count (number of fiducials used - currently 2)
    Line 7: job / model name (used as QCamber model name)
    Line 8: layer id ("l1", "l8", ...)
    Line 9: unknown (reserved, observed value: 0)
    Line 10 .. (10 + align_count - 1): alignment point coords "x,y,idx"
    Line (10 + align_count) .. end: defect coords "x,y,type_code"
    (defect line i <-> image {side}{i}.jpg, 1-based, in file order)

Raw coordinate units are integers where the last 3 digits are the
millimeter decimal part, e.g. 520234 -> 520.234 mm. Confirmed directly by
the user, not guessed.

======================================================================
2. DECISIONS CONFIRMED WITH THE USER (via AskUserQuestion)
======================================================================

  - plc_coor: store the raw board coordinate directly in millimetres -
    NOT run through board_to_plc()/calibration (differs from the original
    KE_HOACH_TRIEN_KHAI_PIPELINE.md sketch, which used calibration).
  - Decimal separator: PERIOD (e.g. "520.234;121.337") - mandatory, because
    Flutter's `double.tryParse` (vrs_main_screen.dart / manual_vrs_screen.dart)
    does not accept a comma decimal.
  - One physical board with multiple layers (l1, l8, ...) -> one separate
    tbBoard row PER LAYER. Added `board_code` + `layer_id` columns to
    tbBoard (migration runs automatically, see ensure_schema()).
  - If declared defect count > actual saved image count: only process the
    first N defects where N = actual image count (skip the remainder -
    they have no image).

======================================================================
3. PROCESSING PIPELINE (per board folder)
======================================================================

  1. Wait until every layer folder currently under a board folder can have
     its `.vrs` parsed AND its saved `.jpg` count stops changing across
     `stable_polls_required` consecutive polls ("du anh chua" check). We do
     NOT hardcode the observed 2000-image cap - that's just what this AOI
     unit happens to do, it might differ per machine/model.
  2. Once every layer folder is stable, move the WHOLE board folder to
     `archive_dir` (shutil.move, timestamp-suffixed on name collision).
  3. For each layer: get-or-create tbModel (by name, vrs line 7),
     get-or-create tbLot (see open question below), insert one tbBoard row
     (board_code, layer_id, defect_quantity), then insert N tbDefect rows
     (type, coordinates, plc_coor, url_image pointing at the ALREADY-MOVED
     image path).

Known limitation: if the process crashes between step 2 (move) and step 3
(DB insert), the folder will already be in archive_dir with no DB rows and
no automatic retry. This follows the order the user asked for (move, then
write DB) - swap the order in process_board() if you'd rather risk "folder
not yet moved after a crash" over "moved but not recorded".

OPEN QUESTION (see ../THIET_KE_CSDL_CAP_NHAT.md section 2): the original
design PDF (muc 1.1.2) describes lot creation as a manual, confirmed
operator action (ConfirmView) BEFORE the line runs - get_or_create_lot()
below instead auto-creates an empty lot if none exists yet, which may not
match that workflow. Not changed yet pending confirmation.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

LAYER_DIR_RE = re.compile(r"^l\d+$", re.IGNORECASE)
IMAGE_NAME_RE = re.compile(r"^([A-Za-z]+)(\d+)\.jpg$", re.IGNORECASE)

log = logging.getLogger("aoi_ingest")


# ======================================================================
# 1. .vrs parsing
# ======================================================================


@dataclasses.dataclass
class VrsHeader:
    version: str
    header_line_count: int
    board_bbox: str
    unknown4: str
    defect_count: int
    align_count: int
    model_name: str
    layer_id: str
    unknown9: str


@dataclasses.dataclass
class VrsData:
    header: VrsHeader
    # (x_raw, y_raw, point_index) for each alignment/fiducial point
    alignment_points: List[Tuple[int, int, int]]
    # (x_raw, y_raw, defect_type_code) for each defect, in file order
    # (file order == image index order: defect_points[0] <-> {side}1.jpg)
    defect_points: List[Tuple[int, int, int]]


class VrsParseError(ValueError):
    pass


def _read_lines(path: Path) -> List[str]:
    # newline=None enables universal-newline translation, so CRLF is handled
    # transparently regardless of platform.
    with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
        return [line.rstrip("\n").rstrip("\r") for line in f]


def parse_header(lines: List[str], *, source: str = "<unknown>") -> VrsHeader:
    if len(lines) < 9:
        raise VrsParseError(
            f"{source}: file has only {len(lines)} line(s), need at least 9 header lines"
        )
    try:
        return VrsHeader(
            version=lines[0].strip(),
            header_line_count=int(lines[1].strip()),
            board_bbox=lines[2].strip(),
            unknown4=lines[3].strip(),
            defect_count=int(lines[4].strip()),
            align_count=int(lines[5].strip()),
            model_name=lines[6].strip(),
            layer_id=lines[7].strip(),
            unknown9=lines[8].strip(),
        )
    except ValueError as exc:
        raise VrsParseError(f"{source}: malformed header line - {exc}") from exc


def _parse_coord_line(line: str, *, source: str, line_no: int) -> Tuple[int, int, int]:
    parts = line.strip().split(",")
    if len(parts) != 3:
        raise VrsParseError(
            f"{source}: line {line_no}: expected 'x,y,code', got {line!r}"
        )
    try:
        x, y, code = (int(p) for p in parts)
    except ValueError as exc:
        raise VrsParseError(f"{source}: line {line_no}: non-integer field - {exc}") from exc
    return x, y, code


def parse_vrs(path: Path) -> VrsData:
    """Parse a full .vrs file. Raises VrsParseError on any structural problem
    (caller should treat this as "not ready yet / still being written" during
    polling, not necessarily a fatal error)."""
    path = Path(path)
    lines = _read_lines(path)
    header = parse_header(lines, source=str(path))

    data_lines = lines[9:]
    align_n = header.align_count

    if align_n < 0:
        raise VrsParseError(f"{path}: negative alignment point count in header ({align_n})")

    if len(data_lines) < align_n:
        raise VrsParseError(
            f"{path}: header declares {align_n} alignment point(s) but only "
            f"{len(data_lines)} data line(s) are present"
        )

    alignment_points = [
        _parse_coord_line(l, source=str(path), line_no=10 + i)
        for i, l in enumerate(data_lines[:align_n])
    ]

    defect_lines = [l for l in data_lines[align_n:] if l.strip()]
    defect_points = [
        _parse_coord_line(l, source=str(path), line_no=10 + align_n + i)
        for i, l in enumerate(defect_lines)
    ]

    return VrsData(header=header, alignment_points=alignment_points, defect_points=defect_points)


def raw_to_mm(raw: int) -> float:
    """Convert a raw AOI integer coordinate to millimetres.

    Confirmed by user with example: 520234 -> 520.234 mm (last 3 digits are
    the decimal part). Negative values divide the same way: -520234 -> -520.234.
    """
    return raw / 1000.0


def format_coord_pair(x_raw: int, y_raw: int) -> str:
    """Format a raw (x, y) pair as "x;y" in millimetres with a PERIOD decimal
    separator.

    This exact format/separator matters: Flutter's `_parsePlcCoords`
    (vrs_main_screen.dart) and the inline parser in manual_vrs_screen.dart both
    call `double.tryParse` on each half after splitting on ';' - and
    `double.tryParse` only accepts a period decimal point. A comma decimal
    (e.g. "520,234") would silently parse to 0, so period is mandatory here,
    not a style choice.
    """
    x_mm = raw_to_mm(x_raw)
    y_mm = raw_to_mm(y_raw)
    return f"{x_mm:.3f};{y_mm:.3f}"


# ======================================================================
# 2. SQLite access layer
# ======================================================================


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the shared `autovrs.db`.

    WAL mode + busy_timeout are mandatory here because two separate
    processes (this watcher and the Flutter app) touch the file concurrently -
    without them, "database is locked" errors are expected under load.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing (mirrors local_database_service.dart's
    _createTables), and apply AOI-ingestion-specific column migrations.
    Idempotent - safe to call on every process start."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tbModel (
            id_model INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            line_size REAL,
            space_size REAL,
            url_gerber TEXT
        );

        CREATE TABLE IF NOT EXISTS tbLot (
            id_lot INTEGER PRIMARY KEY AUTOINCREMENT,
            NG_rate REAL,
            fakeDef REAL,
            board_quantity INTEGER,
            tbModelid_model INTEGER,
            FOREIGN KEY (tbModelid_model) REFERENCES tbModel(id_model)
        );

        CREATE TABLE IF NOT EXISTS tbBoard (
            id_board INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_quantity INTEGER,
            erro_quantity INTEGER,
            tbLotid_lot INTEGER,
            FOREIGN KEY (tbLotid_lot) REFERENCES tbLot(id_lot)
        );

        CREATE TABLE IF NOT EXISTS tbDefect (
            id_defect INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            judgement TEXT,
            height REAL,
            width REAL,
            time TEXT,
            coordinates TEXT,
            url_image TEXT,
            tbBoardid_board INTEGER,
            plc_coor TEXT,
            FOREIGN KEY (tbBoardid_board) REFERENCES tbBoard(id_board)
        );

        CREATE TABLE IF NOT EXISTS tbConfig (
            config_key TEXT PRIMARY KEY,
            config_value TEXT
        );
        """
    )

    board_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tbBoard)")}
    if "board_code" not in board_cols:
        conn.execute("ALTER TABLE tbBoard ADD COLUMN board_code TEXT")
    if "layer_id" not in board_cols:
        conn.execute("ALTER TABLE tbBoard ADD COLUMN layer_id TEXT")
    if "status" not in board_cols:
        # 'pending' (chua xu ly xong) -> 'in_progress' -> 'completed'. Dung de
        # xac dinh board nao con cho trong co che "board tiep theo" ben Flutter
        # (xem vrs_provider.dart::completeCurrentBoardAndCheckNext). SQLite
        # dien gia tri DEFAULT nay cho ca cac dong da co san, khong chi dong moi.
        conn.execute("ALTER TABLE tbBoard ADD COLUMN status TEXT DEFAULT 'pending'")
    if "completed_at" not in board_cols:
        conn.execute("ALTER TABLE tbBoard ADD COLUMN completed_at TEXT")
    conn.commit()


def get_or_create_model(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id_model FROM tbModel WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row["id_model"]
    cur = conn.execute("INSERT INTO tbModel (name) VALUES (?)", (name,))
    return cur.lastrowid


def get_or_create_lot(conn: sqlite3.Connection, id_model: int) -> int:
    """Reuse the OLDEST lot for this model (id_lot ASC), or create a new
    (empty) one if none exists yet.

    IMPORTANT: must match Flutter's convention exactly. `getFirstLotByModelId`
    in local_database_service.dart picks the lot with the SMALLEST id_lot
    (ASC) as "current lot" for a model, and that's what vrs_main_screen.dart
    displays/operates on. If this picked a different lot (e.g. newest, DESC),
    boards inserted here could attach to a lot Flutter never shows, silently
    orphaning them. Previously this used DESC - fixed to ASC to match.

    ASSUMPTION (not explicitly specified by the user, and possibly not
    matching the original design doc - see ../THIET_KE_CSDL_CAP_NHAT.md
    section 2): the original design describes lot creation as a manual,
    confirmed operator action (ConfirmView) before the line runs, and no such
    UI is wired up in Flutter yet (insertLot is only ever called from the
    demo data seeder). Auto-creating an empty lot here is currently the only
    way a real lot gets created at all. Revisit once Flutter has a real lot
    creation/confirmation screen - lots should probably be required to
    pre-exist at that point instead of being auto-created here.
    """
    row = conn.execute(
        "SELECT id_lot FROM tbLot WHERE tbModelid_model = ? ORDER BY id_lot ASC LIMIT 1",
        (id_model,),
    ).fetchone()
    if row is not None:
        return row["id_lot"]
    cur = conn.execute("INSERT INTO tbLot (tbModelid_model) VALUES (?)", (id_model,))
    return cur.lastrowid


def insert_board(
    conn: sqlite3.Connection,
    *,
    id_lot: int,
    board_code: str,
    layer_id: str,
    defect_quantity: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tbBoard (defect_quantity, erro_quantity, tbLotid_lot, board_code, layer_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (defect_quantity, 0, id_lot, board_code, layer_id),
    )
    return cur.lastrowid


def insert_defects(conn: sqlite3.Connection, *, id_board: int, defects: Sequence[dict]) -> List[int]:
    """`defects` items must have keys: type, coordinates, plc_coor, url_image."""
    ids: List[int] = []
    for d in defects:
        cur = conn.execute(
            """
            INSERT INTO tbDefect (type, coordinates, url_image, tbBoardid_board, plc_coor)
            VALUES (?, ?, ?, ?, ?)
            """,
            (d["type"], d["coordinates"], d["url_image"], id_board, d["plc_coor"]),
        )
        ids.append(cur.lastrowid)
    return ids


def insert_layer_result(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    board_code: str,
    layer_id: str,
    defects: Sequence[dict],
) -> Tuple[int, int, List[int]]:
    """Convenience wrapper: model -> lot -> board -> defects, as one
    transaction. Returns (id_board, id_lot, [id_defect, ...])."""
    with conn:  # implicit BEGIN/COMMIT (or ROLLBACK on exception)
        id_model = get_or_create_model(conn, model_name)
        id_lot = get_or_create_lot(conn, id_model)
        id_board = insert_board(
            conn,
            id_lot=id_lot,
            board_code=board_code,
            layer_id=layer_id,
            defect_quantity=len(defects),
        )
        defect_ids = insert_defects(conn, id_board=id_board, defects=defects)
    return id_board, id_lot, defect_ids


# ======================================================================
# 3. Folder watcher
# ======================================================================


@dataclass
class LayerState:
    last_jpg_count: int = -1
    stable_polls: int = 0


@dataclass
class BoardState:
    layer_states: Dict[str, LayerState] = field(default_factory=dict)
    last_layer_names: Optional[FrozenSet[str]] = None
    layer_set_stable_polls: int = 0


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["db_path"] = os.path.expandvars(os.path.expanduser(cfg["db_path"]))
    return cfg


def find_side_file(layer_dir: Path) -> Optional[Path]:
    """Return the single `.vrs` file in a layer folder, or None if not
    present yet (AOI still writing / folder empty)."""
    candidates = sorted(layer_dir.glob("*.vrs"))
    if not candidates:
        return None
    if len(candidates) > 1:
        log.warning(
            "%s: expected exactly 1 .vrs file, found %d - using %s",
            layer_dir, len(candidates), candidates[0].name,
        )
    return candidates[0]


def count_side_images(layer_dir: Path, side: str) -> int:
    """Return the max numeric index found among {side}<n>.jpg images (not
    just a count), so gaps in numbering can't under-report readiness."""
    max_idx = 0
    for f in layer_dir.glob(f"{side}*.jpg"):
        m = IMAGE_NAME_RE.match(f.name)
        if m and m.group(1).lower() == side.lower():
            max_idx = max(max_idx, int(m.group(2)))
    return max_idx


def evaluate_layer(
    layer_dir: Path, state: LayerState, stable_polls_required: int
) -> Optional[VrsData]:
    """Returns parsed VrsData once the layer is considered "ready" (image
    count stable across `stable_polls_required` consecutive polls), else
    None (caller should try again on the next poll)."""
    vrs_path = find_side_file(layer_dir)
    if vrs_path is None:
        return None

    try:
        data = parse_vrs(vrs_path)
    except VrsParseError as exc:
        log.debug("%s: not parseable yet (%s) - will retry", vrs_path, exc)
        return None

    side = vrs_path.stem  # "B.vrs" -> "B"
    jpg_count = count_side_images(layer_dir, side)

    if jpg_count == state.last_jpg_count:
        state.stable_polls += 1
    else:
        state.stable_polls = 1
        state.last_jpg_count = jpg_count

    if jpg_count == 0:
        return None  # nothing saved yet

    if state.stable_polls < stable_polls_required:
        log.info(
            "%s: %d image(s) saved so far, waiting for stability (%d/%d polls)",
            layer_dir, jpg_count, state.stable_polls, stable_polls_required,
        )
        return None

    n_ready = min(data.header.defect_count, jpg_count)
    if jpg_count < data.header.defect_count:
        log.warning(
            "%s: header declares %d defect(s) but only %d image(s) saved - "
            "processing first %d defect(s) only, per confirmed rule",
            layer_dir, data.header.defect_count, jpg_count, n_ready,
        )
    log.info("%s: ready (%d defect(s) to ingest)", layer_dir, n_ready)
    return data


def process_board(board_dir: Path, board_state: BoardState, cfg: dict, conn: sqlite3.Connection) -> bool:
    """Attempt to process one board folder. Returns True if it was fully
    processed (moved + written to DB) during this call."""
    layer_dirs = sorted(
        p for p in board_dir.iterdir() if p.is_dir() and LAYER_DIR_RE.match(p.name)
    )
    if not layer_dirs:
        return False

    layer_names = frozenset(p.name for p in layer_dirs)
    if layer_names != board_state.last_layer_names:
        board_state.last_layer_names = layer_names
        board_state.layer_set_stable_polls = 1
        log.info(
            "%s: layer set = %s (waiting for it to stabilize)",
            board_dir, sorted(layer_names),
        )
        return False
    board_state.layer_set_stable_polls += 1

    stable_polls_required = cfg["stable_polls_required"]
    if board_state.layer_set_stable_polls < stable_polls_required:
        return False

    ready_data: Dict[str, VrsData] = {}
    for layer_dir in layer_dirs:
        state = board_state.layer_states.setdefault(layer_dir.name, LayerState())
        data = evaluate_layer(layer_dir, state, stable_polls_required)
        if data is None:
            return False  # at least one layer not ready yet - wait for all
        ready_data[layer_dir.name] = data

    # --- every layer is ready: move the whole board folder, then write DB ---
    board_code = board_dir.name
    archive_dir = Path(cfg["archive_dir"])
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / board_code
    if dest.exists():
        dest = archive_dir / f"{board_code}_{int(time.time())}"
    log.info("Moving %s -> %s", board_dir, dest)
    shutil.move(str(board_dir), str(dest))

    for layer_name, data in ready_data.items():
        layer_dir = dest / layer_name
        vrs_path = find_side_file(layer_dir)
        side = vrs_path.stem
        n_ready = min(data.header.defect_count, count_side_images(layer_dir, side))

        defects = []
        for i in range(n_ready):
            x_raw, y_raw, type_code = data.defect_points[i]
            img_path = layer_dir / f"{side}{i + 1}.jpg"
            coord_str = format_coord_pair(x_raw, y_raw)
            defects.append(
                {
                    "type": str(type_code),
                    "coordinates": coord_str,
                    "plc_coor": coord_str,
                    "url_image": str(img_path),
                }
            )

        id_board, id_lot, defect_ids = insert_layer_result(
            conn,
            model_name=data.header.model_name,
            board_code=board_code,
            layer_id=data.header.layer_id,
            defects=defects,
        )
        log.info(
            "%s/%s: inserted id_board=%d (lot=%d), %d defect row(s)",
            board_code, layer_name, id_board, id_lot, len(defect_ids),
        )

    board_state.layer_states.clear()
    return True


def run_once(cfg: dict, states: Dict[str, BoardState], conn: sqlite3.Connection) -> None:
    watch_dir = Path(cfg["watch_dir"])
    if not watch_dir.is_dir():
        log.warning("watch_dir %s does not exist yet", watch_dir)
        return

    board_dirs = [p for p in watch_dir.iterdir() if p.is_dir()]
    seen = set()
    for board_dir in board_dirs:
        seen.add(board_dir.name)
        state = states.setdefault(board_dir.name, BoardState())
        try:
            processed = process_board(board_dir, state, cfg, conn)
            if processed:
                states.pop(board_dir.name, None)
        except Exception:
            log.exception("Error processing board folder %s", board_dir)

    # Drop stale state for boards that vanished from watch_dir (e.g. moved or
    # deleted manually outside this watcher).
    for name in list(states.keys()):
        if name not in seen:
            states.pop(name, None)


# ======================================================================
# 4. Entry point
# ======================================================================


def main(argv: Optional[List[str]] = None) -> int:
    # Dong goi PyInstaller (onedir): __file__ tro vao ben trong bundle, khong
    # phai thu muc chua file .exe - phai dung sys.executable de tim dung
    # aoi_ingest_config.json nam canh file .exe khi da build.
    app_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=str(app_dir / "aoi_ingest_config.json")
    )
    parser.add_argument("--once", action="store_true", help="Run a single scan pass and exit")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))

    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if cfg.get("log_file"):
        handlers.append(logging.FileHandler(cfg["log_file"], encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )

    conn = db_connect(cfg["db_path"])
    log.info(
        "AOI ingest service started. watch_dir=%s archive_dir=%s db_path=%s",
        cfg["watch_dir"], cfg["archive_dir"], cfg["db_path"],
    )

    states: Dict[str, BoardState] = {}
    try:
        if args.once:
            run_once(cfg, states, conn)
        else:
            while True:
                run_once(cfg, states, conn)
                time.sleep(cfg["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

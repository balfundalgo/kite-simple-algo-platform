"""
watchlist.py
============
The watchlist replaces the client's Excel sheet: one row per share with its
buy price, market type and S-level. It is edited in the app and kept in
state/watchlist.json (atomic writes). The engine reads it live, so edits
take effect on the next pass without a restart.

Excel / CSV import understands the client's sheet headers
(SHARE_NAME, SHARE_BUYING_PRICE, MKT_TYPE, SHARES_FORCE_SELL) as well as
plain ones (SYMBOL, BUY_PRICE, MARKET_TYPE, S_LEVEL). Notes typed into
those columns (e.g. "IF THIS COLUMN IS YES") are ignored.

Share names from the sheet ("YES BANK") are turned into NSE symbols
(YESBANK) with the Kite instrument list: exact symbol first, then company
name, then a close-name match. Anything still unknown is flagged.
"""

import csv
import difflib
import json
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from portfolio import WatchItem, norm_market_type, norm_s_level

WATCH_FILE = config.STATE_DIR / "watchlist.json"

_lock = threading.RLock()
_items: List[WatchItem] = []

SYMBOL_HEADERS = ("SYMBOL", "TRADINGSYMBOL", "TRADING_SYMBOL", "SHARE_NAME", "SHARE",
                  "STOCK", "NAME", "SCRIP")
BUY_HEADERS = ("BUY_PRICE", "SHARE_BUYING_PRICE", "BUYING_PRICE", "BUY_P", "BUY")
TYPE_HEADERS = ("MARKET_TYPE", "MKT_TYPE", "TYPE")
SLEVEL_HEADERS = ("S_LEVEL", "SHARES_FORCE_SELL", "SLEVEL", "S")
ENABLED_HEADERS = ("ENABLED", "ACTIVE")


# ----------------------------------------------------------------------
# store
# ----------------------------------------------------------------------
def load() -> List[WatchItem]:
    global _items
    with _lock:
        if WATCH_FILE.exists():
            try:
                raw = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
                _items = [WatchItem(**r) for r in raw]
            except Exception:
                _items = []
        return list(_items)


def items() -> List[WatchItem]:
    with _lock:
        return [WatchItem(**asdict(w)) for w in _items]


def save(new_items: List[WatchItem]) -> None:
    global _items
    with _lock:
        _items = [WatchItem(**asdict(w)) for w in new_items]
        config.atomic_write_json(WATCH_FILE, [asdict(w) for w in _items])


def upsert(item: WatchItem, old_symbol: Optional[str] = None) -> None:
    with _lock:
        cur = items()
        key = old_symbol or item.symbol
        for i, w in enumerate(cur):
            if w.symbol == key:
                cur[i] = item
                break
        else:
            cur.append(item)
        save(cur)


def remove(symbol: str) -> None:
    with _lock:
        save([w for w in items() if w.symbol != symbol])


def move(symbol: str, delta: int) -> None:
    with _lock:
        cur = items()
        idx = next((i for i, w in enumerate(cur) if w.symbol == symbol), None)
        if idx is None:
            return
        j = max(0, min(len(cur) - 1, idx + delta))
        cur.insert(j, cur.pop(idx))
        save(cur)


def set_s_level_all(level: str) -> None:
    with _lock:
        cur = items()
        for w in cur:
            w.s_level = norm_s_level(level)
        save(cur)


# ----------------------------------------------------------------------
# import / export
# ----------------------------------------------------------------------
def _key(h) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(h or "").strip().upper()).strip("_")


def _find(headers: List[str], wanted) -> Optional[int]:
    for w in wanted:
        if w in headers:
            return headers.index(w)
    return None


def symbolize(text: str) -> str:
    return re.sub(r"[^A-Z0-9&-]+", "", str(text or "").upper())


def _num(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def rows_to_items(rows: List[list]) -> Tuple[List[WatchItem], List[str]]:
    """First row with a symbol/name header is the header row."""
    notes = []
    hdr_idx = None
    for i, r in enumerate(rows[:20]):
        keys = [_key(c) for c in r]
        if _find(keys, SYMBOL_HEADERS) is not None and _find(keys, BUY_HEADERS) is not None:
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError("No header row with a share name/symbol and a buy price column")
    keys = [_key(c) for c in rows[hdr_idx]]
    ci_sym = _find(keys, SYMBOL_HEADERS)
    ci_buy = _find(keys, BUY_HEADERS)
    ci_type = _find(keys, TYPE_HEADERS)
    ci_s = _find(keys, SLEVEL_HEADERS)
    ci_en = _find(keys, ENABLED_HEADERS)

    out, seen = [], set()
    for r in rows[hdr_idx + 1:]:
        r = list(r) + [None] * (len(keys) - len(r))
        name = str(r[ci_sym] or "").strip()
        buy = _num(r[ci_buy])
        if not name or buy is None:
            continue
        sym = symbolize(name)
        if sym in seen:
            notes.append(f"duplicate {name} skipped")
            continue
        seen.add(sym)
        mt = norm_market_type(r[ci_type]) if ci_type is not None else ""
        if ci_type is not None and r[ci_type] and not mt:
            notes.append(f"{name}: market type '{r[ci_type]}' not recognised, BULL used")
        lvl = norm_s_level(r[ci_s]) if ci_s is not None else ""
        en = True
        if ci_en is not None and str(r[ci_en] or "").strip().upper() in ("NO", "N", "FALSE", "0"):
            en = False
        out.append(WatchItem(symbol=sym, buy_price=buy, market_type=mt or "BULL",
                             s_level=lvl, enabled=en, name=name))
    return out, notes


def read_file(path: str) -> Tuple[List[WatchItem], List[str]]:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.reader(f)]
    return rows_to_items(rows)


def export_file(path: str) -> None:
    p = Path(path)
    hdr = ["SYMBOL", "NAME", "BUY_PRICE", "MARKET_TYPE", "S_LEVEL", "ENABLED"]
    data = [[w.symbol, w.name, w.buy_price, w.market_type, w.s_level,
             "YES" if w.enabled else "NO"] for w in items()]
    if p.suffix.lower() == ".xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "WATCHLIST"
        ws.append(hdr)
        for r in data:
            ws.append(r)
        wb.save(p)
    else:
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(data)


# ----------------------------------------------------------------------
# symbol resolution against the Kite NSE instrument list
# ----------------------------------------------------------------------
def build_index(instruments: List[dict]) -> Dict[str, dict]:
    """tradingsymbol -> row, NSE cash equities only."""
    return {r["tradingsymbol"]: r for r in instruments
            if r.get("segment") == "NSE" and r.get("instrument_type") == "EQ"}


def resolve(item: WatchItem, index: Dict[str, dict]) -> Optional[str]:
    if item.symbol in index:
        return item.symbol
    wanted = (item.name or item.symbol).upper().strip()
    if not wanted:
        return None
    names = {}
    for sym, r in index.items():
        n = str(r.get("name") or "").upper().strip()
        if n:
            names.setdefault(n, sym)
    if wanted in names:
        return names[wanted]
    starts = [s for n, s in names.items() if n.startswith(wanted + " ") or n.startswith(wanted)]
    if len(starts) == 1:
        return starts[0]
    sq = symbolize(wanted)
    if sq in index:
        return sq
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.8)
    if close:
        return names[close[0]]
    return None


def resolve_all(index: Dict[str, dict]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Returns ([(old, new)], [unresolved])."""
    changed, missing = [], []
    with _lock:
        cur = items()
        taken = {w.symbol for w in cur if w.symbol in index}
        for w in cur:
            if w.symbol in index:
                continue
            new = resolve(w, index)
            if new and new not in taken:
                changed.append((w.symbol, new))
                if not w.name:
                    w.name = w.symbol
                w.symbol = new
                taken.add(new)
            else:
                missing.append(w.name or w.symbol)
        if changed:
            save(cur)
    return changed, missing


load()

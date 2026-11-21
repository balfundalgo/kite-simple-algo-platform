"""
config.py
=========
Paths, default settings, settings persistence and IST time helpers.

Everything the user can change lives in settings.json next to the EXE
(or the repo root when running from source). Secrets never leave that file.
Writes are atomic (temp file + replace) so a crash can never leave a
half-written settings file behind.
"""

import copy
import json
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "Balfund Simple Algo Platform"
APP_VERSION = "1.0.0"
BROKER = "Zerodha"

IST = ZoneInfo("Asia/Kolkata")

MARKET_TYPES = ("BULL", "BEAR", "BUY_ON_DIP", "SELL_ON_RALLY")
S_LEVELS = ("S1", "S2", "S3", "S4", "S5")


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):          # PyInstaller EXE
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent  # repo root


BASE_DIR = _base_dir()
SETTINGS_FILE = BASE_DIR / "settings.json"
TOKEN_DIR = BASE_DIR / "tokens"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
for _d in (TOKEN_DIR, LOG_DIR, STATE_DIR):
    _d.mkdir(exist_ok=True)


# ----------------------------------------------------------------------
# defaults
# ----------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "mode": "PAPER",                     # PAPER | LIVE

    # capital split (applied by "Initialise portfolio")
    "initial_investment": 1000000.0,     # rupees
    "equity_pct": 60.0,                  # % of capital split across N share slots
    "num_slots": 4,                      # N

    # entry
    "entry_order_type": "LIMIT",         # LIMIT (at the sheet buy price) | MARKET
    "entry_tolerance_pct": 0.0,          # buy when LTP <= buy price x (1 + tol%)
    "market_buffer_pct": 0.5,            # MARKET buys size qty for this much slippage

    # targets: sell price = multiplier x average cost
    "target_mult": {"BULL": 1.15, "BEAR": 1.09, "BUY_ON_DIP": 1.05, "SELL_ON_RALLY": 1.05},

    # ageing: every `ageing_days` unsold, target x factor (repeats)
    "ageing_days": 30,
    "ageing_factor": {"BULL": 0.95, "BEAR": 0.80, "BUY_ON_DIP": 0.70, "SELL_ON_RALLY": 0.70},

    # averaging down from the reserve pool
    "avg_down_drop_pct": 5.0,            # trigger: LTP <= last buy price x (1 - drop%)
    "avg_down_reserve_pct": 50.0,        # use this % of the free reserve
    "avg_down_max_adds": 0,              # per holding, 0 = unlimited

    # S-levels: % of the holding sold when the level is set and the
    # holding is at a loss or close to cost (LTP <= avg x (1 + near%))
    "s_level_pct": {"S1": 20.0, "S2": 40.0, "S3": 60.0, "S4": 80.0, "S5": 100.0},
    "s_level_near_pct": 1.0,

    # exits
    "exit_order_type": "LIMIT",          # LIMIT (at LTP) | MARKET

    # timing (IST)
    "trade_start": "09:15",
    "trade_end": "15:25",
    "ignore_market_hours": False,        # PAPER testing outside market hours

    # orders
    "limit_order_timeout_s": 60,         # unfilled LIMIT orders are cancelled after this
    "market_protection": -1,             # -1 = Kite auto market protection

    # kill switch (persisted, survives restarts)
    "kill_switch": False,

    # login callback
    "redirect_host": "127.0.0.1",
    "redirect_port": 8765,

    # single Kite account
    "account": {"name": "main", "api_key": "", "api_secret": ""},
}


def _merge(base: dict, stored: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in stored.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return _merge(DEFAULT_SETTINGS, json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return copy.deepcopy(DEFAULT_SETTINGS)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def save_settings(data: dict) -> None:
    atomic_write_json(SETTINGS_FILE, data)


SETTINGS = load_settings()


# ----------------------------------------------------------------------
# time helpers
# ----------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(text: str) -> dtime:
    h, m = text.strip().split(":")
    return dtime(int(h), int(m))


def in_window(now: datetime, start: str, end: str) -> bool:
    """Weekday and between start and end (IST)."""
    if now.weekday() >= 5:
        return False
    t = now.time()
    return parse_hhmm(start) <= t <= parse_hhmm(end)

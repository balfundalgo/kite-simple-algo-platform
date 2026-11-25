"""
test_logic.py
=============
Offline checks of every rule. No broker, no network, no GUI.

    python3 src/test_logic.py

CI runs this on Ubuntu before building the EXE; the build only happens
if everything passes.
"""

import copy
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import portfolio as pf  # noqa: E402
import instruments  # noqa: E402
import reconcile as rc  # noqa: E402
import funds  # noqa: E402
from indicators import rsi  # noqa: E402
from watchlist import resolve, rows_to_items  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"FAIL  {name}")


def near(a, b, tol=0.011):
    return a is not None and b is not None and abs(a - b) <= tol


S = copy.deepcopy(config.DEFAULT_SETTINGS)
D0 = date(2026, 11, 23)


def W(sym, buy, mt="BULL", s="", en=True):
    return pf.WatchItem(symbol=sym, buy_price=buy, market_type=mt, s_level=s, enabled=en)


def fresh(cap=1_000_000, eq=60, n=4):
    return pf.new_book(cap, eq, n, D0)


# ---------------------------------------------------------------- capital split
b = fresh()
check("4 slots", len(b.slots) == 4)
check("slot cash = 0.6 x 10L / 4", all(near(s.cash, 150000) for s in b.slots))
check("reserve = 40%", near(b.reserve, 400000))
b3 = fresh(n=3)
check("N=3 -> 2L per slot", near(b3.slots[0].cash, 200000))
try:
    pf.new_book(100, 60, 0, D0)
    check("N=0 rejected", False)
except ValueError:
    check("N=0 rejected", True)

# ---------------------------------------------------------------- entry
check("entry at buy price", pf.entry_ready(W("A", 100), 100.0, S))
check("entry below buy price", pf.entry_ready(W("A", 100), 99.0, S))
check("no entry above buy price", not pf.entry_ready(W("A", 100), 100.05, S))
s_tol = copy.deepcopy(S)
s_tol["entry_tolerance_pct"] = 0.5
check("tolerance 0.5%", pf.entry_ready(W("A", 100), 100.4, s_tol))
check("qty floor", pf.floor_qty(150000, 17.6) == 8522)
check("qty 0 when too dear", pf.floor_qty(1000, 2180) == 0)

acts = pf.decide(b, [W("YESBANK", 17.6), W("WIPRO", 184.2)],
                 {"YESBANK": 17.5, "WIPRO": 190}, D0, S, busy=set())
check("one BUY (only YESBANK hit)", len(acts) == 1 and acts[0]["kind"] == "BUY"
      and acts[0]["symbol"] == "YESBANK")
check("LIMIT at min(ltp, buy)", near(acts[0]["price"], 17.5))
check("qty from slot 1", acts[0]["qty"] == pf.floor_qty(150000, 17.5) and acts[0]["slot_id"] == 1)

# first ones whose price hits, in list order, one slot each
items = [W("A", 100), W("B", 50), W("C", 10), W("D", 20), W("E", 30)]
ltps = {"A": 99, "B": 49, "C": 9, "D": 19, "E": 29}
acts = pf.decide(fresh(), items, ltps, D0, S, busy=set())
check("4 slots -> 4 buys", [a["symbol"] for a in acts] == ["A", "B", "C", "D"])
check("slots 1..4 in order", [a["slot_id"] for a in acts] == [1, 2, 3, 4])
acts = pf.decide(fresh(), items, ltps, D0, S, busy=set(), busy_slots={1})
check("busy slot not reused", [a["slot_id"] for a in acts] == [2, 3, 4])
acts = pf.decide(fresh(), items, ltps, D0, S, busy={"A"})
check("busy symbol skipped", "A" not in [a["symbol"] for a in acts])
acts = pf.decide(fresh(), [W("A", 100, en=False), W("B", 50)], ltps, D0, S, busy=set())
check("disabled row skipped", [a["symbol"] for a in acts] == ["B"])
s_mkt = copy.deepcopy(S)
s_mkt["entry_order_type"] = "MARKET"
acts = pf.decide(fresh(), [W("A", 100)], {"A": 100}, D0, s_mkt, busy=set())
check("MARKET qty keeps a slippage buffer", acts[0]["qty"] == pf.floor_qty(150000, 100.5))

# ---------------------------------------------------------------- fills
b = fresh()
pf.apply_buy(b, "A", 1000, 100.0, "SLOT", D0, slot_id=1, market_type="BULL")
check("slot 1 taken", b.slot(1).symbol == "A")
check("slot cash reduced", near(b.slot(1).cash, 50000))
h = b.holdings["A"]
check("avg cost", near(h.avg, 100))
acts = pf.decide(b, [W("A", 100), W("B", 50)], {"A": 99, "B": 49}, D0, S, busy=set())
check("held share not bought again", [a["symbol"] for a in acts] == ["B"])
check("next free slot is 2", acts[0]["slot_id"] == 2)

# ---------------------------------------------------------------- targets
check("BULL 1.15", near(pf.target_price(h, "BULL", D0, S), 115.0))
check("BEAR 1.09", near(pf.target_price(h, "BEAR", D0, S), 109.0))
check("BUY ON DIP 1.05", near(pf.target_price(h, "BUY_ON_DIP", D0, S), 105.0))
check("SELL ON RALLY 1.05", near(pf.target_price(h, "SELL_ON_RALLY", D0, S), 105.0))
check("ageing 29 days -> none", pf.ageing_steps(D0.isoformat(), D0 + timedelta(days=29), 30) == 0)
check("ageing 30 days -> 1", pf.ageing_steps(D0.isoformat(), D0 + timedelta(days=30), 30) == 1)
check("ageing 65 days -> 2", pf.ageing_steps(D0.isoformat(), D0 + timedelta(days=65), 30) == 2)
d30, d60 = D0 + timedelta(days=30), D0 + timedelta(days=60)
check("BULL aged once 115 x 0.95", near(pf.target_price(h, "BULL", d30, S), 109.25))
check("BULL aged twice", near(pf.target_price(h, "BULL", d60, S), 103.80, 0.03))
check("BEAR aged once 109 x 0.8", near(pf.target_price(h, "BEAR", d30, S), 87.20))
check("BUY ON DIP aged once 105 x 0.7", near(pf.target_price(h, "BUY_ON_DIP", d30, S), 73.50))
s_age = copy.deepcopy(S)
s_age["ageing_days"] = 10
check("ageing period configurable", near(pf.target_price(h, "BULL", D0 + timedelta(days=10), s_age), 109.25))

acts = pf.decide(b, [W("A", 100)], {"A": 115.0}, D0, S, busy=set())
check("target hit -> SELL all", acts and acts[0]["kind"] == "SELL_TARGET" and acts[0]["qty"] == 1000)
acts = pf.decide(b, [W("A", 100, mt="BEAR")], {"A": 109.0}, D0, S, busy=set())
check("market type read live from watchlist", acts and acts[0]["kind"] == "SELL_TARGET")
acts = pf.decide(b, [], {"A": 115.0}, D0, S, busy=set())
check("removed from watchlist -> still managed", acts and acts[0]["kind"] == "SELL_TARGET")

pnl = pf.apply_sell(b, "A", 1000, 115.0)
check("realized P/L", near(pnl, 15000))
check("slot freed", b.slot(1).symbol is None and "A" not in b.holdings)
check("proceeds back to slot", near(b.slot(1).cash, 165000))
check("reserve untouched", near(b.reserve, 400000))

# ---------------------------------------------------------------- averaging down
b = fresh()
pf.apply_buy(b, "A", 1000, 100.0, "SLOT", D0, slot_id=1)
h = b.holdings["A"]
check("trigger 5% below", near(pf.avg_down_trigger(h, S), 95.0))
check("no add at 95.05", not pf.decide(b, [W("A", 100)], {"A": 95.05}, D0, S, busy=set()))
acts = pf.decide(b, [W("A", 100)], {"A": 95.0}, D0, S, busy=set())
check("ADD at 95", acts and acts[0]["kind"] == "ADD")
check("ADD qty = 50% of reserve", acts[0]["qty"] == pf.floor_qty(200000, 95.0))
acts = pf.decide(b, [W("A", 100)], {"A": 95.0}, D0, S, busy=set(), reserve_busy=True)
check("one averaging order at a time", not acts)
pf.apply_buy(b, "A", pf.floor_qty(200000, 95.0), 95.0, "RESERVE", D0)
check("reserve used", near(b.reserve, 400000 - 2105 * 95.0))
check("adds counted", h.adds == 1)
check("avg cost lowered", h.avg < 100)
check("next trigger from new buy price", near(pf.avg_down_trigger(h, S), 90.25))
check("target follows avg cost", near(pf.target_price(h, "BULL", D0, S), pf.round_tick(1.15 * h.avg)))
acts = pf.decide(b, [W("A", 100)], {"A": 90.25}, D0, S, busy=set())
check("averages again (repeats)", acts and acts[0]["kind"] == "ADD")
s_max = copy.deepcopy(S)
s_max["avg_down_max_adds"] = 1
check("max adds respected", not pf.decide(b, [W("A", 100)], {"A": 90.25}, D0, s_max, busy=set()))

# sale returns reserve money in proportion
res_before, slot_before = b.reserve, b.slot(1).cash
res_part = h.reserve_cost / h.cost
qty = h.qty
pnl = pf.apply_sell(b, "A", qty, 110.0)
check("proportional reserve refund", near(b.reserve - res_before, qty * 110.0 * res_part, 0.05))
check("rest to slot", near(b.slot(1).cash - slot_before, qty * 110.0 * (1 - res_part), 0.05))
check("money conserved", near(b.reserve + b.slot_cash + b.invested(), 1_000_000 + b.realized, 0.05))

# ---------------------------------------------------------------- S-levels
b = fresh()
pf.apply_buy(b, "A", 1000, 100.0, "SLOT", D0, slot_id=1)
h = b.holdings["A"]
check("S1 20%", pf.s_level_qty(h, "S1", S) == 200)
check("S4 80%", pf.s_level_qty(h, "S4", S) == 800)
check("S5 100%", pf.s_level_qty(h, "S5", S) == 1000)
s_cfg = copy.deepcopy(S)
s_cfg["s_level_pct"]["S2"] = 50
check("S-level % configurable", pf.s_level_qty(h, "S2", s_cfg) == 500)
check("near cost applies", pf.s_level_applies(h, 100.9, S))
check("in profit does not apply", not pf.s_level_applies(h, 101.5, S))
acts = pf.decide(b, [W("A", 100, s="S2")], {"A": 98.0}, D0, S, busy=set())
check("S2 sells 40%", acts and acts[0]["kind"] == "SELL_SLEVEL" and acts[0]["qty"] == 400)
check("no S-level sell in profit", not [a for a in pf.decide(
    b, [W("A", 100, s="S2")], {"A": 104.0}, D0, S, busy=set()) if a["kind"] == "SELL_SLEVEL"])
pf.apply_sell(b, "A", 400, 98.0)
h.s_done = "S2"
check("S2 fires once", not pf.decide(b, [W("A", 100, s="S2")], {"A": 98.0}, D0, S, busy=set()))
acts = pf.decide(b, [W("A", 100, s="S3")], {"A": 98.0}, D0, S, busy=set())
check("new level fires again on current qty", acts and acts[0]["qty"] == 360)
check("partial sale keeps slot", b.slot(1).symbol == "A")
tiny = pf.Holding("T", 1, "BULL", D0.isoformat(), qty=3, cost=300, slot_cost=300, last_buy_price=100)
check("S1 on 3 shares sells at least 1", pf.s_level_qty(tiny, "S1", S) == 1)

# ---------------------------------------------------------------- kill switch
s_kill = copy.deepcopy(S)
s_kill["kill_switch"] = True
check("kill switch: no actions", pf.decide(fresh(), items, ltps, D0, s_kill, busy=set()) == [])
check("uninitialised book: no actions", pf.decide(pf.Book(), items, ltps, D0, S, busy=set()) == [])

# ---------------------------------------------------------------- persistence
b = fresh()
pf.apply_buy(b, "A", 10, 100.0, "SLOT", D0, slot_id=2)
b2 = pf.Book.from_dict(b.to_dict())
check("book round trip", b2.holdings["A"].qty == 10 and b2.slot(2).symbol == "A"
      and near(b2.reserve, b.reserve))

# ---------------------------------------------------------------- normalisers
check("mkt type BULL", pf.norm_market_type("bull") == "BULL")
check("mkt type BUY ON DIP", pf.norm_market_type("BUY ON DIP") == "BUY_ON_DIP")
check("mkt type SELL ON RALLY", pf.norm_market_type("Sell on rally") == "SELL_ON_RALLY")
check("mkt type note ignored", pf.norm_market_type("IF THIS COLUMN") == "")
check("s-level", pf.norm_s_level(" s3 ") == "S3")
check("s-level note ignored", pf.norm_s_level("DEPENDING UPON VALUES S1,S2,S3,") == "")

# ---------------------------------------------------------------- client sheet import
rows = [
    ["SL_NO", "SHARE_NAME", "TRADING_DATE", "SHARE_MKT_PRICE", "SHARE_BUYING_PRICE",
     "SHARE_PROFIT_OR_LOSS", "SHARE_BUY_DATE", "SHARE_BUY_DATE_MINUS_TRADE_DATE", "MKT_TYPE",
     "SHARE_SELL_P", "INDEX_FUTURE_BUY", "INDEX_FUTURE_BUY_LEVEL", "INDEX_FUTURE_SELL_LEVEL",
     "INDEX_FUTURE_FORCE_SELL", "SHARES_FORCE_SELL"],
    [None, None, None, None, None, 0],
    [1, "YES BANK", None, None, 17.6, -17.6, None, None, "BULL"],
    [2, "VMM", None, None, 93.7, -93.7, None, "IF THIS IS MORE THAN 30 DAYS", "BEAR", None,
     "IF THIS COLUMN IS YES", None, None, "IF THIS COLUMN VALUE IS YES",
     "DEPENDING UPON VALUES S1,S2,S3,"],
    [3, "OLA ELECTRIC", None, None, 32.6, -32.6, None, None, "BUY ON DIP"],
    [4, "ASHOK LEYLAND", None, None, 160.3, -160.3, None, None, "SELL ON RALLY", None, None,
     None, None, None, "S2"],
    [10, "KALYAN JEWELLERS", None, None, 630, -630],
]
got, notes = rows_to_items(rows)
check("5 shares imported", len(got) == 5)
check("names kept", got[0].name == "YES BANK" and got[0].symbol == "YESBANK")
check("buy price", near(got[1].buy_price, 93.7))
check("market types", [g.market_type for g in got] ==
      ["BULL", "BEAR", "BUY_ON_DIP", "SELL_ON_RALLY", "BULL"])
check("S-level only real values", [g.s_level for g in got] == ["", "", "", "S2", ""])

idx = {"YESBANK": {"tradingsymbol": "YESBANK", "name": "YES BANK"},
       "ASHOKLEY": {"tradingsymbol": "ASHOKLEY", "name": "ASHOK LEYLAND"},
       "OLAELEC": {"tradingsymbol": "OLAELEC", "name": "OLA ELECTRIC MOBILITY"},
       "WIPRO": {"tradingsymbol": "WIPRO", "name": "WIPRO"}}
check("resolve exact symbol", resolve(pf.WatchItem("WIPRO", 1), idx) == "WIPRO")
check("resolve by name", resolve(pf.WatchItem("ASHOKLEYLAND", 1, name="ASHOK LEYLAND"), idx) == "ASHOKLEY")
check("resolve by name prefix", resolve(pf.WatchItem("OLAELECTRIC", 1, name="OLA ELECTRIC"), idx) == "OLAELEC")
idx["WELCORP"] = {"tradingsymbol": "WELCORP", "name": "WELSPUN CORP"}
check("resolve truncated Kite name", resolve(pf.WatchItem("WELSPUNCORPORATION", 1,
                                                          name="WELSPUN CORPORATION"), idx) == "WELCORP")
check("unknown stays unresolved", resolve(pf.WatchItem("ZZZ", 1, name="NO SUCH CO"), idx) is None)

# ---------------------------------------------------------------- RSI
check("RSI needs 15 closes", rsi([1.0] * 10) is None)
check("RSI all up = 100", near(rsi([float(i) for i in range(1, 30)]), 100.0))
check("RSI all down = 0", near(rsi([float(i) for i in range(30, 1, -1)]), 0.0))
alt = [100 + (1 if i % 2 else -1) for i in range(40)]
check("RSI alternating ~50", 45 < rsi(alt) < 55)

# ---------------------------------------------------------------- time window
from datetime import datetime  # noqa: E402
mon = datetime(2026, 11, 23, 10, 0, tzinfo=config.IST)
check("window open Mon 10:00", config.in_window(mon, "09:15", "15:25"))
check("window shut 09:00", not config.in_window(mon.replace(hour=9, minute=0), "09:15", "15:25"))
check("window shut Saturday", not config.in_window(mon.replace(day=21), "09:15", "15:25"))

# ---------------------------------------------------------------- stock search
instruments._set([
    {"tradingsymbol": "YESBANK", "name": "YES BANK", "instrument_token": 1, "tick_size": 0.01},
    {"tradingsymbol": "ASHOKLEY", "name": "ASHOK LEYLAND", "instrument_token": 2, "tick_size": 0.05},
    {"tradingsymbol": "KALYANKJIL", "name": "KALYAN JEWELLERS INDIA", "instrument_token": 3, "tick_size": 0.05},
    {"tradingsymbol": "BANKBARODA", "name": "BANK OF BARODA", "instrument_token": 4, "tick_size": 0.05},
    {"tradingsymbol": "MAHABANK", "name": "BANK OF MAHARASHTRA", "instrument_token": 5, "tick_size": 0.01},
    {"tradingsymbol": "YESBANK-BE", "name": "YES BANK", "instrument_token": 6, "tick_size": 0.01},
    {"tradingsymbol": "CANBK", "name": "CANARA BANK", "instrument_token": 7, "tick_size": 0.01},
], "2026-11-21")
sy = lambda q: [r["tradingsymbol"] for r in instruments.search(q)]  # noqa: E731
check("search symbol prefix", sy("yes")[0] == "YESBANK")
check("main listing before -BE", sy("yes") == ["YESBANK", "YESBANK-BE"])
check("search company name", sy("ashok") == ["ASHOKLEY"])
check("search later word in name", sy("kalyan") == ["KALYANKJIL"])
check("search two words", sy("bank maha")[0] == "MAHABANK")
check("search 'bank of' finds both", set(sy("bank of")) == {"BANKBARODA", "MAHABANK"})
check("search contains", "CANBK" in sy("canara"))
check("exact symbol first", sy("canbk")[0] == "CANBK")
check("no match", sy("zzzz") == [])
check("empty query", sy("  ") == [])
check("get listed", instruments.get("CANBK")["name"] == "CANARA BANK")
check("get unlisted", instruments.get("NOPE") is None)

# ---------------------------------------------------------------- reconciliation
hold = [{"tradingsymbol": "A", "quantity": 100, "t1_quantity": 0, "used_quantity": 0},
        {"tradingsymbol": "B", "quantity": 60, "t1_quantity": 0, "used_quantity": 40},
        {"tradingsymbol": "C", "quantity": 0, "t1_quantity": 50, "used_quantity": 0}]
pos = [{"tradingsymbol": "B", "product": "CNC", "buy_quantity": 0, "sell_quantity": 40},
       {"tradingsymbol": "D", "product": "CNC", "buy_quantity": 30, "sell_quantity": 0},
       {"tradingsymbol": "E", "product": "CNC", "buy_quantity": 30, "sell_quantity": 10},
       {"tradingsymbol": "F", "product": "MIS", "buy_quantity": 99, "sell_quantity": 0}]
bq = rc.broker_quantities(hold, pos)
check("settled holding", bq["A"] == 100)
check("intraday sale of holding counted once", bq["B"] == 60)
check("T1 shares counted", bq["C"] == 50)
check("today's CNC buy counted", bq["D"] == 30)
check("today's buy partly sold", bq["E"] == 20)
check("MIS ignored", "F" not in bq)
rows = {r["symbol"]: r["state"] for r in rc.compare(
    {"A": 100, "B": 100, "C": 40, "D": 30, "G": 5}, bq, busy={"G"}, kept={})}
check("match OK", rows["A"] == "OK")
check("shortfall MISMATCH", rows["B"] == "MISMATCH")
check("extra shares EXTRA", rows["C"] == "EXTRA")
check("busy skipped", rows["G"] == "BUSY")
rows = {r["symbol"]: r["state"] for r in rc.compare({"B": 100}, bq, set(), {"B": 60})}
check("kept at same Zerodha qty", rows["B"] == "KEPT")
rows = {r["symbol"]: r["state"] for r in rc.compare({"B": 100}, {"B": 50}, set(), {"B": 60})}
check("kept but Zerodha changed again -> MISMATCH", rows["B"] == "MISMATCH")

# ---------------------------------------------------------------- applied order ids
b = fresh()
b.mark_applied("X1")
b.mark_applied("X1")
check("applied once", b.applied_orders == ["X1"] and b.was_applied("X1"))
check("not applied", not b.was_applied("X2") and not b.was_applied(""))
b.recon_kept["A"] = 5
b2 = pf.Book.from_dict(b.to_dict())
check("applied ids persisted", b2.was_applied("X1") and b2.recon_kept == {"A": 5})
old = pf.Book.from_dict({"capital": 1, "slots": [{"id": 1, "cash": 1}]})
check("old book files still load", old.applied_orders == [] and old.recon_kept == {})

# ---------------------------------------------------------------- Zerodha funds
m = {"enabled": True, "net": 160000.0,
     "available": {"cash": 100000.0, "live_balance": 95000.0, "collateral": 60000.0}}
check("live_balance used (not net with collateral)", funds.live_cash(m) == 95000.0)
check("falls back to cash", funds.live_cash({"available": {"cash": 5000}}) == 5000.0)
check("no margins -> None", funds.live_cash({}) is None)
now = 1_000_000.0
check("free = live - committed", funds.free_cash(95000, 20000, [], now=now) == 75000)
check("recent buy fills held back", funds.free_cash(95000, 0, [(now - 60, 30000)], now=now) == 65000)
check("old buy fills already in live_balance", funds.free_cash(95000, 0, [(now - 400, 30000)], now=now) == 95000)
check("unknown balance -> None", funds.free_cash(None, 0, [], now=now) is None)
check("LIMIT buy amount", funds.buy_amount({"qty": 100, "price": 50.0}, 0.5, False) == 5000)
check("MARKET buy amount with buffer", near(funds.buy_amount({"qty": 100, "price": 50.0}, 0.5, True), 5025))

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

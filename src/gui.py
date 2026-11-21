"""
gui.py
======
Dashboard for the Balfund Simple Algo Platform.

Tabs: Dashboard (book, slots, live watchlist, NIFTY), Watchlist (edit the
shares that replace the Excel sheet), Orders, Account (Kite login),
Settings (rules + Initialise portfolio), Log.

The engine runs on its own threads; the GUI only reads snapshots and log
lines from queues every 300 ms. When the engine is not running the book
is still shown from disk.
"""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

import config
import logger as logmod
import portfolio as pf
import watchlist
from broker import SESSION
from engine import Engine, book_path, load_book, save_book
from logger import logger

BG = "#0f1115"
CARD = "#171a21"
CARD2 = "#1d212a"
TEXT = "#e6e8ec"
SUB = "#8b93a3"
ACCENT = "#4f8cff"
OK = "#2ecc71"
WARN = "#ff5c5c"
AMBER = "#f5a623"
MUTED = "#3a4050"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def fmt(x, nd=2, dash="—"):
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else dash


def fmt_pnl(x):
    return f"{x:+,.2f}" if isinstance(x, (int, float)) else "—"


def make_tree(parent, cols, height=8):
    wrap = tk.Frame(parent, bg=CARD)
    tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings",
                        height=height, style="Dark.Treeview")
    for c, t, w in cols:
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="center")
    tree.tag_configure("profit", foreground="#7ee2a8")
    tree.tag_configure("loss", foreground="#ff9a9a")
    tree.tag_configure("busy", foreground="#ffe0a3")
    tree.tag_configure("dim", foreground="#5d6577")
    tree.tag_configure("ready", foreground="#c7dbff")
    tree.tag_configure("bad", foreground=WARN)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return wrap, tree


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{config.APP_NAME} v{config.APP_VERSION} — Balfund")
        self.geometry("1500x960")
        self.minsize(1200, 760)
        self.configure(fg_color=BG)

        self.engine = None
        self.snap = None
        self.logq = queue.Queue()
        self.snapq = queue.Queue(maxsize=5)
        logmod.add_gui_sink(self.logq.put)

        self._style_tree()
        self._build_header()
        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=ACCENT)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_dashboard(self.tabs.add("Dashboard"))
        self._build_watchlist(self.tabs.add("Watchlist"))
        self._build_orders(self.tabs.add("Orders"))
        self._build_account(self.tabs.add("Account"))
        self._build_settings(self.tabs.add("Settings"))
        self._build_log(self.tabs.add("Log"))

        self._refresh_account()
        self._refresh_watch_editor()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._pump)
        self.after(250, self._tick_clock)
        self.after(500, self._idle_refresh)
        logger.info("%s v%s ready. Mode: %s", config.APP_NAME, config.APP_VERSION,
                    config.SETTINGS["mode"])

    # ------------------------------------------------------------ styling
    def _style_tree(self):
        st = ttk.Style()
        st.theme_use("default")
        st.configure("Dark.Treeview", background=CARD2, fieldbackground=CARD2,
                     foreground=TEXT, rowheight=22, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Dark.Treeview.Heading", background=CARD, foreground=SUB,
                     relief="flat", font=("Segoe UI", 10, "bold"))
        st.map("Dark.Treeview", background=[("selected", "#2b3550")])

    # ------------------------------------------------------------ header
    def _build_header(self):
        h = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        h.pack(fill="x")
        left = ctk.CTkFrame(h, fg_color="transparent")
        left.pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(left, text="SIMPLE ALGO PLATFORM", font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(left, text="  Zerodha Kite · NSE delivery · N slots + reserve",
                     text_color=SUB).pack(side="left")
        self.lbl_mode = ctk.CTkLabel(left, text="", corner_radius=6,
                                     font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_mode.pack(side="left", padx=(14, 6))
        self.lbl_status = ctk.CTkLabel(left, text=" IDLE ", fg_color=MUTED, corner_radius=6,
                                       font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_status.pack(side="left", padx=6)

        right = ctk.CTkFrame(h, fg_color="transparent")
        right.pack(side="right", padx=16)
        self.lbl_clock = ctk.CTkLabel(right, text="", font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_clock.pack(side="right", padx=(14, 0))
        self.btn_kill = ctk.CTkButton(right, text="", width=170, command=self._toggle_kill)
        self.btn_kill.pack(side="right", padx=4)
        self.btn_stop = ctk.CTkButton(right, text="Stop", width=80, fg_color=MUTED,
                                      state="disabled", command=self._stop)
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ctk.CTkButton(right, text="Start", width=90, fg_color=OK,
                                       hover_color="#27ae60", text_color="#0b1a10",
                                       command=self._start)
        self.btn_start.pack(side="right", padx=4)

        strip = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        strip.pack(fill="x", padx=16, pady=(4, 0))
        self.lbl_feed = ctk.CTkLabel(strip, text="WS: offline", text_color=SUB, anchor="w")
        self.lbl_feed.pack(side="left")
        self.lbl_nifty = ctk.CTkLabel(strip, text="NIFTY 50: —", text_color=TEXT, anchor="e")
        self.lbl_nifty.pack(side="right")
        self._paint_mode()
        self._paint_kill()

    def _paint_mode(self):
        m = config.SETTINGS["mode"]
        self.lbl_mode.configure(text=f" {m} ", fg_color=WARN if m == "LIVE" else AMBER,
                                text_color="#111")

    def _paint_kill(self):
        on = bool(config.SETTINGS.get("kill_switch"))
        self.btn_kill.configure(
            text="KILL SWITCH: ON" if on else "KILL SWITCH: off",
            fg_color=WARN if on else MUTED, hover_color="#d84343" if on else "#4a5163")

    # ------------------------------------------------------------ dashboard
    def _stat(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        ctk.CTkLabel(f, text=title, text_color=SUB).pack(anchor="w", padx=14, pady=(10, 0))
        v = ctk.CTkLabel(f, text="—", font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT)
        v.pack(anchor="w", padx=14, pady=(0, 10))
        return f, v

    def _build_dashboard(self, tab):
        stats = ctk.CTkFrame(tab, fg_color="transparent")
        stats.pack(fill="x", pady=(4, 8))
        self.stat = {}
        for key, title in (("capital", "Capital"), ("value", "Holdings value"),
                           ("slot_cash", "Slot cash free"), ("reserve", "Reserve (averaging)"),
                           ("realized", "Realized P/L"), ("unrealized", "Unrealized P/L")):
            f, v = self._stat(stats, title)
            f.pack(side="left", fill="x", expand=True, padx=4)
            self.stat[key] = v

        top = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        top.pack(fill="both", expand=True, padx=4)
        ctk.CTkLabel(top, text="Slots & holdings", text_color=SUB, anchor="w").pack(
            fill="x", padx=14, pady=(8, 2))
        cols = [("slot", "Slot", 45), ("symbol", "Share", 120), ("type", "Type", 110),
                ("qty", "Qty", 70), ("avg", "Avg cost", 90), ("ltp", "LTP", 90),
                ("pnl", "P/L", 100), ("pct", "P/L %", 70), ("target", "Target", 90),
                ("age", "Age (d)", 65), ("steps", "Aged x", 60), ("trig", "Avg-down at", 95),
                ("adds", "Adds", 50), ("slv", "S-level", 75), ("cash", "Slot cash", 100)]
        wrap, self.hold_tree = make_tree(top, cols, height=8)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        bot = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        bot.pack(fill="both", expand=True, pady=(8, 0), padx=4)
        ctk.CTkLabel(bot, text="Watchlist (live)", text_color=SUB, anchor="w").pack(
            fill="x", padx=14, pady=(8, 2))
        cols = [("symbol", "Share", 120), ("buy", "Buy price", 90), ("type", "Type", 110),
                ("ltp", "LTP", 90), ("dist", "vs buy %", 75), ("open", "Open", 85),
                ("high", "High", 85), ("low", "Low", 85), ("prev", "Prev close", 90),
                ("rsi", "RSI 14", 65), ("slv", "S-level", 65), ("state", "State", 130)]
        wrap, self.live_tree = make_tree(bot, cols, height=9)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ------------------------------------------------------------ watchlist
    def _build_watchlist(self, tab):
        box = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        box.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(box, text="Shares in priority order. A free slot buys the first one whose "
                               "price reaches its buy price. Edits apply on the next second, "
                               "no restart needed.",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        cols = [("n", "#", 40), ("symbol", "NSE symbol", 130), ("name", "Name (sheet)", 220),
                ("buy", "Buy price", 100), ("type", "Market type", 130),
                ("slv", "S-level", 80), ("en", "Enabled", 80)]
        wrap, self.wl_tree = make_tree(box, cols, height=14)
        wrap.pack(fill="both", expand=True, padx=14, pady=4)
        self.wl_tree.bind("<<TreeviewSelect>>", self._wl_select)

        form = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        form.pack(fill="x", pady=8)
        self.wl_vars = {"symbol": tk.StringVar(), "name": tk.StringVar(),
                        "buy": tk.StringVar(), "type": tk.StringVar(value="BULL"),
                        "slv": tk.StringVar(value="—"), "en": tk.BooleanVar(value=True)}
        self._wl_editing = None
        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 4))
        for label, key, w in (("NSE symbol", "symbol", 130), ("Name", "name", 200),
                              ("Buy price", "buy", 100)):
            ctk.CTkLabel(row, text=label, text_color=SUB).pack(side="left", padx=(0, 6))
            ctk.CTkEntry(row, textvariable=self.wl_vars[key], width=w).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(row, text="Market type", text_color=SUB).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(row, variable=self.wl_vars["type"], values=list(config.MARKET_TYPES),
                          width=150).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(row, text="S-level", text_color=SUB).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(row, variable=self.wl_vars["slv"], values=["—"] + list(config.S_LEVELS),
                          width=80).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(row, text="Enabled", variable=self.wl_vars["en"]).pack(side="left")

        btns = ctk.CTkFrame(form, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(4, 12))
        ctk.CTkButton(btns, text="Add / Update", width=120, command=self._wl_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="New", width=70, fg_color=MUTED, command=self._wl_clear).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Remove", width=90, fg_color=MUTED, command=self._wl_remove).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="▲", width=40, fg_color=MUTED,
                      command=lambda: self._wl_move(-1)).pack(side="left", padx=(16, 2))
        ctk.CTkButton(btns, text="▼", width=40, fg_color=MUTED,
                      command=lambda: self._wl_move(1)).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Import Excel / CSV", width=150,
                      command=self._wl_import).pack(side="left", padx=(24, 4))
        ctk.CTkButton(btns, text="Export", width=80, fg_color=MUTED,
                      command=self._wl_export).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Resolve NSE symbols", width=160, fg_color=MUTED,
                      command=self._wl_resolve).pack(side="left", padx=4)
        self.slv_all = tk.StringVar(value="—")
        ctk.CTkLabel(btns, text="   S-level for all:", text_color=SUB).pack(side="left")
        ctk.CTkOptionMenu(btns, variable=self.slv_all, values=["—"] + list(config.S_LEVELS),
                          width=80).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Apply", width=70, fg_color=AMBER, text_color="#111",
                      command=self._wl_slv_all).pack(side="left", padx=4)

    def _refresh_watch_editor(self):
        self.wl_tree.delete(*self.wl_tree.get_children())
        for i, w in enumerate(watchlist.items(), 1):
            self.wl_tree.insert("", "end", iid=w.symbol, tags=(() if w.enabled else ("dim",)),
                                values=(i, w.symbol, w.name, fmt(w.buy_price), w.market_type,
                                        w.s_level or "—", "yes" if w.enabled else "no"))

    def _wl_select(self, _=None):
        sel = self.wl_tree.selection()
        if not sel:
            return
        for w in watchlist.items():
            if w.symbol == sel[0]:
                self._wl_editing = w.symbol
                self.wl_vars["symbol"].set(w.symbol)
                self.wl_vars["name"].set(w.name)
                self.wl_vars["buy"].set(f"{w.buy_price:g}")
                self.wl_vars["type"].set(w.market_type)
                self.wl_vars["slv"].set(w.s_level or "—")
                self.wl_vars["en"].set(w.enabled)

    def _wl_clear(self):
        self._wl_editing = None
        for k in ("symbol", "name", "buy"):
            self.wl_vars[k].set("")
        self.wl_vars["type"].set("BULL")
        self.wl_vars["slv"].set("—")
        self.wl_vars["en"].set(True)
        self.wl_tree.selection_remove(self.wl_tree.selection())

    def _wl_save(self):
        sym = watchlist.symbolize(self.wl_vars["symbol"].get())
        if not sym:
            messagebox.showerror("Watchlist", "NSE symbol is required")
            return
        try:
            buy = float(self.wl_vars["buy"].get())
            if buy <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Watchlist", "Buy price must be a number above 0")
            return
        if sym != self._wl_editing and sym in {w.symbol for w in watchlist.items()}:
            messagebox.showerror("Watchlist", f"{sym} is already in the list")
            return
        slv = self.wl_vars["slv"].get()
        item = pf.WatchItem(symbol=sym, buy_price=buy, market_type=self.wl_vars["type"].get(),
                            s_level="" if slv == "—" else slv, enabled=bool(self.wl_vars["en"].get()),
                            name=self.wl_vars["name"].get().strip() or sym)
        watchlist.upsert(item, old_symbol=self._wl_editing)
        self._wl_editing = sym
        self._refresh_watch_editor()
        logger.info("Watchlist: %s saved (buy %.2f, %s, S-level %s)", sym, buy,
                    item.market_type, item.s_level or "none")

    def _wl_remove(self):
        sel = self.wl_tree.selection()
        if not sel:
            return
        held = sel[0] in load_book(config.SETTINGS["mode"]).holdings
        msg = f"Remove {sel[0]}?" + ("\n\nIt is held: the holding is still managed "
                                     "(target / ageing / averaging) with its stored market type."
                                     if held else "")
        if messagebox.askyesno("Remove", msg):
            watchlist.remove(sel[0])
            self._wl_clear()
            self._refresh_watch_editor()

    def _wl_move(self, d):
        sel = self.wl_tree.selection()
        if sel:
            watchlist.move(sel[0], d)
            self._refresh_watch_editor()
            self.wl_tree.selection_set(sel[0])

    def _wl_import(self):
        path = filedialog.askopenfilename(filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv")])
        if not path:
            return
        try:
            new, notes = watchlist.read_file(path)
        except Exception as e:
            messagebox.showerror("Import", str(e))
            return
        if not new:
            messagebox.showwarning("Import", "No rows with a share name and a buy price found")
            return
        cur = watchlist.items()
        replace = messagebox.askyesnocancel(
            "Import", f"{len(new)} shares found.\n\nYes = replace the watchlist\n"
                      f"No = add / update into the current list")
        if replace is None:
            return
        if replace:
            merged = new
        else:
            by = {w.symbol: w for w in cur}
            for w in new:
                by[w.symbol] = w
            merged = list(by.values())
        watchlist.save(merged)
        if SESSION.logged_in:
            self._wl_resolve(quiet=True)
        self._refresh_watch_editor()
        logger.info("Imported %d shares from %s", len(new), path)
        for n in notes:
            logger.warning("Import: %s", n)

    def _wl_export(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if path:
            watchlist.export_file(path)
            logger.info("Watchlist exported to %s", path)

    def _wl_resolve(self, quiet=False):
        if not SESSION.logged_in:
            messagebox.showinfo("Resolve", "Log in on the Account tab first")
            return

        def work():
            try:
                idx = watchlist.build_index(SESSION.kite.instruments("NSE"))
                changed, missing = watchlist.resolve_all(idx)
                for old, new in changed:
                    logger.info("Resolved %s -> %s", old, new)
                for m in missing:
                    logger.error("Not found on NSE: %s (type the NSE symbol yourself)", m)
                if not changed and not missing:
                    logger.info("All watchlist symbols are valid NSE symbols")
            except Exception as e:
                logger.error("Resolve failed: %s", e)
            self.after(0, self._refresh_watch_editor)
        threading.Thread(target=work, daemon=True).start()

    def _wl_slv_all(self):
        lvl = self.slv_all.get()
        txt = "clear the S-level on every share" if lvl == "—" else \
            f"set {lvl} ({config.SETTINGS['s_level_pct'][lvl]:g}%) on every share"
        if messagebox.askyesno("S-level", f"This will {txt}. Holdings at a loss or near cost "
                                          f"will be sold accordingly. Continue?"):
            watchlist.set_s_level_all("" if lvl == "—" else lvl)
            self._refresh_watch_editor()
            logger.warning("S-level %s applied to all shares", lvl)

    # ------------------------------------------------------------ orders
    def _build_orders(self, tab):
        box = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        box.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(box, text="Orders this session (all are also in logs/orders_<date>.csv; "
                               "fills in logs/trades_<date>.csv)",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        cols = [("time", "Time", 75), ("kind", "Kind", 110), ("symbol", "Share", 120),
                ("txn", "Side", 60), ("type", "Type", 75), ("qty", "Qty", 70),
                ("price", "Price", 90), ("oid", "Order id", 160), ("status", "Status", 110),
                ("filled", "Filled", 65), ("avg", "Avg", 90), ("msg", "Message", 300)]
        wrap, self.ord_tree = make_tree(box, cols, height=25)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ------------------------------------------------------------ account
    def _build_account(self, tab):
        form = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        form.pack(fill="x", pady=4)
        acct = config.SETTINGS["account"]
        self.acc_vars = {"api_key": tk.StringVar(value=acct.get("api_key", "")),
                         "api_secret": tk.StringVar(value=acct.get("api_secret", ""))}
        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(14, 6))
        for label, key, w, show in (("API key", "api_key", 240, ""),
                                    ("API secret", "api_secret", 300, "•")):
            ctk.CTkLabel(row, text=label, text_color=SUB).pack(side="left", padx=(0, 6))
            ctk.CTkEntry(row, textvariable=self.acc_vars[key], width=w, show=show).pack(
                side="left", padx=(0, 14))
        btns = ctk.CTkFrame(form, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(6, 6))
        ctk.CTkButton(btns, text="Save", width=90, command=self._acc_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Login", width=100, fg_color=OK, text_color="#0b1a10",
                      command=lambda: self._acc_login(False)).pack(side="left", padx=(24, 4))
        ctk.CTkButton(btns, text="Force fresh login", width=140, fg_color=MUTED,
                      command=lambda: self._acc_login(True)).pack(side="left", padx=4)
        self.lbl_acc = ctk.CTkLabel(form, text="not logged in", text_color=SUB, anchor="w",
                                    font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_acc.pack(fill="x", padx=14, pady=(8, 4))
        port = config.SETTINGS["redirect_port"]
        host = config.SETTINGS["redirect_host"]
        ctk.CTkLabel(form, text=f"Redirect URL on developers.kite.trade must be exactly  "
                                f"http://{host}:{port}/   ·   Login is once per day; the token is cached.\n"
                                f"LIVE orders need the static IP registered on your Kite app.",
                     text_color=SUB, anchor="w", justify="left").pack(fill="x", padx=14, pady=(0, 14))

    def _refresh_account(self):
        if SESSION.logged_in:
            self.lbl_acc.configure(text=f"LOGGED IN · {SESSION.user_id}", text_color=OK)
        elif SESSION.error:
            self.lbl_acc.configure(text=f"ERROR · {SESSION.error[:90]}", text_color=WARN)
        else:
            self.lbl_acc.configure(text="not logged in", text_color=SUB)

    def _acc_save(self):
        config.SETTINGS["account"] = {"name": "main",
                                      "api_key": self.acc_vars["api_key"].get().strip(),
                                      "api_secret": self.acc_vars["api_secret"].get().strip()}
        config.save_settings(config.SETTINGS)
        SESSION.kite = None
        self._refresh_account()
        logger.info("Kite API key saved")

    def _acc_login(self, force):
        self._acc_save()

        def work():
            try:
                SESSION.login(force=force)
            except Exception:
                pass
            self.after(0, self._refresh_account)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ settings
    def _build_settings(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color=CARD, corner_radius=12)
        box.pack(fill="both", expand=True, pady=4)
        self.set_vars = {}

        def get(key):
            if "." in key:
                a, b = key.split(".")
                return config.SETTINGS[a][b]
            return config.SETTINGS[key]

        def section(title):
            ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                         text_color=ACCENT, anchor="w").pack(fill="x", padx=16, pady=(14, 4))

        def field(label, key, hint="", kind="float", options=None):
            r = ctk.CTkFrame(box, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=3)
            ctk.CTkLabel(r, text=label, width=300, anchor="w", text_color=TEXT).pack(side="left")
            val = get(key)
            if kind == "bool":
                v = tk.BooleanVar(value=bool(val))
                ctk.CTkSwitch(r, text="", variable=v).pack(side="left")
            elif kind == "option":
                v = tk.StringVar(value=str(val))
                ctk.CTkSegmentedButton(r, values=options, variable=v).pack(side="left")
            elif kind == "menu":
                v = tk.StringVar(value=str(val))
                ctk.CTkOptionMenu(r, values=options, variable=v, width=120).pack(side="left")
            else:
                if isinstance(val, float):
                    txt = str(int(val)) if val.is_integer() else f"{val:.10g}"
                else:
                    txt = str(val)
                v = tk.StringVar(value=txt)
                ctk.CTkEntry(r, textvariable=v, width=160).pack(side="left")
            if hint:
                ctk.CTkLabel(r, text=hint, text_color=SUB, anchor="w").pack(side="left", padx=12)
            self.set_vars[key] = (v, kind)

        section("Mode")
        field("Trading mode", "mode", "PAPER simulates fills · LIVE sends real CNC orders",
              kind="option", options=["PAPER", "LIVE"])

        section("Capital  ·  applied by 'Initialise portfolio' below")
        field("Initial investment (₹)", "initial_investment")
        field("Equity share of capital (%)", "equity_pct", "split equally across N slots; "
              "the rest is the averaging reserve")
        field("Number of shares N", "num_slots", "one share per slot", kind="menu",
              options=[str(i) for i in range(1, 21)])
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=(6, 3))
        ctk.CTkButton(r, text="Initialise portfolio", width=180, fg_color=AMBER, text_color="#111",
                      command=self._init_book).pack(side="left")
        self.lbl_book = ctk.CTkLabel(r, text="", text_color=SUB, anchor="w")
        self.lbl_book.pack(side="left", padx=12)

        section("Entry")
        field("Entry order type", "entry_order_type", "LIMIT at the buy price, or MARKET",
              kind="option", options=["LIMIT", "MARKET"])
        field("Entry tolerance (%)", "entry_tolerance_pct", "buy when LTP <= buy price x (1 + tol%)")
        field("MARKET slippage buffer (%)", "market_buffer_pct", "qty sized for this much slippage")

        section("Targets  ·  sell price = multiplier x average cost")
        for mt in config.MARKET_TYPES:
            field(f"Target multiplier {mt}", f"target_mult.{mt}")

        section("Ageing  ·  unsold for every N days -> target x factor (repeats)")
        field("Ageing period (days)", "ageing_days", kind="int")
        for mt in config.MARKET_TYPES:
            field(f"Ageing factor {mt}", f"ageing_factor.{mt}")

        section("Averaging down  ·  from the reserve")
        field("Trigger drop (%)", "avg_down_drop_pct", "LTP this far below the last buy price")
        field("Use % of free reserve", "avg_down_reserve_pct")
        field("Max adds per holding", "avg_down_max_adds", "0 = unlimited", kind="int")

        section("S-levels  ·  % of the holding sold")
        for lv in config.S_LEVELS:
            field(f"{lv} (%)", f"s_level_pct.{lv}")
        field("Applies when LTP <= avg cost + (%)", "s_level_near_pct",
              "only holdings at a loss or close to cost")

        section("Exits & orders")
        field("Exit order type", "exit_order_type", "LIMIT at LTP, or MARKET",
              kind="option", options=["LIMIT", "MARKET"])
        field("Cancel unfilled LIMIT after (s)", "limit_order_timeout_s", kind="int")
        field("Market protection", "market_protection", "-1 = Kite automatic, 1–100 = %", kind="int")

        section("Timing (IST)")
        field("Trading start (HH:MM)", "trade_start", kind="time")
        field("Trading end (HH:MM)", "trade_end", kind="time")
        field("Ignore market hours (PAPER only)", "ignore_market_hours", kind="bool")

        section("Kite login")
        field("Redirect host", "redirect_host", kind="str")
        field("Redirect port", "redirect_port", kind="int")

        ctk.CTkButton(box, text="Save settings", width=160, command=self._save_settings).pack(
            anchor="w", padx=16, pady=16)
        self._paint_book_label()

    def _paint_book_label(self):
        b = load_book(config.SETTINGS["mode"])
        if b.initialised:
            self.lbl_book.configure(
                text=f"{config.SETTINGS['mode']} book: capital {b.capital:,.0f} · {len(b.slots)} slots · "
                     f"reserve {b.reserve:,.0f} · {len(b.holdings)} holdings · since {b.created}")
        else:
            self.lbl_book.configure(text=f"{config.SETTINGS['mode']} book not initialised yet")

    def _collect(self):
        new = {}
        for key, (v, kind) in self.set_vars.items():
            val = v.get()
            if kind == "float":
                val = float(val)
            elif kind in ("int", "menu"):
                val = int(float(val))
            elif kind == "time":
                config.parse_hhmm(val)
            new[key] = val
        return new

    def _save_settings(self):
        s = config.SETTINGS
        try:
            new = self._collect()
        except Exception as e:
            messagebox.showerror("Settings", f"Invalid value: {e}")
            return
        if not (0 < new["equity_pct"] <= 100):
            messagebox.showerror("Settings", "Equity % must be between 0 and 100")
            return
        if new["mode"] != s["mode"] and self.engine and self.engine.running:
            messagebox.showerror("Settings", "Stop the engine before changing PAPER / LIVE")
            return
        if new["mode"] == "LIVE" and s["mode"] != "LIVE":
            if not messagebox.askyesno("LIVE mode", "LIVE sends real CNC orders to Zerodha from "
                                                    "your registered static IP. Continue?"):
                return
        for key, val in new.items():
            if "." in key:
                a, b = key.split(".")
                s[a][b] = val
            else:
                s[key] = val
        config.save_settings(s)
        self._paint_mode()
        self._paint_book_label()
        logger.info("Settings saved (mode %s). Rule changes apply on the next second.", s["mode"])

    def _init_book(self):
        if self.engine and self.engine.running:
            messagebox.showerror("Initialise", "Stop the engine first")
            return
        try:
            new = self._collect()
        except Exception as e:
            messagebox.showerror("Initialise", f"Invalid value: {e}")
            return
        mode = config.SETTINGS["mode"]
        cap, eq, n = new["initial_investment"], new["equity_pct"], new["num_slots"]
        old = load_book(mode)
        if old.holdings:
            messagebox.showerror("Initialise", f"The {mode} book still has {len(old.holdings)} "
                                               "holdings. It can only be re-initialised when empty.")
            return
        per = cap * eq / 100 / n
        if not messagebox.askyesno(
                "Initialise portfolio",
                f"{mode} book:\n\nCapital ₹{cap:,.0f}\n{n} slots x ₹{per:,.0f} = ₹{cap * eq / 100:,.0f} "
                f"({eq:g}%)\nReserve ₹{cap * (100 - eq) / 100:,.0f}\n\n"
                + ("This replaces the current empty book. " if old.initialised else "")
                + "Continue?"):
            return
        for k in ("initial_investment", "equity_pct", "num_slots"):
            config.SETTINGS[k] = new[k]
        config.save_settings(config.SETTINGS)
        save_book(mode, pf.new_book(cap, eq, n, config.now_ist().date()))
        logger.info("%s portfolio initialised: capital %.0f, %d slots of %.0f, reserve %.0f (%s)",
                    mode, cap, n, per, cap * (100 - eq) / 100, book_path(mode).name)
        self._paint_book_label()

    # ------------------------------------------------------------ log
    def _build_log(self, tab):
        self.txt = ctk.CTkTextbox(tab, fg_color=CARD, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(tab, text=f"Logs folder: {config.LOG_DIR}   ·   State folder: {config.STATE_DIR}",
                     text_color=SUB, anchor="w").pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------------ controls
    def _start(self):
        if self.engine and self.engine.running:
            return
        if not SESSION.logged_in:
            messagebox.showwarning("Start", "Log in first (Account tab).")
            return
        if not load_book(config.SETTINGS["mode"]).initialised:
            messagebox.showwarning("Start", "Initialise the portfolio first (Settings tab).")
            return
        if config.SETTINGS["mode"] == "LIVE" and not messagebox.askyesno(
                "LIVE", "Start in LIVE mode? Real CNC orders will be placed."):
            return
        self.engine = Engine(status_cb=self._on_snap)
        self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.tabs.set("Dashboard")

    def _stop(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _toggle_kill(self):
        on = not bool(config.SETTINGS.get("kill_switch"))
        if on and not messagebox.askyesno("Kill switch", "Stop ALL trading now?\n\nNo new orders; "
                                                         "working orders are cancelled. Holdings "
                                                         "are NOT sold."):
            return
        if not on and not messagebox.askyesno("Kill switch", "Release the kill switch and resume trading?"):
            return
        config.SETTINGS["kill_switch"] = on
        config.save_settings(config.SETTINGS)
        self._paint_kill()
        logger.warning("Kill switch %s", "ON" if on else "released")

    def _on_close(self):
        if self.engine and self.engine.running:
            if not messagebox.askyesno("Exit", "Engine is running. Exit anyway?"):
                return
            self.engine.stop()
        self.destroy()

    # ------------------------------------------------------------ rendering
    def _on_snap(self, snap):
        try:
            self.snapq.put_nowait(snap)
        except queue.Full:
            pass

    def _idle_refresh(self):
        """Engine not running: show the book from disk."""
        if not (self.engine and self.engine.running):
            try:
                self._render(Engine().snapshot(), idle=True)
            except Exception as e:
                logger.debug("idle refresh: %s", e)
        self.after(3000, self._idle_refresh)

    def _pump(self):
        lines = []
        while not self.logq.empty():
            lines.append(self.logq.get_nowait())
        if lines:
            self.txt.insert("end", "\n".join(lines) + "\n")
            self.txt.see("end")
        snap = None
        while not self.snapq.empty():
            snap = self.snapq.get_nowait()
        if snap:
            self.snap = snap
            self._render(snap)
        self.after(300, self._pump)

    def _tick_clock(self):
        self.lbl_clock.configure(text=config.now_ist().strftime("%H:%M:%S IST"))
        self.after(250, self._tick_clock)

    def _render(self, s, idle=False):
        st = "IDLE" if idle else s["status"]
        color = {"RUNNING": OK, "KILLED": WARN, "ERROR": WARN, "MARKET CLOSED": ACCENT,
                 "STOPPED": MUTED, "IDLE": MUTED}.get(st, AMBER)
        self.lbl_status.configure(text=f" {st} ", fg_color=color, text_color="#111")
        self._paint_kill()
        if not idle:
            self.lbl_feed.configure(
                text=f"WS: {'live' if s['feed'] else 'offline'} · {s['ticks']:,} ticks   |   "
                     f"Broker check: {s['broker_check']}",
                text_color=(WARN if s["broker_check"] == "MISMATCH" else OK if s["feed"] else SUB))
            n = s.get("nifty") or {}
            if n.get("ltp"):
                self.lbl_nifty.configure(
                    text=f"NIFTY 50  {fmt(n['ltp'])}   O {fmt(n['open'])}  H {fmt(n['high'])}  "
                         f"L {fmt(n['low'])}  PC {fmt(n['prev'])}   RSI {fmt(n['rsi'], 1)}")
            if st in ("STOPPED", "ERROR"):
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")

        self.stat["capital"].configure(text=fmt(s["capital"], 0))
        self.stat["value"].configure(text=fmt(s["value"], 0))
        self.stat["slot_cash"].configure(text=fmt(s["slot_cash"], 0))
        self.stat["reserve"].configure(text=fmt(s["reserve"], 0))
        for k in ("realized", "unrealized"):
            v = s[k]
            self.stat[k].configure(text=fmt_pnl(v), text_color=OK if v > 0 else WARN if v < 0 else TEXT)

        self.hold_tree.delete(*self.hold_tree.get_children())
        for h in s["holdings"]:
            if not h["symbol"]:
                self.hold_tree.insert("", "end", tags=("dim",), values=(
                    h["slot"], "— free —", "", "", "", "", "", "", "", "", "", "", "", "",
                    fmt(h["cash"], 0)))
                continue
            tag = "busy" if h["busy"] else ("profit" if (h["pnl"] or 0) > 0 else
                                            "loss" if (h["pnl"] or 0) < 0 else "")
            slv = h["s_level"] or "—"
            if h["s_done"]:
                slv += f" (done {h['s_done']})"
            self.hold_tree.insert("", "end", tags=(tag,), values=(
                h["slot"], h["symbol"], h["type"], h["qty"], fmt(h["avg"]), fmt(h["ltp"]),
                fmt_pnl(h["pnl"]), fmt(h["pnl_pct"], 1), fmt(h["target"]), h["age"],
                h["steps"], fmt(h["avg_trigger"], dash="max"), h["adds"], slv, fmt(h["cash"], 0)))

        self.live_tree.delete(*self.live_tree.get_children())
        for w in s["watch"]:
            tag = {"HELD": "profit", "READY": "ready", "ORDER": "busy", "OFF": "dim",
                   "NOT FOUND": "bad"}.get(w["state"], "")
            self.live_tree.insert("", "end", tags=((tag,) if tag else ()), values=(
                w["symbol"], fmt(w["buy"]), w["type"], fmt(w["ltp"]), fmt(w["dist"], 1),
                fmt(w["open"]), fmt(w["high"]), fmt(w["low"]), fmt(w["prev"]),
                fmt(w["rsi"], 1), w["s_level"] or "—", w["state"]))

        if not idle:
            self.ord_tree.delete(*self.ord_tree.get_children())
            for o in s["orders"]:
                tag = "profit" if o["status"] == "COMPLETE" else \
                    "loss" if o["status"] in ("REJECTED", "CANCELLED") else "busy"
                self.ord_tree.insert("", "end", tags=(tag,), values=(
                    o["time"], o["kind"], o["symbol"], o["txn"], o["type"], o["qty"],
                    fmt(o["price"], dash="MKT"), o["order_id"], o["status"], o["filled"],
                    fmt(o["avg"]), o["msg"][:80]))


def run():
    App().mainloop()


if __name__ == "__main__":
    run()

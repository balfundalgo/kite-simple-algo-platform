"""
gui.py
======
Light-theme desktop app for the Balfund Simple Algo Platform.

Tabs: Dashboard (book, slots, live watchlist, NIFTY), Watchlist (type-ahead
stock search + the rules per share), Orders, Reconcile (book vs Zerodha,
Sync / Keep for frozen shares), Account (Kite login), Settings, Log.

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
import funds
import instruments
import logger as logmod
import portfolio as pf
import watchlist
from broker import SESSION
from engine import Engine, book_path, load_book, save_book
from logger import logger

# ---------------------------------------------------------------- light palette
BG = "#f3f5f9"
CARD = "#ffffff"
CARD2 = "#ffffff"
LINE = "#e3e7ee"
TEXT = "#1f2937"
SUB = "#6b7280"
ACCENT = "#2563eb"
OK = "#16a34a"
WARN = "#dc2626"
AMBER = "#d97706"
MUTED = "#e5e7eb"
MUTED_HOVER = "#d1d5db"
HEAD = "#eef2f7"
SELECT = "#dbeafe"

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


def fmt(x, nd=2, dash="—"):
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else dash


def fmt_pnl(x):
    return f"{x:+,.2f}" if isinstance(x, (int, float)) else "—"


def button(parent, text, command, kind="primary", width=110):
    colors = {
        "primary": (ACCENT, "#1d4ed8", "#ffffff"),
        "muted": (MUTED, MUTED_HOVER, TEXT),
        "ok": (OK, "#15803d", "#ffffff"),
        "warn": (WARN, "#b91c1c", "#ffffff"),
        "amber": (AMBER, "#b45309", "#ffffff"),
    }[kind]
    return ctk.CTkButton(parent, text=text, command=command, width=width, fg_color=colors[0],
                         hover_color=colors[1], text_color=colors[2],
                         text_color_disabled="#9ca3af")


def make_tree(parent, cols, height=8):
    wrap = tk.Frame(parent, bg=CARD)
    tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings",
                        height=height, style="Light.Treeview")
    for c, t, w in cols:
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="center")
    tree.tag_configure("profit", foreground="#15803d")
    tree.tag_configure("loss", foreground="#b91c1c")
    tree.tag_configure("busy", foreground="#b45309")
    tree.tag_configure("dim", foreground="#9ca3af")
    tree.tag_configure("ready", foreground="#1d4ed8")
    tree.tag_configure("bad", foreground="#ffffff", background="#ef4444")
    tree.tag_configure("odd", background="#f9fafb")
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return wrap, tree


# ======================================================================
# type-ahead stock search
# ======================================================================
class SearchBox(ctk.CTkFrame):
    """
    Entry with a drop-down of matching NSE shares (symbol or company name).
    Arrow keys + Enter or mouse click pick a share; only picked / exact
    listed symbols are accepted by symbol().
    """
    MAX_ROWS = 12

    def __init__(self, master, on_pick, width=320):
        super().__init__(master, fg_color="transparent")
        self.on_pick = on_pick
        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(self, textvariable=self.var, width=width,
                                  placeholder_text="Type symbol or company name…")
        self.entry.pack(side="left")
        self.results = []
        self.selected = None
        self.lb = None
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", self._to_list)
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<FocusOut>", lambda e: self.after(200, self._maybe_hide))

    # -- public --------------------------------------------------------------
    def symbol(self):
        """The chosen NSE symbol, or None if the text is not a listed share."""
        if self.selected:
            return self.selected["tradingsymbol"]
        txt = self.var.get().strip().upper().split(" ")[0] if self.var.get().strip() else ""
        return txt if txt and instruments.get(txt) else None

    def set_symbol(self, sym):
        row = instruments.get(sym)
        self.selected = row
        self.var.set(sym)
        self.hide()

    def clear(self):
        self.selected = None
        self.var.set("")
        self.hide()

    # -- internals -------------------------------------------------------------
    def _on_key(self, e):
        if e.keysym in ("Up", "Down", "Return", "Escape", "Tab", "Shift_L", "Shift_R"):
            return
        self.selected = None
        q = self.var.get()
        self.results = instruments.search(q, 30) if q.strip() else []
        if q.strip() and not instruments.count():
            self.results = []
            self._show(["Stock list not loaded yet — log in once (Account tab)"], info=True)
            return
        if q.strip() and not self.results:
            self._show(["No NSE share matches"], info=True)
            return
        self._show([f"{r['tradingsymbol']:<14} {r['name']}" for r in self.results])

    def _show(self, lines, info=False):
        if not lines:
            self.hide()
            return
        top = self.winfo_toplevel()
        if self.lb is None:
            self.lb = tk.Listbox(top, font=("Consolas", 11), activestyle="none",
                                 bg="#ffffff", fg=TEXT, selectbackground=SELECT,
                                 selectforeground=TEXT, relief="solid", bd=1,
                                 highlightthickness=0, exportselection=False)
            self.lb.bind("<ButtonRelease-1>", lambda e: self._pick_selected())
            self.lb.bind("<Return>", lambda e: self._pick_selected())
            self.lb.bind("<Escape>", lambda e: (self.hide(), self.entry.focus_set()))
            self.lb.bind("<Up>", self._list_up)
            self.lb.bind("<FocusOut>", lambda e: self.after(200, self._maybe_hide))
        self._info = info
        self.lb.delete(0, "end")
        for ln in lines:
            self.lb.insert("end", ln)
        self.lb.configure(height=min(len(lines), self.MAX_ROWS),
                          fg=SUB if info else TEXT)
        top.update_idletasks()
        x = self.entry.winfo_rootx() - top.winfo_rootx()
        y = self.entry.winfo_rooty() - top.winfo_rooty() + self.entry.winfo_height() + 2
        self.lb.place(x=x, y=y, width=max(self.entry.winfo_width(), 460))
        self.lb.lift()

    def hide(self):
        if self.lb is not None:
            self.lb.place_forget()

    def _maybe_hide(self):
        f = self.focus_get()
        if f is not self.lb:
            self.hide()

    def _to_list(self, e=None):
        if self.lb is not None and self.lb.winfo_ismapped() and self.results:
            self.lb.focus_set()
            self.lb.selection_clear(0, "end")
            self.lb.selection_set(0)
            self.lb.activate(0)
        return "break"

    def _list_up(self, e=None):
        sel = self.lb.curselection()
        if sel and sel[0] == 0:
            self.entry.focus_set()
            return "break"

    def _enter(self, e=None):
        if not self.results:
            return "break"
        q = self.var.get().strip().upper()
        exact = next((r for r in self.results if r["tradingsymbol"] == q), None)
        self._pick(exact or self.results[0])
        return "break"

    def _pick_selected(self):
        if getattr(self, "_info", False):
            return
        sel = self.lb.curselection()
        if sel and sel[0] < len(self.results):
            self._pick(self.results[sel[0]])

    def _pick(self, row):
        self.selected = row
        self.var.set(row["tradingsymbol"])
        self.hide()
        self.entry.focus_set()
        self.entry.icursor("end")
        self.on_pick(row)


# ======================================================================
# app
# ======================================================================
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
        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=ACCENT,
                                   segmented_button_fg_color=MUTED,
                                   segmented_button_unselected_color=MUTED,
                                   segmented_button_unselected_hover_color=MUTED_HOVER,
                                   text_color=TEXT)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_dashboard(self.tabs.add("Dashboard"))
        self._build_watchlist(self.tabs.add("Watchlist"))
        self._build_orders(self.tabs.add("Orders"))
        self._build_reconcile(self.tabs.add("Reconcile"))
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
        if instruments.count():
            logger.info("Stock list: %d NSE shares (from %s)", instruments.count(),
                        instruments.cache_date())
        else:
            threading.Thread(target=self._public_instruments, daemon=True).start()

    # ------------------------------------------------------------ styling
    def _style_tree(self):
        st = ttk.Style()
        st.theme_use("default")
        st.configure("Light.Treeview", background=CARD2, fieldbackground=CARD2,
                     foreground=TEXT, rowheight=24, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Light.Treeview.Heading", background=HEAD, foreground=SUB,
                     relief="flat", font=("Segoe UI", 10, "bold"))
        st.map("Light.Treeview", background=[("selected", SELECT)],
               foreground=[("selected", TEXT)])
        st.map("Light.Treeview.Heading", background=[("active", LINE)])

    # ------------------------------------------------------------ header
    def _build_header(self):
        h = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64,
                         border_width=1, border_color=LINE)
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
        self.lbl_clock = ctk.CTkLabel(right, text="", text_color=TEXT,
                                      font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_clock.pack(side="right", padx=(14, 0))
        self.btn_kill = button(right, "", self._toggle_kill, "muted", 170)
        self.btn_kill.pack(side="right", padx=4)
        self.btn_stop = button(right, "Stop", self._stop, "muted", 80)
        self.btn_stop.configure(state="disabled")
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = button(right, "Start", self._start, "ok", 90)
        self.btn_start.pack(side="right", padx=4)

        strip = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        strip.pack(fill="x", padx=16, pady=(6, 0))
        self.lbl_feed = ctk.CTkLabel(strip, text="WS: offline", text_color=SUB, anchor="w")
        self.lbl_feed.pack(side="left")
        self.lbl_recon = ctk.CTkLabel(strip, text="", text_color=SUB, anchor="w")
        self.lbl_recon.pack(side="left", padx=24)
        self.lbl_nifty = ctk.CTkLabel(strip, text="NIFTY 50: —", text_color=TEXT, anchor="e")
        self.lbl_nifty.pack(side="right")
        self._paint_mode()
        self._paint_kill()

    def _paint_mode(self):
        m = config.SETTINGS["mode"]
        self.lbl_mode.configure(text=f" {m} ", fg_color=WARN if m == "LIVE" else AMBER,
                                text_color="#ffffff")

    def _paint_kill(self):
        on = bool(config.SETTINGS.get("kill_switch"))
        self.btn_kill.configure(
            text="KILL SWITCH: ON" if on else "KILL SWITCH: off",
            fg_color=WARN if on else MUTED, hover_color="#b91c1c" if on else MUTED_HOVER,
            text_color="#ffffff" if on else TEXT)

    # ------------------------------------------------------------ dashboard
    def _card(self, parent, **kw):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=1,
                            border_color=LINE, **kw)

    def _stat(self, parent, title):
        f = self._card(parent)
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
                           ("realized", "Realized P/L"), ("unrealized", "Unrealized P/L"),
                           ("zfree", "Zerodha free cash")):
            f, v = self._stat(stats, title)
            f.pack(side="left", fill="x", expand=True, padx=4)
            self.stat[key] = v

        top = self._card(tab)
        top.pack(fill="both", expand=True, padx=4)
        ctk.CTkLabel(top, text="Slots & holdings", text_color=SUB, anchor="w").pack(
            fill="x", padx=14, pady=(8, 2))
        cols = [("slot", "Slot", 45), ("symbol", "Share", 115), ("type", "Type", 110),
                ("qty", "Qty", 65), ("avg", "Avg cost", 85), ("ltp", "LTP", 85),
                ("pnl", "P/L", 95), ("pct", "P/L %", 65), ("target", "Target", 85),
                ("age", "Age (d)", 60), ("steps", "Aged x", 55), ("trig", "Avg-down at", 90),
                ("adds", "Adds", 45), ("slv", "S-level", 75), ("cash", "Slot cash", 90),
                ("st", "Status", 85)]
        wrap, self.hold_tree = make_tree(top, cols, height=8)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        ctk.CTkLabel(top, text="Right-click a holding to set its S-level.", text_color=SUB,
                     anchor="w").pack(fill="x", padx=14, pady=(0, 8))
        self.hold_tree.bind("<Button-3>", self._hold_menu)
        self.hold_tree.bind("<Button-2>", self._hold_menu)        # macOS trackpad

        bot = self._card(tab)
        bot.pack(fill="both", expand=True, pady=(8, 0), padx=4)
        ctk.CTkLabel(bot, text="Watchlist (live)", text_color=SUB, anchor="w").pack(
            fill="x", padx=14, pady=(8, 2))
        cols = [("symbol", "Share", 120), ("buy", "Buy price", 90), ("type", "Type", 110),
                ("ltp", "LTP", 90), ("dist", "vs buy %", 75), ("open", "Open", 85),
                ("high", "High", 85), ("low", "Low", 85), ("prev", "Prev close", 90),
                ("rsi", "RSI 14", 65), ("slv", "S-level", 65), ("state", "State", 130)]
        wrap, self.live_tree = make_tree(bot, cols, height=9)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def _hold_menu(self, event):
        sym = self.hold_tree.identify_row(event.y)
        if not sym or sym.startswith("slot-"):
            return
        self.hold_tree.selection_set(sym)
        menu = tk.Menu(self, tearoff=0, bg="#ffffff", fg=TEXT, activebackground=SELECT,
                       activeforeground=TEXT)
        menu.add_command(label=f"S-level for {sym}", state="disabled")
        menu.add_separator()
        menu.add_command(label="—  (none)", command=lambda: self._set_s_level(sym, ""))
        for lv in config.S_LEVELS:
            pct = config.SETTINGS["s_level_pct"][lv]
            menu.add_command(label=f"{lv}  sell {pct:g}%",
                             command=lambda lv=lv: self._set_s_level(sym, lv))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_s_level(self, sym, level):
        items = watchlist.items()
        item = next((w for w in items if w.symbol == sym), None)
        if item is None:
            messagebox.showinfo("S-level", f"{sym} is no longer in the watchlist. Add it back "
                                           "(Watchlist tab) to set an S-level for it.")
            return
        if level:
            pct = config.SETTINGS["s_level_pct"][level]
            if not messagebox.askyesno(
                    "S-level", f"Set {level} on {sym}?\n\nIt sells {pct:g}% of the holding "
                               f"while it is at a loss or within "
                               f"{config.SETTINGS['s_level_near_pct']:g}% of cost."):
                return
        item.s_level = level
        watchlist.upsert(item)
        self._refresh_watch_editor()
        logger.warning("S-level for %s set to %s", sym, level or "none")

    # ------------------------------------------------------------ watchlist
    def _build_watchlist(self, tab):
        form = self._card(tab)
        form.pack(fill="x", pady=(4, 8))
        self.wl_vars = {"name": tk.StringVar(), "buy": tk.StringVar(),
                        "type": tk.StringVar(value="BULL"), "slv": tk.StringVar(value="—"),
                        "en": tk.BooleanVar(value=True)}
        self._wl_editing = None

        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(14, 4))
        ctk.CTkLabel(row, text="Search share", text_color=SUB).pack(side="left", padx=(0, 8))
        self.search = SearchBox(row, self._on_search_pick, width=320)
        self.search.pack(side="left")
        self.lbl_pick = ctk.CTkLabel(row, text="", text_color=TEXT, anchor="w",
                                     font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_pick.pack(side="left", padx=14)
        self.lbl_count = ctk.CTkLabel(row, text="", text_color=SUB, anchor="e")
        self.lbl_count.pack(side="right")

        row2 = ctk.CTkFrame(form, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(6, 4))
        ctk.CTkLabel(row2, text="Buy price", text_color=SUB).pack(side="left", padx=(0, 6))
        ctk.CTkEntry(row2, textvariable=self.wl_vars["buy"], width=110).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(row2, text="Market type", text_color=SUB).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(row2, variable=self.wl_vars["type"], values=list(config.MARKET_TYPES),
                          width=160).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(row2, text="S-level", text_color=SUB).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(row2, variable=self.wl_vars["slv"], values=["—"] + list(config.S_LEVELS),
                          width=80).pack(side="left", padx=(0, 16))
        ctk.CTkCheckBox(row2, text="Enabled", variable=self.wl_vars["en"],
                        text_color=TEXT).pack(side="left")

        btns = ctk.CTkFrame(form, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(8, 14))
        button(btns, "Add / Update", self._wl_save, "primary", 120).pack(side="left", padx=4)
        button(btns, "New", self._wl_clear, "muted", 70).pack(side="left", padx=4)
        button(btns, "Remove", self._wl_remove, "muted", 90).pack(side="left", padx=4)
        button(btns, "▲", lambda: self._wl_move(-1), "muted", 40).pack(side="left", padx=(16, 2))
        button(btns, "▼", lambda: self._wl_move(1), "muted", 40).pack(side="left", padx=2)
        button(btns, "Import Excel / CSV", self._wl_import, "primary", 150).pack(side="left", padx=(24, 4))
        button(btns, "Export", self._wl_export, "muted", 80).pack(side="left", padx=4)
        button(btns, "Update stock list", self._wl_refresh_list, "muted", 140).pack(side="left", padx=4)
        self.slv_all = tk.StringVar(value="—")
        ctk.CTkLabel(btns, text="   S-level for all:", text_color=SUB).pack(side="left")
        ctk.CTkOptionMenu(btns, variable=self.slv_all, values=["—"] + list(config.S_LEVELS),
                          width=80).pack(side="left", padx=4)
        button(btns, "Apply", self._wl_slv_all, "amber", 70).pack(side="left", padx=4)

        box = self._card(tab)
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Shares in priority order. A free slot buys the first one whose "
                               "price reaches its buy price. Red rows are not NSE shares: select "
                               "the row and pick the right share in the search box.",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        cols = [("n", "#", 40), ("symbol", "NSE symbol", 130), ("name", "Company", 260),
                ("buy", "Buy price", 100), ("type", "Market type", 130),
                ("slv", "S-level", 80), ("en", "Enabled", 80)]
        wrap, self.wl_tree = make_tree(box, cols, height=14)
        wrap.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.wl_tree.bind("<<TreeviewSelect>>", self._wl_select)

    def _on_search_pick(self, row):
        self.wl_vars["name"].set(row["name"])
        self.lbl_pick.configure(text=f"{row['tradingsymbol']} · {row['name']}")
        if SESSION.logged_in:
            sym = row["tradingsymbol"]

            def work():
                try:
                    ltp = SESSION.kite.ltp([f"NSE:{sym}"])[f"NSE:{sym}"]["last_price"]
                    self.after(0, lambda: self.lbl_pick.configure(
                        text=f"{sym} · {row['name']} · LTP {ltp:,.2f}"))
                    if not self.wl_vars["buy"].get().strip():
                        self.after(0, lambda: self.wl_vars["buy"].set(f"{ltp:g}"))
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

    def _refresh_watch_editor(self):
        idx = instruments.index()
        self.wl_tree.delete(*self.wl_tree.get_children())
        for i, w in enumerate(watchlist.items(), 1):
            bad = bool(idx) and w.symbol not in idx
            tags = ("bad",) if bad else (("dim",) if not w.enabled else (("odd",) if i % 2 == 0 else ()))
            name = (idx.get(w.symbol) or {}).get("name") or w.name
            if bad:
                name = f"NOT FOUND ({w.name or w.symbol}) — pick with search"
            self.wl_tree.insert("", "end", iid=w.symbol, tags=tags,
                                values=(i, w.symbol, name, fmt(w.buy_price), w.market_type,
                                        w.s_level or "—", "yes" if w.enabled else "no"))
        self.lbl_count.configure(
            text=(f"Stock list: {instruments.count():,} NSE shares ({instruments.cache_date()})"
                  if instruments.count() else "Stock list not loaded — log in once"))

    def _wl_select(self, _=None):
        sel = self.wl_tree.selection()
        if not sel:
            return
        for w in watchlist.items():
            if w.symbol == sel[0]:
                self._wl_editing = w.symbol
                row = instruments.get(w.symbol)
                if row:
                    self.search.set_symbol(w.symbol)
                    self.lbl_pick.configure(text=f"{w.symbol} · {row['name']}")
                else:
                    self.search.clear()
                    self.search.var.set(w.name or w.symbol)
                    self.lbl_pick.configure(text="not an NSE share — pick from the list",
                                            text_color=WARN)
                    self.after(2500, lambda: self.lbl_pick.configure(text_color=TEXT))
                self.wl_vars["name"].set(w.name)
                self.wl_vars["buy"].set(f"{w.buy_price:g}")
                self.wl_vars["type"].set(w.market_type)
                self.wl_vars["slv"].set(w.s_level or "—")
                self.wl_vars["en"].set(w.enabled)

    def _wl_clear(self):
        self._wl_editing = None
        self.search.clear()
        self.lbl_pick.configure(text="")
        self.wl_vars["name"].set("")
        self.wl_vars["buy"].set("")
        self.wl_vars["type"].set("BULL")
        self.wl_vars["slv"].set("—")
        self.wl_vars["en"].set(True)
        self.wl_tree.selection_remove(self.wl_tree.selection())

    def _wl_save(self):
        if not instruments.count():
            messagebox.showerror("Watchlist", "The NSE stock list is not loaded yet. Log in once "
                                              "on the Account tab; it is then kept on this PC.")
            return
        sym = self.search.symbol()
        if not sym:
            messagebox.showerror("Watchlist", "Pick a share from the search list "
                                              "(type part of the symbol or company name).")
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
                            name=instruments.get(sym)["name"] or sym)
        watchlist.upsert(item, old_symbol=self._wl_editing)
        self._wl_editing = sym
        self._refresh_watch_editor()
        if self.wl_tree.exists(sym):
            self.wl_tree.selection_set(sym)
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
        logger.info("Imported %d shares from %s", len(new), path)
        for n in notes:
            logger.warning("Import: %s", n)
        self._resolve_watchlist()

    def _resolve_watchlist(self):
        idx = instruments.index()
        if idx:
            changed, missing = watchlist.resolve_all(idx)
            for old, new in changed:
                logger.info("Matched %s -> %s (%s)", old, new, idx[new]["name"])
            for m in missing:
                logger.error("Not found on NSE: %s — select the red row and pick the share "
                             "with the search box", m)
        self._refresh_watch_editor()

    def _wl_export(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if path:
            watchlist.export_file(path)
            logger.info("Watchlist exported to %s", path)

    def _wl_refresh_list(self):
        def work():
            try:
                if SESSION.logged_in:
                    n = instruments.refresh(SESSION.kite, force=True)
                else:
                    n = instruments.refresh_public(config.SETTINGS["account"].get("api_key", ""))
                logger.info("Stock list updated: %d NSE shares", n)
            except Exception as e:
                logger.error("Stock list download failed: %s", e)
            self.after(0, self._resolve_watchlist)
        threading.Thread(target=work, daemon=True).start()

    def _public_instruments(self):
        try:
            n = instruments.refresh_public(config.SETTINGS["account"].get("api_key", ""))
            logger.info("Stock list downloaded: %d NSE shares", n)
            self.after(0, self._resolve_watchlist)
        except Exception as e:
            logger.info("Stock list will be downloaded after login (%s)", e)

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
        box = self._card(tab)
        box.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(box, text="Orders this session, including any recovered from the journal at "
                               "start (also in logs/orders_<date>.csv; fills in logs/trades_<date>.csv)",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        cols = [("time", "Time", 75), ("kind", "Kind", 110), ("symbol", "Share", 120),
                ("txn", "Side", 60), ("type", "Type", 75), ("qty", "Qty", 70),
                ("price", "Price", 90), ("oid", "Order id", 160), ("status", "Status", 120),
                ("filled", "Filled", 65), ("avg", "Avg", 90), ("msg", "Message", 300)]
        wrap, self.ord_tree = make_tree(box, cols, height=25)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ------------------------------------------------------------ reconcile
    def _build_reconcile(self, tab):
        box = self._card(tab)
        box.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(box, text="Book vs Zerodha (holdings + today's CNC positions), checked at "
                               "every start and every few seconds while running in LIVE.",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(12, 2))
        ctk.CTkLabel(box, text="OK = match · EXTRA = Zerodha holds more (your own shares, not "
                               "managed) · MISMATCH = Zerodha holds LESS than the book: the share "
                               "is FROZEN (no orders) until you choose:",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkLabel(box, text="   Sync to Zerodha — the book takes Zerodha's quantity; the "
                               "shortfall is booked as sold (today's sell price, else LTP). "
                               "If nothing is left, the slot is freed.\n"
                               "   Keep book — you confirm the book is right for this Zerodha "
                               "quantity (e.g. shares pledged/moved); trading resumes and it "
                               "re-freezes if Zerodha's quantity changes again.",
                     text_color=TEXT, anchor="w", justify="left").pack(fill="x", padx=14, pady=(0, 8))
        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(0, 6))
        self.lbl_recon_big = ctk.CTkLabel(top, text="Not checked yet (Start in LIVE mode)",
                                          text_color=TEXT, font=ctk.CTkFont(size=15, weight="bold"))
        self.lbl_recon_big.pack(side="left")
        button(top, "Keep book", self._recon_keep, "muted", 110).pack(side="right", padx=4)
        button(top, "Sync to Zerodha", self._recon_sync, "amber", 150).pack(side="right", padx=4)
        button(top, "Check now", self._recon_check, "primary", 110).pack(side="right", padx=4)
        cols = [("symbol", "Share", 160), ("book", "Book qty", 120), ("zerodha", "Zerodha qty", 120),
                ("diff", "Difference", 120), ("state", "State", 140)]
        wrap, self.recon_tree = make_tree(box, cols, height=16)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def _engine_live(self):
        if not (self.engine and self.engine.running and self.engine.live):
            messagebox.showinfo("Reconcile", "Reconciliation runs while the engine is running in LIVE mode.")
            return False
        return True

    def _recon_check(self):
        if self._engine_live():
            self.engine.reconcile_now()

    def _recon_selected(self):
        sel = self.recon_tree.selection()
        if not sel:
            messagebox.showinfo("Reconcile", "Select a share first")
            return None
        return sel[0]

    def _recon_sync(self):
        if not self._engine_live():
            return
        sym = self._recon_selected()
        if not sym:
            return
        row = next((r for r in (self.snap or {}).get("recon", []) if r["symbol"] == sym), None)
        if row and row["zerodha"] >= row["book"]:
            messagebox.showinfo("Reconcile", f"{sym} is already in line with Zerodha")
            return
        if messagebox.askyesno("Sync to Zerodha",
                               f"{sym}: set the book to Zerodha's quantity"
                               + (f" ({row['zerodha']}, book {row['book']})" if row else "")
                               + "?\n\nThe shortfall is booked as sold."):
            threading.Thread(target=self.engine.recon_sync, args=(sym,), daemon=True).start()

    def _recon_keep(self):
        if not self._engine_live():
            return
        sym = self._recon_selected()
        if sym and messagebox.askyesno(
                "Keep book", f"Keep the book quantity for {sym} although Zerodha shows less?\n\n"
                             "Orders for it resume. Sell orders may be rejected by Zerodha if "
                             "the shares are really not there."):
            self.engine.recon_keep(sym)

    # ------------------------------------------------------------ account
    def _build_account(self, tab):
        form = self._card(tab)
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
        button(btns, "Save", self._acc_save, "primary", 90).pack(side="left", padx=4)
        button(btns, "Login", lambda: self._acc_login(False), "ok", 100).pack(side="left", padx=(24, 4))
        button(btns, "Force fresh login", lambda: self._acc_login(True), "muted", 140).pack(side="left", padx=4)
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
                try:
                    n = instruments.refresh(SESSION.kite)
                    logger.info("Stock list: %d NSE shares (%s)", n, instruments.cache_date())
                except Exception as e:
                    logger.warning("Stock list download failed: %s", e)
                self.after(0, self._resolve_watchlist)
            except Exception:
                pass
            self.after(0, self._refresh_account)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ settings
    def _build_settings(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color=CARD, corner_radius=12, border_width=1,
                                     border_color=LINE)
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
        button(r, "Initialise portfolio", self._init_book, "amber", 180).pack(side="left")
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

        section("Zerodha reconciliation (LIVE)")
        field("Check holdings every (s)", "reconcile_seconds", "minimum 15", kind="int")

        section("Timing (IST)")
        field("Trading start (HH:MM)", "trade_start", kind="time")
        field("Trading end (HH:MM)", "trade_end", kind="time")
        field("Ignore market hours (PAPER only)", "ignore_market_hours", kind="bool")

        section("Kite login")
        field("Redirect host", "redirect_host", kind="str")
        field("Redirect port", "redirect_port", kind="int")

        button(box, "Save settings", self._save_settings, "primary", 160).pack(
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
        cash_note = ""
        if mode == "LIVE":
            if not SESSION.logged_in:
                messagebox.showerror("Initialise", "LIVE: log in first (Account tab) so the "
                                                   "capital can be checked against your Zerodha cash.")
                return
            try:
                zcash = funds.live_cash(SESSION.kite.margins("equity"))
            except Exception as e:
                messagebox.showerror("Initialise", f"Could not read your Zerodha funds: {e}")
                return
            if zcash is None:
                messagebox.showerror("Initialise", "Zerodha did not return a cash balance.")
                return
            if cap > zcash:
                messagebox.showerror(
                    "Initialise", f"Capital ₹{cap:,.0f} is more than your Zerodha free cash "
                                  f"₹{zcash:,.0f}.\n\nLower the initial investment to at most "
                                  f"₹{zcash:,.0f} (or add funds in Zerodha).")
                return
            cash_note = f"Zerodha free cash ₹{zcash:,.0f} (OK)\n\n"
        if not messagebox.askyesno(
                "Initialise portfolio",
                f"{mode} book:\n\nCapital ₹{cap:,.0f}\n{n} slots x ₹{per:,.0f} = ₹{cap * eq / 100:,.0f} "
                f"({eq:g}%)\nReserve ₹{cap * (100 - eq) / 100:,.0f}\n\n"
                + cash_note
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
        self.txt = ctk.CTkTextbox(tab, fg_color=CARD, text_color=TEXT, border_width=1,
                                  border_color=LINE, font=ctk.CTkFont(family="Consolas", size=12))
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
            if not messagebox.askyesno("Exit", "Engine is running. Exit anyway?\n\nThe book is "
                                               "saved; working orders are settled on the next start."):
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
                 "STOPPED": "#9ca3af", "IDLE": "#9ca3af"}.get(st, AMBER)
        self.lbl_status.configure(text=f" {st} ", fg_color=color, text_color="#ffffff")
        self._paint_kill()
        if not idle:
            self.lbl_feed.configure(
                text=f"WS: {'live' if s['feed'] else 'offline'} · {s['ticks']:,} ticks · "
                     f"working orders {s['working']}",
                text_color=OK if s["feed"] else SUB)
            rs = s["recon_state"]
            rtxt = f"Zerodha sync: {rs}" + (f" @ {s['recon_time']}" if s["recon_time"] else "")
            if s.get("zcash") is not None:
                rtxt += f"   ·   Zerodha cash {s['zcash']:,.0f} @ {s['funds_time']}"
            self.lbl_recon.configure(text=rtxt, text_color=(WARN if rs.startswith("MISMATCH")
                                                            or rs == "CHECK FAILED" else
                                                            OK if rs == "OK" else SUB))
            self.lbl_recon_big.configure(text=rtxt + (f"   ·   FROZEN: {', '.join(s['frozen'])}"
                                                      if s["frozen"] else ""),
                                         text_color=WARN if s["frozen"] else TEXT)
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
        if idle or s.get("mode") != "LIVE":
            self.stat["zfree"].configure(text="paper" if s.get("mode") == "PAPER" and not idle
                                         else "—", text_color=SUB)
        else:
            zf = s.get("zfree")
            self.stat["zfree"].configure(
                text=fmt(zf, 0) if zf is not None else s.get("funds_state", "—"),
                text_color=(WARN if zf is not None and zf < 0 else TEXT) if zf is not None else WARN)

        hsel = self.hold_tree.selection()
        self.hold_tree.delete(*self.hold_tree.get_children())
        for h in s["holdings"]:
            if not h["symbol"]:
                self.hold_tree.insert("", "end", iid=f"slot-{h['slot']}", tags=("dim",), values=(
                    h["slot"], "— free —", "", "", "", "", "", "", "", "", "", "", "", "",
                    fmt(h["cash"], 0), ""))
                continue
            if h["frozen"]:
                tag, status = "bad", "FROZEN"
            elif h["busy"]:
                tag, status = "busy", "ORDER"
            else:
                tag = "profit" if (h["pnl"] or 0) > 0 else "loss" if (h["pnl"] or 0) < 0 else ""
                status = "managed"
            slv = h["s_level"] or "—"
            if h["s_done"]:
                slv += f" (done {h['s_done']})"
            self.hold_tree.insert("", "end", iid=h["symbol"], tags=((tag,) if tag else ()), values=(
                h["slot"], h["symbol"], h["type"], h["qty"], fmt(h["avg"]), fmt(h["ltp"]),
                fmt_pnl(h["pnl"]), fmt(h["pnl_pct"], 1), fmt(h["target"]), h["age"],
                h["steps"], fmt(h["avg_trigger"], dash="max"), h["adds"], slv,
                fmt(h["cash"], 0), status))
        for x in hsel:
            if self.hold_tree.exists(x):
                self.hold_tree.selection_set(x)

        self.live_tree.delete(*self.live_tree.get_children())
        for i, w in enumerate(s["watch"]):
            tag = {"HELD": "profit", "READY": "ready", "ORDER": "busy", "OFF": "dim",
                   "NOT FOUND": "bad", "FROZEN": "bad"}.get(w["state"], "odd" if i % 2 else "")
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

            sel = self.recon_tree.selection()
            self.recon_tree.delete(*self.recon_tree.get_children())
            for r in s["recon"]:
                tag = {"MISMATCH": "bad", "OK": "profit", "EXTRA": "ready", "KEPT": "busy",
                       "BUSY": "dim", "CHECKING": "busy"}.get(r["state"], "")
                self.recon_tree.insert("", "end", iid=r["symbol"], tags=((tag,) if tag else ()),
                                       values=(r["symbol"], r["book"], r["zerodha"],
                                               f"{r['diff']:+d}", r["state"]))
            for x in sel:
                if self.recon_tree.exists(x):
                    self.recon_tree.selection_set(x)


def run():
    App().mainloop()


if __name__ == "__main__":
    run()

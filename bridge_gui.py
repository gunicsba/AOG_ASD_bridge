"""
Window for the AOG-ASD bridge (the default; --console keeps the old text mode).

Shows the terminal / AgIO connection, the base rate, the sections AgOpenGPS
commands with the rate sent to / reported by each side, and the log. The
operator picks the COM port, sets the base rate, changes settings and
exports logs here. The bridge itself runs in the core module's threads; the
window only polls its state. Texts come from lang/<code>.ini (see i18n.py).
"""

import collections
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font
from tkinter import filedialog, messagebox, ttk

import serial.tools.list_ports

import i18n
import log_archive

POLL_MS = 250
LOG_LINES = 1000
MAX_RATE = 5000.0

FONT = "Segoe UI"
MONO = "Consolas"

BG = "#eef1f4"
CARD = "#ffffff"
BORDER = "#d5dae0"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"

GREEN, GREEN_BG, GREEN_FG = "#16a34a", "#dcfce7", "#166534"
AMBER_BG, AMBER_FG = "#fef3c7", "#92400e"
RED, RED_BG, RED_FG = "#dc2626", "#fee2e2", "#991b1b"
BLUE_BG, BLUE_FG = "#dbeafe", "#1e40af"
GRAY_BG, GRAY_FG = "#e5e7eb", "#4b5563"

TONES = {
    "ok": (GREEN_BG, GREEN_FG),
    "warn": (AMBER_BG, AMBER_FG),
    "error": (RED_BG, RED_FG),
    "info": (BLUE_BG, BLUE_FG),
    "off": (GRAY_BG, GRAY_FG),
}


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def fmt_rate(v) -> str:
    if v is None:
        return "–"
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.1f}"


class QueueLogHandler(logging.Handler):
    """Hands INFO+ records to the window (Tk must only be touched from its
    own thread)."""

    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait((record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


class Pill(tk.Label):
    def __init__(self, master):
        super().__init__(master, font=(FONT, 9, "bold"), padx=8, pady=2)

    def show(self, text: str, tone: str):
        bg, fg = TONES[tone]
        text = f"●  {text}"
        if self.cget("text") != text or self.cget("bg") != bg:
            self.config(text=text, bg=bg, fg=fg)


class Card(tk.Frame):
    def __init__(self, master, title: str = ""):
        super().__init__(master, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        if title:
            tk.Label(self, text=title.upper(), bg=CARD, fg=MUTED,
                     font=(FONT, 8, "bold")).pack(anchor="w", padx=12, pady=(8, 0))


class SectionBar(tk.Canvas):
    """Section boxes. For the Amados split into a left and a right side, each
    with the rate sent to it (→) and the rate the terminal reports (←)."""

    def __init__(self, master, scale: float, T):
        self.s, self.T = scale, T
        super().__init__(master, bg=CARD, highlightthickness=0, height=int(48 * scale))
        self.state = None
        self.bind("<Configure>", lambda e: self._draw())

    def show(self, count: int, mask: int, per_side, live: bool, sides=None):
        """sides: [(sent, reported), (sent, reported)] texts or None."""
        state = (count, mask, per_side, live, tuple(sides or ()))
        if state != self.state:
            self.state = state
            want = int((112 if per_side else 48) * self.s)
            if int(self.cget("height")) != want:
                self.config(height=want)
            self._draw()

    def _draw(self):
        self.delete("all")
        if not self.state or not self.state[0]:
            return
        count, mask, per_side, live, sides = self.state
        s, T = self.s, self.T
        w = self.winfo_width()
        margin, gap, side_gap = 12 * s, 5 * s, (24 * s if per_side else 0)
        box_w = (w - 2 * margin - gap * (count - 1) - side_gap) / count
        top, box_h = (20 * s if per_side else 6 * s), 34 * s
        x = margin
        starts = []
        for i in range(count):
            if per_side and i == per_side:
                x += side_gap
            if per_side and i % per_side == 0:
                starts.append(x)
            if mask >> i & 1:
                fill, outline, fg = (GREEN, GREEN, "#ffffff") if live else \
                                    (GREEN_BG, GREEN, GREEN_FG)
            else:
                fill, outline, fg = GRAY_BG, BORDER, GRAY_FG
            self.create_rectangle(x, top, x + box_w, top + box_h, fill=fill,
                                  outline=outline, width=max(1, int(s)))
            self.create_text(x + box_w / 2, top + box_h / 2, text=str(i + 1),
                             fill=fg, font=(FONT, 10, "bold"))
            x += box_w + gap
        if not per_side:
            return
        side_w = per_side * box_w + (per_side - 1) * gap
        for n, sx in enumerate(starts[:2]):
            cx = sx + side_w / 2
            self.create_text(cx, 9 * s, text=T(("side_left", "side_right")[n]),
                             fill=MUTED, font=(FONT, 8, "bold"))
            sent, reported = sides[n] if n < len(sides) else ("–", "–")
            y = top + box_h + 16 * s
            self.create_text(cx - 6 * s, y, anchor="e", text=T("sent"), fill=MUTED,
                             font=(FONT, 9))
            self.create_text(cx, y, anchor="w", text=f"→ {sent} {T('unit_rate')}",
                             fill=TEXT, font=(FONT, 12, "bold"))
            y += 22 * s
            self.create_text(cx - 6 * s, y, anchor="e", text=T("reported"), fill=MUTED,
                             font=(FONT, 9))
            self.create_text(cx, y, anchor="w", text=f"← {reported} {T('unit_rate')}",
                             fill=TEXT, font=(FONT, 12, "bold"))


class RepeatButton(ttk.Button):
    """Runs `step` on press and keeps repeating while held."""
    DELAY_MS, REPEAT_MS = 450, 120

    def __init__(self, master, text, step, **kw):
        super().__init__(master, text=text, **kw)
        self.step, self.job = step, None
        self.bind("<ButtonPress-1>", self._press, add="+")
        for ev in ("<ButtonRelease-1>", "<Leave>"):
            self.bind(ev, self._stop, add="+")

    def _press(self, _e):
        if self.instate(["disabled"]):
            return
        self.step()
        self.job = self.after(self.DELAY_MS, self._repeat)

    def _repeat(self):
        self.step()
        self.job = self.after(self.REPEAT_MS, self._repeat)

    def _stop(self, _e):
        if self.job:
            self.after_cancel(self.job)
            self.job = None


class BridgeApp:
    def __init__(self, root: tk.Tk, core, config):
        self.root, self.core, self.config = root, core, config
        self.T = i18n.Lang([resource("lang"), os.path.join(core.APP_DIR, "lang")])
        self.T.load(config.get("main", "language", fallback="auto"))
        self.bridge = core.Bridge(config)
        self.log_q: queue.Queue = queue.Queue()
        self.log_lines = collections.deque(maxlen=LOG_LINES)   # kept for rebuild()
        self.log_handler = QueueLogHandler(self.log_q)
        logging.getLogger().addHandler(self.log_handler)
        self.closing = False
        self.busy = False               # connect / disconnect in progress
        self.after_busy = None
        self.scale = root.winfo_fpixels("1i") / 96.0

        self._style()
        root.geometry(f"{int(760 * self.scale)}x{int(600 * self.scale)}")
        root.minsize(int(620 * self.scale), int(480 * self.scale))
        try:
            root.iconbitmap(default=resource("icon.ico"))
        except tk.TclError:
            pass
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.protocol("WM_SAVE_YOURSELF", self.on_session_end)   # logoff/shutdown

        saved = config.get("main", "com", fallback="0")
        if saved != "0" and saved in self._ports():
            self.connect(saved)
        elif saved != "0":
            core.logger.info(f"Saved port {saved} not found")
        self.tick()

    # ---- layout ----

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        self.root.option_add("*Font", f"{{{FONT}}} 9")
        st.configure(".", font=(FONT, 9), background=BG)
        # Touch friendly: buttons >= ~11 mm high, wide scrollbar, big list items
        px = lambda v: int(v * self.scale)
        st.configure("TButton", padding=(px(14), px(10)), font=(FONT, 10),
                     background="#f8fafc", bordercolor=BORDER, lightcolor="#f8fafc",
                     darkcolor=BORDER)
        st.map("TButton", background=[("active", "#e2e8f0"), ("pressed", "#cbd5e1"),
                                      ("disabled", "#f1f5f9")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                     font=(FONT, 10, "bold"))
        st.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af"),
                                             ("disabled", "#93c5fd")])
        st.configure("Step.TButton", padding=(px(4), px(12)), font=(FONT, 12, "bold"))
        st.configure("TCombobox", padding=px(8), arrowsize=px(22))
        self.root.option_add("*TCombobox*Listbox.font", f"{{{FONT}}} 13")
        st.configure("TEntry", padding=px(6))
        st.configure("Vertical.TScrollbar", arrowsize=px(30), background="#e2e8f0",
                     troughcolor="#f1f5f9", bordercolor=BORDER)
        st.configure("Horizontal.TProgressbar", troughcolor=BLUE_BG, background=ACCENT,
                     bordercolor=BLUE_BG, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Horizontal.TProgressbar", troughcolor=BLUE_BG, background=ACCENT,
                     bordercolor=BLUE_BG, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build(self):
        root, T = self.root, self.T
        root.configure(bg=BG)
        root.title(T("app_title"))
        pad = 10

        # header
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=pad, pady=(pad, 4))
        tk.Label(head, text=T("app_title"), bg=BG, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(side="left")
        self.pill_ctrl, self.pill_agio, self.pill_term = Pill(head), Pill(head), Pill(head)
        for p in (self.pill_ctrl, self.pill_agio, self.pill_term):
            p.pack(side="right", padx=(5, 0))

        # banner (warnings / scan progress), shown below the anchor
        self.banner_anchor = tk.Frame(root, bg=BG, height=0)
        self.banner_anchor.pack(fill="x")
        self.banner = tk.Frame(root, bg=BLUE_BG)
        self.banner_text = tk.Label(self.banner, bg=BLUE_BG, fg=BLUE_FG, justify="left",
                                    anchor="w", padx=10, pady=6)
        self.banner_text.pack(side="left", fill="x", expand=True)
        self.banner_text.bind("<Configure>", lambda e: self.banner_text.config(
            wraplength=max(200, e.width - 20)))
        self.banner_bar = ttk.Progressbar(self.banner, length=int(150 * self.scale),
                                          maximum=1.0)
        self.banner_shown = None

        # base rate (Amados), top
        self.rate_card = Card(root)
        row = tk.Frame(self.rate_card, bg=CARD)
        row.pack(fill="x", padx=12, pady=8)
        left = tk.Frame(row, bg=CARD)
        left.pack(side="left")
        tk.Label(left, text=T("base_rate").upper(), bg=CARD, fg=MUTED,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        val = tk.Frame(left, bg=CARD)
        val.pack(anchor="w")
        self.base_value = tk.Label(val, text="–", bg=CARD, fg=TEXT, font=(FONT, 24, "bold"))
        self.base_value.pack(side="left")
        tk.Label(val, text=T("unit_rate"), bg=CARD, fg=MUTED,
                 font=(FONT, 10)).pack(side="left", anchor="s", pady=(0, 5), padx=(3, 0))
        self.base_src = tk.Label(left, bg=CARD, fg=MUTED, font=(FONT, 8))
        self.base_src.pack(anchor="w")

        setrow = tk.Frame(row, bg=CARD)
        setrow.pack(side="right", anchor="center")
        self.rate_var = tk.StringVar()
        # No keyboard: the new rate is dialled in with -10/-1/+1/+10 (hold to
        # repeat), shown amber until Set rate sends it; x drops it
        self.step_btns = []
        for delta in (-10, -1):
            b = RepeatButton(setrow, f"−{-delta}", lambda d=delta: self.step_rate(d),
                             style="Step.TButton", width=4)
            b.pack(side="left", padx=(0, 4))
            self.step_btns.append(b)
        self.rate_value = tk.Label(setrow, textvariable=self.rate_var, width=5, anchor="e",
                                   bg=CARD, fg=TEXT, font=(FONT, 18, "bold"), padx=6,
                                   highlightthickness=1, highlightbackground=BORDER)
        self.rate_value.pack(side="left", fill="y")
        for delta in (1, 10):
            b = RepeatButton(setrow, f"+{delta}", lambda d=delta: self.step_rate(d),
                             style="Step.TButton", width=4)
            b.pack(side="left", padx=(4, 0))
            self.step_btns.append(b)
        tk.Label(setrow, text=T("unit_rate"), bg=CARD, fg=MUTED).pack(side="left", padx=(4, 8))
        self.rate_btn = ttk.Button(setrow, text=T("btn_set_rate"), style="Accent.TButton",
                                   command=self.set_rate)
        self.rate_btn.pack(side="left", fill="y")
        self.cancel_btn = ttk.Button(setrow, text="✕", width=3, command=self.reset_rate_entry)
        self.rate_dirty = False         # a value is dialled in but not sent
        self.shown_base = object()

        # sections
        self.sect_card = Card(root, T("card_sections"))
        self.sect_card.pack(fill="x", padx=pad, pady=4)
        self.sections = SectionBar(self.sect_card, self.scale, T)
        self.sections.pack(fill="x", pady=(2, 4))
        self.machine_line = tk.Label(self.sect_card, bg=CARD, fg=MUTED, font=(FONT, 8))

        # footer (packed before the log so it never gets squeezed out)
        foot = tk.Frame(root, bg=BG)
        foot.pack(side="bottom", fill="x", padx=pad, pady=(4, pad))
        tk.Label(foot, text=T("port"), bg=BG, fg=MUTED).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(foot, textvariable=self.port_var, width=22,
                                     state="readonly", postcommand=self._fill_ports,
                                     font=(FONT, 11))
        self.port_box.pack(side="left", padx=5, fill="y")
        self.conn_btn = ttk.Button(foot, text=T("btn_connect"), style="Accent.TButton",
                                   command=self.toggle_connection)
        self.conn_btn.pack(side="left")
        ttk.Button(foot, text=T("btn_export"), command=self.export_logs).pack(side="right")
        ttk.Button(foot, text=T("btn_logs_folder"),
                   command=self.open_logs).pack(side="right", padx=5)
        ttk.Button(foot, text=T("btn_settings"), command=self.open_settings).pack(side="right")
        self._fill_ports()

        # log
        log_card = Card(root, T("card_log"))
        log_card.pack(fill="both", expand=True, padx=pad, pady=(4, 0))
        frame = tk.Frame(log_card, bg=CARD)
        frame.pack(fill="both", expand=True, padx=(12, 3), pady=(3, 8))
        self.log = tk.Text(frame, height=6, bg=CARD, fg=TEXT, font=(MONO, 9),
                           relief="flat", wrap="word", state="disabled",
                           highlightthickness=0)
        sb = ttk.Scrollbar(frame, command=self.log.yview, style="Vertical.TScrollbar")
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("warn", foreground=AMBER_FG)
        self.log.tag_configure("error", foreground=RED)
        self.log_follow = True
        for w in (self.log, sb):
            for ev in ("<MouseWheel>", "<ButtonRelease-1>", "<KeyRelease>"):
                w.bind(ev, self._log_scrolled, add="+")
        # drag with a finger to scroll
        self.log.bind("<ButtonPress-1>", self._drag_start)
        self.log.bind("<B1-Motion>", self._drag_move)
        self._append_log(list(self.log_lines), keep=False)

    def rebuild(self):
        """Redraw everything (after a language change)."""
        for w in self.root.winfo_children():
            w.destroy()
        self._build()

    # ---- ports / connection ----

    def _ports(self) -> dict:
        return {p.device: p.description for p in serial.tools.list_ports.comports()}

    def _fill_ports(self):
        ports = self._ports()
        values = [f"{d}  –  {desc}" if desc and desc != "n/a" else d
                  for d, desc in sorted(ports.items())]
        self.port_box["values"] = values
        current = self.port_var.get().split(" ")[0] or \
            self.config.get("main", "com", fallback="0")
        for v in values:
            if v.split(" ")[0] == current:
                self.port_var.set(v)
                break

    def toggle_connection(self):
        if self.bridge.phase in (self.bridge.IDLE, self.bridge.ERROR):
            port = self.port_var.get().split(" ")[0]
            if not port:
                messagebox.showinfo(self.T("app_title"), self.T("msg_select_port_first"),
                                    parent=self.root)
                return
            self.connect(port)
        else:
            self.disconnect()

    def connect(self, port: str):
        if self.config.get("main", "com", fallback="0") != port:
            self.config.set("main", "com", port)
            self._save_config()
        self.port_var.set(port)
        self._fill_ports()
        self._run_busy(lambda: self.bridge.start(port))

    def disconnect(self, then=None):
        self._run_busy(self.bridge.stop, then)

    def _run_busy(self, work, then=None):
        """Run a blocking bridge call off the Tk thread; tick() runs `then`
        on the Tk thread once it is done."""
        self.busy = True
        self.after_busy = then

        def run():
            try:
                work()
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True, name="bridge-ctl").start()

    def _save_config(self):
        try:
            self.core.save_config(self.config)
        except OSError as e:
            messagebox.showwarning(self.T("app_title"),
                                   self.T("msg_config_save_failed", error=e), parent=self.root)

    # ---- base rate ----

    def _entry_rate(self):
        try:
            return float(self.rate_var.get().strip().replace(",", "."))
        except ValueError:
            return None

    def step_rate(self, delta: int):
        """−10/−1/+1/+10 buttons: change the value in the box; Set rate sends it."""
        req = self._amados()
        cur = self._entry_rate()
        if cur is None:
            cur = (req.base_rate if req else None) or 0.0
        new = min(MAX_RATE, max(0.0, round(cur + delta, 1)))
        self.rate_var.set(fmt_rate(new))
        self.rate_dirty = True

    def reset_rate_entry(self):
        self.rate_dirty = False
        self.shown_base = object()          # re-sync with the base on next tick

    def _amados(self):
        req = self.bridge.requester
        return req if isinstance(req, self.core.AmadosRequester) else None

    def set_rate(self):
        T = self.T
        req = self._amados()
        if req is None or req.state == self.core.MachineState.DISCONNECTED:
            return
        rate = self._entry_rate()
        if rate is None or not 0 < rate <= MAX_RATE:
            messagebox.showwarning(T("app_title"), T("msg_rate_range", max=f"{MAX_RATE:.0f}"),
                                   parent=self.root)
            return
        cur = req.base_rate
        if cur is None or abs(rate - cur) > 0.5 * cur:
            text = T("confirm_rate_new", new=fmt_rate(rate)) if cur is None else \
                T("confirm_rate_change", old=fmt_rate(cur), new=fmt_rate(rate))
            if not messagebox.askyesno(T("app_title"), text, parent=self.root):
                return
        req.request_base(rate)
        self.rate_dirty = False
        self.root.focus_set()

    # ---- periodic refresh ----

    def tick(self):
        if self.closing:
            return
        if not self.busy and self.after_busy:
            then, self.after_busy = self.after_busy, None
            then()
        try:
            self._drain_log()
            self._refresh()
        except Exception as e:     # never let a display bug stop the polling
            self.core.logger.debug(f"window refresh failed: {e!r}")
        self.root.after(POLL_MS, self.tick)

    def _drain_log(self):
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self._append_log(lines)

    def _append_log(self, lines, keep=True):
        if keep:
            self.log_lines.extend(lines)
        self.log.configure(state="normal")
        for level, text in lines:
            tag = "error" if level >= logging.ERROR else \
                "warn" if level >= logging.WARNING else ""
            self.log.insert("end", text + "\n", tag)
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        if self.log_follow:
            self.log.see("end")

    def _drag_start(self, e):
        self.drag_y = e.y
        return "break"                  # no text selection

    def _drag_move(self, e):
        line_h = max(1, tk.font.Font(font=self.log.cget("font")).metrics("linespace"))
        n = int((self.drag_y - e.y) / line_h)
        if n:
            self.log.yview_scroll(n, "units")
            self.drag_y -= n * line_h
            self._log_scrolled()
        return "break"

    def _log_scrolled(self, _event=None):
        # Follow new lines only while the operator is at the bottom
        self.root.after_idle(lambda: setattr(
            self, "log_follow", self.log.yview()[1] >= 0.999))

    def _refresh(self):
        b, T = self.bridge, self.T
        MS = self.core.MachineState
        req = b.requester if b.phase == b.RUNNING else None
        amados = self._amados() if req else None

        # connection button / port box
        connected = b.phase not in (b.IDLE, b.ERROR)
        self.conn_btn.config(text=T("btn_disconnect") if connected else T("btn_connect"),
                             style="TButton" if connected else "Accent.TButton",
                             state="disabled" if self.busy and b.phase != b.SCANNING
                             else "normal")
        self.port_box.config(state="disabled" if connected else "readonly")

        # pills
        mode = {"amados": "Amados", "quantron": "Quantron"}.get(b.machine, "")
        if b.phase == b.ERROR:
            self.pill_term.show(T("pill_term_error", port=b.port), "error")
        elif b.phase in (b.OPENING, b.SCANNING):
            self.pill_term.show(T("pill_term_reading", port=b.port), "info")
        elif req is None:
            self.pill_term.show(T("pill_term_none"), "off")
        elif req.state == MS.DISCONNECTED:
            self.pill_term.show(T("pill_term_silent", port=b.port), "error")
        else:
            self.pill_term.show(T("pill_term_ok", mode=mode, port=b.port), "ok")

        if req is None:
            self.pill_agio.show("AgIO", "off")
        elif req.agio_connected:
            self.pill_agio.show(T("pill_agio_ok"), "ok")
        else:
            self.pill_agio.show(T("pill_agio_wait"), "warn")

        if amados is not None:
            if amados.controlling:
                self.pill_ctrl.show(T("pill_ctrl_aog"), "ok")
            else:
                self.pill_ctrl.show(T("pill_ctrl_terminal"), "off")
        elif req is not None and req.state == MS.RUNNING:
            self.pill_ctrl.show(T("pill_ctrl_sections"), "ok")
        else:
            self.pill_ctrl.show(T("pill_ctrl_idle"), "off")

        # base rate card on top (Amados only)
        if amados is not None:
            if not self.rate_card.winfo_ismapped():
                self.rate_card.pack(fill="x", padx=10, pady=4, before=self.sect_card)
            self._refresh_base(amados)
        elif self.rate_card.winfo_ismapped():
            self.rate_card.pack_forget()

        # sections
        if amados is not None:
            self._refresh_amados_sections(amados)
        elif req is not None:
            n = max(1, min(req.section_count, 16))
            ms = req.machine_sections
            self.sections.show(n, ms[0] | ms[1] << 8, None, req.state == MS.RUNNING)
            self.machine_line.pack_forget()
        else:
            n = self.config.getint("main", "sections", fallback=8)
            self.sections.show(n, 0, None, False)
            self.machine_line.pack_forget()

        self._refresh_banner(b, amados)

    def _refresh_base(self, r):
        T = self.T
        src, color = {
            r.SRC_TERMINAL: (T("src_terminal"), MUTED),
            r.SRC_PC: (T("src_pc"), MUTED),
            r.SRC_REMEMBERED: (T("src_remembered"), AMBER_FG),
        }.get(r.base_source, (T("src_waiting"), MUTED))
        text = fmt_rate(r.base_rate)
        fg = AMBER_FG if r.base_source == r.SRC_REMEMBERED else TEXT
        if self.base_value.cget("text") != text or self.base_value.cget("fg") != fg:
            self.base_value.config(text=text, fg=fg)
        if self.base_src.cget("text") != src:
            self.base_src.config(text=src, fg=color)

        can_set = r.state != self.core.MachineState.DISCONNECTED
        self.rate_btn.config(state="normal" if can_set else "disabled")
        for b in self.step_btns:
            b.config(state="normal" if can_set else "disabled")
        if not self.rate_dirty and r.base_rate != self.shown_base:
            self.shown_base = r.base_rate
            self.rate_var.set("" if r.base_rate is None else fmt_rate(r.base_rate))
        # amber box = a value not sent yet
        typed = self._entry_rate()
        pending = self.rate_dirty and typed is not None and typed != r.base_rate
        bg = AMBER_BG if pending else CARD
        if self.rate_value.cget("bg") != bg:
            self.rate_value.config(bg=bg, highlightbackground=AMBER_FG if pending else BORDER)
            if pending:
                self.cancel_btn.pack(side="left", padx=(4, 0), fill="y")
            else:
                self.cancel_btn.pack_forget()

    def _refresh_amados_sections(self, r):
        # The terminal only reports one target for the whole machine (average
        # of both sides). When it matches what we sent, each side is confirmed
        # at its sent value; otherwise show the reported average on both.
        cmd, target = r.cmd, r.target_avg
        sent = [cmd[1], cmd[2]] if r.controlling else [None, None]
        if target is None:
            reported = [None, None]
        elif None not in sent and abs(target - sum(sent) / 2) <= 1.5:
            reported = sent
        else:
            reported = [target, target]
        sides = [(fmt_rate(sent[i]), fmt_rate(reported[i])) for i in (0, 1)]
        count = r.per_side * 2
        if r.controlling:
            mask = r.section_mask
        else:
            # Terminal in control: handed back fully open (if it has a rate)
            mask = (1 << count) - 1 if target else 0
        self.sections.show(count, mask, r.per_side, r.controlling, sides)

        speed = "–" if r.speed is None else f"{r.speed:.1f}"
        line = self.T("machine_line", actual=fmt_rate(r.actual_rate),
                      width=fmt_rate(r.width), speed=speed)
        if self.machine_line.cget("text") != line:
            self.machine_line.config(text=line)
        if not self.machine_line.winfo_ismapped():
            self.machine_line.pack(anchor="w", padx=12, pady=(0, 8))

    def _refresh_banner(self, b, r):
        T = self.T
        msg, tone, progress = None, "info", None
        if self.closing:
            msg = T("banner_closing")
        elif b.phase == b.ERROR:
            msg, tone = T("banner_error", port=b.port, error=b.error), "error"
        elif b.phase == b.OPENING:
            msg = T("banner_opening", port=b.port)
        elif b.phase == b.SCANNING:
            msg = T("banner_scanning") if b.scan_progress else T("banner_waiting_terminal")
            progress = b.scan_progress
        elif b.phase == b.IDLE and not self.busy:
            msg = T("banner_select_port")
        elif r is not None:
            last = self.config.getfloat("main", "last_base_rate", fallback=0.0)
            if r.base_rate is None and r.terminal_zero:
                msg, tone = T("banner_zero_no_rate"), "warn"
            elif r.base_source == r.SRC_REMEMBERED:
                msg, tone = T("banner_remembered", rate=fmt_rate(last)), "warn"
            elif r.terminal_zero and r.controlling:
                msg, tone = T("banner_zero_resend"), "warn"
            elif r.config_error:
                msg, tone = T("banner_config_error", error=r.config_error), "warn"

        if msg is None:
            if self.banner_shown is not None:
                self.banner.pack_forget()
                self.banner_shown = None
            return
        shown = (msg, tone, progress is not None)
        if shown != self.banner_shown:
            bg, fg = TONES[tone]
            self.banner.config(bg=bg)
            self.banner_text.config(text=msg, bg=bg, fg=fg)
            if progress is not None:
                self.banner_bar.pack(side="right", padx=10)
            else:
                self.banner_bar.pack_forget()
            if self.banner_shown is None:
                self.banner.pack(fill="x", padx=10, pady=(0, 4), after=self.banner_anchor)
            self.banner_shown = shown
        if progress is not None:
            self.banner_bar["value"] = progress

    # ---- logs ----

    def open_logs(self):
        os.makedirs(self.core.LOG_DIR, exist_ok=True)
        os.startfile(self.core.LOG_DIR)

    def export_logs(self):
        T = self.T
        days = max(1, self.config.getint("main", "log_keep_days",
                                         fallback=self.core.DEFAULT_LOG_KEEP_DAYS))
        dest = filedialog.asksaveasfilename(
            parent=self.root, title=T("export_title"),
            initialdir=os.path.join(os.path.expanduser("~"), "Desktop"),
            initialfile=f"AOG-ASD-logs_{time.strftime('%Y%m%d_%H%M')}.zip",
            defaultextension=".zip", filetypes=[(T("zip_files"), "*.zip")])
        if not dest:
            return
        for h in logging.getLogger().handlers:
            h.flush()
        try:
            n = log_archive.export_logs(dest, self.core.LOG_DIR, days,
                                        extra_files=[self.core.CONFIG_PATH])
        except Exception as e:
            messagebox.showerror(T("app_title"), T("export_failed", error=e), parent=self.root)
            return
        self.core.logger.info(f"Exported {n} log(s) of the last {days} days to {dest}")
        messagebox.showinfo(T("app_title"), T("export_done", n=n, days=days, path=dest),
                            parent=self.root)

    # ---- settings ----

    def open_settings(self):
        SettingsDialog(self)

    def settings_saved(self, changed: bool, language_changed: bool):
        if language_changed:
            self.T.load(self.config.get("main", "language", fallback="auto"))
            self.rebuild()
        if changed and self.bridge.phase == self.bridge.RUNNING:
            if messagebox.askyesno(self.T("app_title"), self.T("reconnect_ask"),
                                   parent=self.root):
                port = self.bridge.port
                self.disconnect(then=lambda: self.connect(port))

    # ---- closing ----

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        self._refresh_banner(self.bridge, None)
        t = threading.Thread(target=self.bridge.stop, daemon=True, name="bridge-stop")
        t.start()

        def wait():
            if t.is_alive():
                self.root.after(100, wait)
            else:
                logging.getLogger().removeHandler(self.log_handler)
                self.root.destroy()
        wait()

    def on_session_end(self):
        """Windows logoff / shutdown: restore before the process dies."""
        self.closing = True
        self.core.logger.info("Windows session ending -- shutting down")
        self.bridge.stop()
        logging.shutdown()
        self.root.destroy()


class SettingsDialog(tk.Toplevel):
    # key, label key, kind, extra, default; kind "choice" extra = values
    FIELDS = (
        ("language", "set_language", "lang", None, "auto"),
        ("machine", "set_machine", "choice", ("auto", "amados", "quantron"), "auto"),
        ("sections", "set_sections", "int", (2, 16), "8"),
        ("startup_scan", "set_startup_scan", "bool", None, "1"),
        ("sct_hz", "set_sct_hz", "int", (1, 10), "2"),
        ("subnet", "set_subnet", "str", None, "255.255.255.255"),
        ("log_keep_days", "set_log_keep_days", "int", (0, 3650), "7"),
    )
    # changing these does not need a reconnect
    NO_RECONNECT = ("language", "log_keep_days")

    def __init__(self, app: BridgeApp):
        super().__init__(app.root, bg=CARD)
        self.app = app
        T = app.T
        self.title(T("settings_title"))
        self.transient(app.root)
        self.resizable(False, False)
        cfg = app.config
        self.vars = {}
        self.langs = {"auto": T("lang_auto")}
        self.langs.update(T.available())
        frm = tk.Frame(self, bg=CARD)
        frm.pack(padx=16, pady=14)
        # Touch: toggles and -/+ steppers instead of check boxes / spin boxes
        for row, (key, label, kind, extra, default) in enumerate(self.FIELDS):
            value = cfg.get("main", key, fallback=default)
            tk.Label(frm, text=T(label), bg=CARD, fg=TEXT, font=(FONT, 10), justify="left",
                     wraplength=int(300 * app.scale)).grid(row=row, column=0, sticky="w", pady=4)
            if kind == "bool":
                var = tk.BooleanVar(value=value.strip() not in ("0", "false", "no", "off"))
                w = ttk.Button(frm, width=8)

                def show(w=w, var=var):
                    w.config(text=T("toggle_on") if var.get() else T("toggle_off"),
                             style="Accent.TButton" if var.get() else "TButton")
                w.config(command=lambda var=var, show=show: (var.set(not var.get()), show()))
                show()
            elif kind == "int":
                var = tk.StringVar(value=value)
                w = tk.Frame(frm, bg=CARD)
                lo, hi = extra
                for text, d in (("−", -1), (None, 0), ("+", 1)):
                    if text is None:
                        tk.Label(w, textvariable=var, width=5, bg=CARD, fg=TEXT,
                                 font=(FONT, 13, "bold")).pack(side="left")
                        continue
                    RepeatButton(w, text, lambda var=var, d=d, lo=lo, hi=hi:
                                 self._step(var, d, lo, hi),
                                 style="Step.TButton", width=3).pack(side="left")
            else:
                var = tk.StringVar(value=value)
                if kind == "lang":
                    var.set(self.langs.get(value.strip().lower(), self.langs["auto"]))
                    w = ttk.Combobox(frm, textvariable=var, values=list(self.langs.values()),
                                     state="readonly", width=18, font=(FONT, 11))
                elif kind == "choice":
                    w = ttk.Combobox(frm, textvariable=var, values=extra,
                                     state="readonly", width=18, font=(FONT, 11))
                else:
                    w = ttk.Entry(frm, textvariable=var, width=18, font=(FONT, 11))
            w.grid(row=row, column=1, sticky="w", padx=(14, 0), pady=4)
            self.vars[key] = (var, kind, extra, label)
        tk.Label(frm, text=T("settings_stored", path=app.core.CONFIG_PATH), bg=CARD,
                 fg=MUTED, font=(FONT, 8), wraplength=int(420 * app.scale),
                 justify="left").grid(row=len(self.FIELDS), column=0, columnspan=2,
                                      sticky="w", pady=(8, 0))
        btns = tk.Frame(self, bg=CARD)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(btns, text=T("btn_save"), style="Accent.TButton",
                   command=self.save).pack(side="right")
        ttk.Button(btns, text=T("btn_cancel"), command=self.destroy).pack(side="right", padx=5)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    @staticmethod
    def _step(var, d, lo, hi):
        try:
            n = int(var.get())
        except ValueError:
            n = lo
        var.set(str(min(hi, max(lo, n + d))))

    def save(self):
        T, cfg = self.app.T, self.app.config
        new = {}
        for key, (var, kind, extra, label) in self.vars.items():
            if kind == "bool":
                new[key] = "1" if var.get() else "0"
                continue
            value = var.get().strip()
            if kind == "lang":
                value = next((c for c, n in self.langs.items() if n == value), "auto")
            elif kind == "int":
                try:
                    n = int(value)
                except ValueError:
                    n = None
                if n is None or not extra[0] <= n <= extra[1]:
                    messagebox.showwarning(T("settings_title"), T(
                        "settings_int_range", field=T(label), lo=extra[0], hi=extra[1]),
                        parent=self)
                    return
                value = str(n)
            new[key] = value
        diff = {k for k, v in new.items() if cfg.get("main", k, fallback=None) != v}
        for k, v in new.items():
            cfg.set("main", k, v)
        self.app._save_config()
        self.destroy()
        self.app.settings_saved(bool(diff - set(self.NO_RECONNECT)), "language" in diff)


def run(core, config):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    BridgeApp(root, core, config)
    root.mainloop()

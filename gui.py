#!/usr/bin/env python3
"""ALR Quote Verifier desktop GUI."""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from argparse import Namespace

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wintypes

import alr_quote_verifier as aqv
from alr_quote_verifier import run_audit
from local_a2aj import InstallCancelled
from verifier_core import api_key_store, overlay_store, paths, registry

APP_TITLE = "ALR Quote Verifier"
KEY_SIGNUP_URL = "https://platform.openai.com/api-keys"


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _asset_path(name: str) -> str:
    """Resolve a bundled asset both frozen (PyInstaller datas -> assets/)
    and from a source checkout (assets live under packaging/assets/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) if meipass else Path(__file__).resolve().parent / "packaging"
    return str(base / "assets" / name)


# ---------------------------------------------------------------------------
# Brand palette (matches assets/app_icon.ico)
# ---------------------------------------------------------------------------
GREEN_DARK = "#103124"      # header / primary button
GREEN = "#1F5C40"           # hover / accents
GREEN_SOFT = "#3D7A5C"      # secondary accents
GOLD = "#E8B84B"            # brand rule / highlights
BG = "#F2F5F3"              # window background
CARD = "#FFFFFF"            # card surfaces
INK = "#1A2B23"             # primary text
MUTED = "#5F7268"           # secondary text
LINE = "#D7E0DA"            # hairline borders
STRIPE = "#F6F9F7"          # listbox zebra stripe
LOG_BG = "#0E1F18"          # log terminal background
LOG_FG = "#D9E6DE"          # log default text
LOG_DIM = "#6E8579"         # log timestamps
LOG_OK = "#8FD4A8"          # log success
LOG_WARN = "#E8C97B"        # log warnings
LOG_ERR = "#F09A8A"         # log errors
LOG_HEAD = "#B9D9F0"        # log file headers
PV_PENDING = "#B9C8BF"      # progress view: waiting phase marker
PV_OK = "#2E7D52"           # progress view: success text on white
PV_WARN = "#A87B1F"         # progress view: warning text on white
PV_ERR = "#B4462F"          # progress view: error text on white
HL_BG = "#F3F7F4"           # highlights: tinted panel
HL_INK = "#3B4A42"          # highlights: soft body text
HL_DIM = "#8DA096"          # highlights: secondary text / idle hint


def _default_output_folder() -> str:
    return str(_app_dir() / "CHECKED_EDITS")


def _settings_path() -> str:
    """Frozen builds keep settings in the per-user config dir (the _MEIPASS
    dir that __file__ points at is deleted on exit); source checkouts keep the repo-root file."""
    if getattr(sys, "frozen", False):
        base = paths.config_dir()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = _app_dir()
        return str(base / "gui_settings.json")
    return str(Path(__file__).resolve().parent / "gui_settings.json")


SETTINGS_PATH = _settings_path()

VALID_RUN_MODES = {"high_accuracy", "economy", "ultra_economy", "free"}
RUN_MODE_LABELS = {
    "High accuracy": "high_accuracy",
    "Economy": "economy",
    "Ultra economy": "ultra_economy",
    "Free (no AI calls)": "free",
}
RUN_MODE_BY_ID = {v: k for k, v in RUN_MODE_LABELS.items()}
SUPRA_LINKING_LABELS = {"Safe": "safe", "Aggressive": "aggressive"}
SUPRA_LINKING_BY_ID = {v: k for k, v in SUPRA_LINKING_LABELS.items()}
VALID_FRAGMENT_MODES = {"all", "pinpointless", "off"}
VALID_EXPORT_DETAILS = {"display", "display-json", "diagnostic-hidden", "diagnostic"}
EXPORT_DETAIL_LABELS = {
    "Display rows only": "display",
    "Display + JSON": "display-json",
    "Display + hidden diagnostics": "diagnostic-hidden",
    "Everything (diagnostic rows)": "diagnostic",
}
EXPORT_DETAIL_BY_MODE = {v: k for k, v in EXPORT_DETAIL_LABELS.items()}

# How many articles may verify at once. "auto" sizes the pool from the
# account's live OpenAI rate limits (read from response headers) once the
# first model call answers.
MAX_PARALLEL_DOCS = 4
VALID_PARALLEL_CHOICES = {"auto", "1", "2", "3", "4"}
PARALLEL_LABELS = {
    "Auto (recommended)": "auto",
    "1 — one at a time": "1",
    "2 at once": "2",
    "3 at once": "3",
    "4 at once": "4",
}
PARALLEL_BY_ID = {v: k for k, v in PARALLEL_LABELS.items()}

# Bumped when a default flips so existing settings files can be migrated.
_SETTINGS_REV = 2

DEFAULT_GUI_SETTINGS = {
    "settings_rev": _SETTINGS_REV,
    "window_geometry": "1000x720",
    "run_mode": "high_accuracy",
    "supra_linking": "safe",
    "llm_cache": True,
    "frag_mode": "all",
    "a2aj": True,
    "local_only": False,
    "us_uk_case_lookup": True,
    "open_workbook": False,
    "open_folder": True,
    "export_detail": "diagnostic-hidden",
    "term_only": False,
    "detailed_log": False,
    "parallel_files": "auto",
    "fn_filter": "",
    "onboarding_done": False,
}


def _settings_with_defaults(settings: dict | None) -> dict:
    merged = dict(DEFAULT_GUI_SETTINGS)
    if settings:
        for k in merged:
            if k in settings:
                merged[k] = settings[k]
    if merged["run_mode"] not in VALID_RUN_MODES:
        merged["run_mode"] = DEFAULT_GUI_SETTINGS["run_mode"]
    if merged["supra_linking"] not in SUPRA_LINKING_BY_ID:
        merged["supra_linking"] = "safe"
    if merged["frag_mode"] not in VALID_FRAGMENT_MODES:
        merged["frag_mode"] = DEFAULT_GUI_SETTINGS["frag_mode"]
    if settings and "export_detail" not in settings and "hide_debug_cols" in settings:
        merged["export_detail"] = "diagnostic-hidden" if settings.get("hide_debug_cols", True) else "diagnostic"
    if merged["export_detail"] not in VALID_EXPORT_DETAILS:
        merged["export_detail"] = DEFAULT_GUI_SETTINGS["export_detail"]
    if str(merged["parallel_files"]) not in VALID_PARALLEL_CHOICES:
        merged["parallel_files"] = DEFAULT_GUI_SETTINGS["parallel_files"]
    return merged


def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("settings_rev", 1) < 2:
        data["settings_rev"] = _SETTINGS_REV
    return data


def _save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Win32 drag & drop (Windows only; other platforms use the Add files button)
# ---------------------------------------------------------------------------
DRAG_DROP_AVAILABLE = sys.platform == "win32"

if DRAG_DROP_AVAILABLE:
    WM_DROPFILES = 0x0233

    shell32 = ctypes.windll.shell32
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragAcceptFiles.restype = None
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [wintypes.HANDLE]
    shell32.DragFinish.restype = None

    user32 = ctypes.windll.user32
    GWLP_WNDPROC = -4
    LONG_PTR = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LONG_PTR, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
    )
    SetWindowLongPtrW = (
        user32.SetWindowLongPtrW
        if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
    )
    SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
    SetWindowLongPtrW.restype = LONG_PTR
    user32.CallWindowProcW.argtypes = [
        LONG_PTR, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.CallWindowProcW.restype = LONG_PTR

    class DropHandler:
        def __init__(self, widget, callback):
            self.widget = widget
            self.callback = callback
            self.hwnd = widget.winfo_id()
            self.old_proc = None
            self._setup()

        def _setup(self):
            shell32.DragAcceptFiles(self.hwnd, True)

            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_DROPFILES:
                    hdrop = wparam
                    count = shell32.DragQueryFileW(hdrop, -1, None, 0)
                    files = []
                    for i in range(count):
                        length = shell32.DragQueryFileW(hdrop, i, None, 0)
                        buf = ctypes.create_unicode_buffer(length + 1)
                        shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                        files.append(buf.value)
                    shell32.DragFinish(hdrop)
                    self.widget.after(0, self.callback, files)
                    return 0
                return user32.CallWindowProcW(self.old_proc, hwnd, msg, wparam, lparam)

            self.proc = WNDPROC(wnd_proc)
            proc_ptr = ctypes.cast(self.proc, ctypes.c_void_p).value
            self.old_proc = SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, proc_ptr)


def _open_path(path: str) -> None:
    """Open a file or folder with the OS default handler."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Args bridge to the engine
# ---------------------------------------------------------------------------
def _source_run_flags(app) -> tuple[bool, bool]:
    local_only_var = getattr(app, "local_only_var", None)
    if local_only_var is not None and local_only_var.get():
        citation_db = registry.get_citation_db()
        setter = getattr(citation_db, "set_external_enabled", None)
        if callable(setter):
            setter(False)
        return False, True
    enabled = app is None or app.us_uk_case_lookup_var.get()
    citation_db = registry.get_citation_db()
    setter = getattr(citation_db, "set_external_enabled", None)
    if callable(setter):
        setter(enabled)
    return False, True


def _build_args(
    dry_fire: bool,
    footnote_ids: str | None = None,
    supra_mode: str = "aggressive",
    supra_linking: str = "safe",
    use_a2aj: bool = True,
    use_db_search: bool = True,
    text_fragment_mode: str = "all",
    export_detail: str = "diagnostic-hidden",
    llm_cache: bool = True,
    run_mode: str = "high_accuracy",
    local_only: bool = False,
) -> Namespace:
    return Namespace(
        input="",
        output_name="CHECKED_EDITS",
        recursive=False,
        max_lookahead=400,
        no_block_quotes=False,
        dry_fire=dry_fire,
        footnote_ids=footnote_ids,
        supra_mode=supra_mode,
        supra_linking=supra_linking,
        use_db_search=use_db_search,
        use_a2aj=use_a2aj,
        text_fragment_mode=text_fragment_mode,
        export_detail=export_detail,
        no_hidden_columns=False,
        no_llm_cache=not llm_cache,
        run_mode=run_mode,
        local_only=local_only,
    )


# ---------------------------------------------------------------------------
# Log plumbing: engine stdout -> queue -> styled Text widget
# ---------------------------------------------------------------------------
_TS_LINE_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}):\d{2}\s+(.*)$")

_ERR_HINTS = ("fail", "error", "traceback", "exception", "could not", "missing")
_WARN_HINTS = ("warn", "no_match", "not found", "skipped", "unresolved", "retry")
_OK_HINTS = ("done", "match", "verified", "wrote", "exported", "resolved", "complete")


def _classify_log_line(text: str) -> str:
    low = text.lower()
    if any(h in low for h in _ERR_HINTS):
        return "err"
    if any(h in low for h in _WARN_HINTS):
        return "warn"
    if any(h in low for h in _OK_HINTS):
        return "ok"
    return ""


class _LogRedirect:
    """File-like object capturing engine prints into the GUI log queue.

    Each line is attributed to a document AT PRINT TIME (via `resolve`,
    which maps the printing thread to its article): when several documents
    verify in parallel, every engine message lands in the right Activity
    tab even if it is drained after that worker has moved on.
    """

    def __init__(self, log_fn, *, quiet=False, resolve=None):
        self.log_fn = log_fn
        self._quiet = bool(quiet)
        self._resolve = resolve or (lambda ident: None)

    def write(self, text):
        if not text or self._quiet:
            return
        doc = self._resolve(threading.get_ident())
        parts = text.split("\n")
        lines = [part + "\n" for part in parts if part]
        if lines:
            self.log_fn((doc, "".join(lines)))

    def flush(self):
        pass


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 22
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip, text=self.text, justify=tk.LEFT,
            background=CARD, foreground=INK,
            relief=tk.SOLID, borderwidth=1,
            wraplength=380, padx=8, pady=6, font=("Segoe UI", 9),
        )
        label.pack()

    def _hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _card_caption(text):
    """Letterspaced small-caps card titles — the app's editorial accent."""
    return " " + " ".join(text.upper()) + " "


def _info_dot(parent, text, style="Info.TLabel"):
    lbl = ttk.Label(parent, text="ⓘ", style=style, cursor="question_arrow")
    ToolTip(lbl, text)
    return lbl


class AutoHideScrollbar(ttk.Scrollbar):
    """Keeps its width (no layout jank) but paints itself invisible while
    the content fits; the thumb only appears once scrolling is possible."""

    def __init__(self, parent, *, shown_style, ghost_style, **kw):
        super().__init__(parent, style=ghost_style, **kw)
        self._shown = shown_style
        self._ghost = ghost_style

    def set(self, lo, hi):
        target = (
            self._ghost if float(lo) <= 0.0 and float(hi) >= 1.0
            else self._shown
        )
        if str(self.cget("style")) != target:
            self.configure(style=target)
        super().set(lo, hi)


PV_PHASES = (
    ("read", "Read document && footnotes"),
    ("analyze", "Analyze && link citations"),
    ("journal", "Match journal articles"),
    ("supra", "Connect ibid && supra references"),
    ("quotes", "Verify quotations"),
    ("write", "Write Excel workbook"),
)
_PV_IDLE_HINT = "Add documents, then press Run verification."

_FN_PROGRESS_RE = re.compile(r"Footnote (\d+)/(\d+)")
_FN_TOTAL_RE = re.compile(r"Analyzing (\d+) footnotes with GPT")

_TAB_GLYPHS = {"queued": "◦", "running": "▸", "done": "✓", "failed": "✗"}


class DocProgressView:
    """One article's Activity subtab: the phase checklist with its rolling
    sub-step slot on top, the highlights feed below, allocated 60/40 so a
    busy checklist never starves the highlights (or vice versa). Each
    parallel article owns one of these; engine log lines are routed here by
    the thread that printed them."""

    PHASES = PV_PHASES

    def __init__(self, app, notebook, name, index, total):
        self.app = app
        self.notebook = notebook
        self.name = name
        self.index = index
        self.total = total
        self.status = "queued"  # queued | running | done | failed
        self.fn_done = 0
        self.fn_total = 0
        self.file_frac = 0.0
        self._active_key = None
        self._sub_msgs = []
        self._fn_ctx = ""
        self._last_ticker_at = 0.0

        self.frame = tk.Frame(notebook, bg=CARD, highlightthickness=1,
                              highlightbackground=LINE)
        notebook.add(self.frame, text=self.tab_text())
        # 60/40 vertical split: checklist over highlights. The checklist
        # side keeps a floor so its fixed content (header + six phases +
        # two sub-step lines) never clips in a short window; only the
        # highlights half compresses below that point.
        self.frame.rowconfigure(0, weight=3, uniform="split", minsize=238)
        self.frame.rowconfigure(1, weight=2, uniform="split")
        self.frame.columnconfigure(0, weight=1)

        top = tk.Frame(self.frame, bg=CARD)
        top.grid(row=0, column=0, sticky="nsew")

        head = tk.Frame(top, bg=CARD)
        head.pack(fill=tk.X, padx=14, pady=(8, 0))
        self.count_var = tk.StringVar(
            value=f"article {index + 1} of {total}" if total > 1 else "")
        tk.Label(
            head, textvariable=self.count_var, bg=CARD, fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT)
        self.doc_var = tk.StringVar(value=self._shorten(name, 46))
        tk.Label(
            head, textvariable=self.doc_var, bg=CARD, fg=INK,
            font=("Segoe UI Semibold", 11), anchor=tk.W,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        body = tk.Frame(top, bg=CARD)
        body.pack(fill=tk.X, padx=16, pady=(6, 0))
        self.phase_icon = {}
        self.phase_name = {}
        self.phase_detail = {}
        for key, label in self.PHASES:
            row = tk.Frame(body, bg=CARD)
            row.pack(fill=tk.X, pady=1)
            icon = tk.Label(row, text="○", width=2, bg=CARD, fg=PV_PENDING,
                            font=("Segoe UI", 10))
            icon.pack(side=tk.LEFT)
            name_lbl = tk.Label(row, text=label.replace("&&", "&"), bg=CARD,
                                fg=MUTED, font=("Segoe UI", 10), anchor=tk.W)
            name_lbl.pack(side=tk.LEFT, padx=(4, 0))
            detail_var = tk.StringVar(value="")
            tk.Label(row, textvariable=detail_var, bg=CARD, fg=MUTED,
                     font=("Segoe UI", 9)).pack(side=tk.RIGHT)
            self.phase_icon[key] = icon
            self.phase_name[key] = name_lbl
            self.phase_detail[key] = detail_var

        # Fixed two-line slot for the active phase's rolling sub-steps.
        # Pre-created labels keep the space reserved, so checklist rows
        # never shift while a run progresses.
        now_panel = tk.Frame(top, bg=CARD)
        now_panel.pack(fill=tk.X, padx=16, pady=(1, 4))
        self.now_labels = []
        for _ in range(2):
            lbl = tk.Label(now_panel, text="", bg=CARD, fg=MUTED,
                           font=("Segoe UI", 8, "italic"), anchor=tk.W)
            lbl.pack(fill=tk.X, padx=(30, 0))
            self.now_labels.append(lbl)

        bottom = tk.Frame(self.frame, bg=CARD)
        bottom.grid(row=1, column=0, sticky="nsew")
        tk.Label(
            bottom, text=_card_caption("Highlights").strip(), bg=CARD,
            fg="#9DB2A6", font=("Segoe UI Semibold", 7),
        ).pack(anchor=tk.W, padx=17, pady=(2, 2))
        self.hl = tk.Text(
            bottom, bg=HL_BG, fg=HL_INK, font=("Segoe UI", 9), wrap=tk.WORD,
            state=tk.DISABLED, relief=tk.FLAT, borderwidth=0, height=4,
            padx=16, pady=9, spacing1=4, cursor="arrow",
        )
        self.hl.pack(fill=tk.BOTH, expand=True)
        self.hl.tag_configure("body", foreground=HL_INK)
        self.hl.tag_configure("dot_ok", foreground=PV_OK)
        self.hl.tag_configure("dot_warn", foreground=PV_WARN)
        self.hl.tag_configure("dot_err", foreground=PV_ERR)
        self.hl.tag_configure("dot_dim", foreground="#B6C6BC")
        self.hl.tag_configure("dim", foreground=HL_DIM)
        self.hl.tag_configure(
            "head", foreground=GREEN_DARK, font=("Segoe UI Semibold", 9),
            spacing1=7)
        hl_scroll = AutoHideScrollbar(
            self.hl, command=self.hl.yview,
            shown_style="Hl.Vertical.TScrollbar",
            ghost_style="GhostHl.Vertical.TScrollbar")
        hl_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.hl["yscrollcommand"] = hl_scroll.set

    # ---------------- tab controls ----------------
    def tab_text(self):
        stem = Path(self.name).stem
        short = stem if len(stem) <= 16 else stem[:15] + "…"
        return f"{_TAB_GLYPHS[self.status]} {short}"

    def set_status(self, status):
        self.status = status
        try:
            self.notebook.tab(self.frame, text=self.tab_text())
        except Exception:
            pass
        if status == "running":
            self._maybe_select()

    def _maybe_select(self):
        """Bring a freshly started article into view — but never yank the
        user away from a tab they could be actively reading."""
        try:
            sel = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            sel = None
        for dv in self.app.doc_views:
            if dv.frame is sel and dv.status != "queued" and dv is not self:
                return
        try:
            self.notebook.select(self.frame)
        except Exception:
            pass

    # ---------------- phase checklist ----------------
    def set_phase_state(self, key, state):
        icon = self.phase_icon[key]
        name = self.phase_name[key]
        if state == "done":
            icon.config(text="✓", fg=PV_OK)
            name.config(fg=MUTED, font=("Segoe UI", 10))
        elif state == "active":
            icon.config(text="◉", fg=GOLD)
            name.config(fg=INK, font=("Segoe UI Semibold", 10))
        else:
            icon.config(text="○", fg=PV_PENDING)
            name.config(fg=MUTED, font=("Segoe UI", 10))
            self.phase_detail[key].set("")
        if state == "active":
            self._set_active(key)
        elif self._active_key == key:
            self._set_active(None)

    def _set_active(self, key):
        """Track which phase owns the rolling sub-step lines; they clear
        when the run moves to the next phase."""
        if key == self._active_key:
            return
        self._active_key = key
        self._fn_ctx = ""
        self._sub_msgs = []
        for lbl in self.now_labels:
            lbl.config(text="")

    def phase(self, key):
        """Mark `key` active and everything before it done."""
        seen = False
        for k, _label in self.PHASES:
            if k == key:
                self.set_phase_state(k, "active")
                seen = True
            elif not seen:
                self.set_phase_state(k, "done")

    def phase_done_through(self, key):
        for k, _label in self.PHASES:
            self.set_phase_state(k, "done")
            if k == key:
                break

    def spin_tick(self, frame_char):
        if self.status == "running" and self._active_key:
            self.phase_icon[self._active_key].config(text=frame_char, fg=GOLD)

    def reset(self):
        for k, _label in self.PHASES:
            self.set_phase_state(k, "pending")

    def start(self):
        self.file_frac = 0.0
        self.fn_done = self.fn_total = 0
        self._last_ticker_at = 0.0
        self.reset()
        self.set_phase_state("read", "active")
        self.now("Reading the document…")
        self.set_status("running")

    # ---------------- ticker + highlights ----------------
    def ctx_msg(self, text, limit=76):
        """Prefix a live action with the footnote it belongs to, so lines
        like a bare "DB search: no match" never appear context-free."""
        if self._fn_ctx:
            budget = max(limit - len(self._fn_ctx) - 3, 30)
            return f"{self._fn_ctx} · {self._shorten(text, budget)}"
        return self._shorten(text, limit)

    def now(self, text):
        """Show a live action in the fixed sub-step slot (two rolling
        lines). Dropped when no phase is active."""
        if self._active_key is None:
            return
        self._sub_msgs.append(self._shorten(text, 76))
        if len(self._sub_msgs) > 2:
            self._sub_msgs.pop(0)
        for lbl, msg in zip(self.now_labels, self._sub_msgs):
            lbl.config(text="·  " + msg)

    def hl_replace_pausing(self):
        """Remove the transient '⏸ Pausing…' highlight so the definitive
        'Paused' line replaces it instead of stacking under it."""
        try:
            line = int(self.hl.index("end-1c").split(".")[0]) - 1
            if line >= 1 and "Pausing" in self.hl.get(f"{line}.0", f"{line}.end"):
                self.hl.config(state=tk.NORMAL)
                self.hl.delete(f"{line}.0", f"{line + 1}.0")
                self.hl.config(state=tk.DISABLED)
        except Exception:
            pass

    def highlight(self, text, tag="dim"):
        self.hl.config(state=tk.NORMAL)
        if tag == "head":
            self.hl.insert(tk.END, text.rstrip() + "\n", ("head",))
        else:
            # Quiet body text; the colored dot alone carries the status.
            self.hl.insert(tk.END, "●  ", (f"dot_{tag}",))
            body_tag = "dim" if tag == "dim" else "body"
            self.hl.insert(tk.END, text.rstrip() + "\n", (body_tag,))
        line_count = int(self.hl.index("end-1c").split(".")[0])
        if line_count > 600:
            self.hl.delete("1.0", f"{line_count - 500}.0")
        self.hl.see(tk.END)
        self.hl.config(state=tk.DISABLED)

    @staticmethod
    def _shorten(text, limit=86):
        text = text.strip()
        if len(text) <= limit:
            return text
        head = (limit - 1) * 2 // 3
        return text[:head] + "…" + text[len(text) - (limit - 1 - head):]

    # ---------------- pause / resume ----------------
    def on_pausing(self):
        if self.status != "running":
            return
        self.now("Pausing — finishing the current operation…")
        self.highlight("⏸ Pausing — finishing the current operation…", "warn")

    def on_paused(self):
        if self.status != "running":
            return
        self.now("Paused — press Resume to continue.")
        self.hl_replace_pausing()
        self.highlight("⏸ Paused — press Resume to continue.", "warn")
        if self._active_key:
            self.phase_icon[self._active_key].config(text="⏸", fg=PV_WARN)

    def on_resumed(self):
        if self.status != "running":
            return
        self.now("Resumed.")
        self.highlight("▶ Resumed.", "ok")
        if self._active_key:
            self.phase_icon[self._active_key].config(text="◉", fg=GOLD)

    # ---------------- progress ----------------
    def set_file_frac(self, v):
        """Advance this article's progress fraction (never backward).
        Footnote analysis spans 2%..85%; the post-phases (journal, supra,
        quote checks, workbook write) fill in the rest."""
        self.file_frac = max(self.file_frac, v)
        self.app._update_bar()

    def overall_frac(self):
        if self.status in ("done", "failed"):
            return 1.0
        return min(self.file_frac, 1.0)

    # ---------------- log-line translation ----------------
    def feed(self, body, tag):
        """Translate one of this article's log lines into progress-view
        state. Anything not recognized becomes the live ticker, so the view
        always moves."""
        if not body:
            return
        if body.startswith("▸ "):
            self.start()
            return
        if body.startswith("──"):
            return  # run-level rules stay in the raw log
        m = _FN_TOTAL_RE.search(body)
        if m:
            self.phase("analyze")
            self.fn_done, self.fn_total = 0, int(m.group(1))
            self.phase_detail["analyze"].set(f"0 of {m.group(1)}")
            self.now("Analyzing footnotes with the model…")
            self.app._note_analyze_started()
            self.set_file_frac(0.02)
            return
        m = _FN_PROGRESS_RE.search(body)
        if m:
            self.phase("analyze")
            self._fn_ctx = f"Footnote {m.group(1)}"
            self.fn_done, self.fn_total = int(m.group(1)), int(m.group(2))
            self.phase_detail["analyze"].set(f"{m.group(1)} of {m.group(2)}")
            self.now(f"Working on footnote {m.group(1)} of {m.group(2)}…")
            self.app._note_fn_progress()
            # Footnote analysis is the bulk of a run: spread it over 2%..85%
            # of the article's bar; the post-phases fill in the rest.
            self.set_file_frac(
                0.02 + 0.83 * int(m.group(1)) / max(int(m.group(2)), 1))
            return
        if body.startswith("Building audit data"):
            # Printed BEFORE the footnote loop — still the "read" stage.
            self.now("Preparing the document…")
            return
        # Per-stage outcome stats — the substance of the highlights feed.
        if body.startswith("Split ") and (
            "citation parts" in body or "(Partial Run)" in body
        ):
            self.highlight(body, "ok")
            return
        m = re.search(r"Journal search: (\d+)/(\d+) matches", body)
        if m:
            n, total_j = int(m.group(1)), int(m.group(2))
            self.highlight(
                f"{n} of {total_j} journal citations matched",
                "ok" if n else "dim")
            return
        if body.startswith("References: "):
            self.highlight(body, "ok")
            return
        m = re.search(r"Quote checks: (\d+) verified, (\d+) partial, (\d+) not found", body)
        if m:
            self.highlight(body, "warn" if int(m.group(3)) else "ok")
            return
        if body.startswith("Resolving journal article links"):
            self.phase("journal")
            self.now("Matching journal articles against the article database…")
            self.set_file_frac(0.87)
            return
        if body.startswith("Resolving ibid/supra"):
            self.phase("supra")
            self.now("Connecting ibid and supra references…")
            self.set_file_frac(0.90)
            return
        if body.startswith("Running quote checks"):
            self.phase("quotes")
            self.now("Checking each quotation against its source…")
            self.set_file_frac(0.93)
            return
        if body.startswith("Writing workbook") or body.startswith("Writing combined workbook"):
            self.phase("write")
            self.now("Writing the Excel workbook…")
            self.set_file_frac(0.99)
            return
        if body.startswith("✓"):
            self.phase_done_through("write")
            self.now("Done.")
            self.highlight(body, "ok")
            self.set_status("done")
            self.app._update_bar()
            return
        if body.startswith("✗"):
            self.highlight(body, "err")
            self.now(self._shorten(body))
            self.set_status("failed")
            self.app._update_bar()
            return
        if body.startswith("[DB]"):
            return
        if body.startswith("Processing URL:"):
            return  # candidate churn — stays in the raw log only
        if body.startswith("Fetching URL:"):
            url = body.split(":", 1)[1].strip()
            self.now(self.ctx_msg("checking " + url))
            return
        if "LLM cache hit" in body:
            self.now(self.ctx_msg("reusing a saved model answer"))
            return
        if body.startswith("Retrying fetch"):
            self.now(self.ctx_msg("CanLII is busy — waiting before trying again…"))
            return
        if "learned new case" in body:
            detail = body.split(":", 1)[-1].strip()
            self.highlight("Learned a new case for the local database — " + detail, "ok")
            return
        if "statute link canonicalized" in body:
            self.highlight(self._shorten(body, 110), "ok")
            return
        if tag == "err":
            self.highlight(self._shorten(body, 120), "err")
            return
        now = time.monotonic()
        if now - self._last_ticker_at >= 0.1:
            self._last_ticker_at = now
            self.now(self.ctx_msg(body))


class ALRQuoteVerifierGUI:
    def __init__(self):
        self.root = tk.Tk()
        # Hidden until the UI is fully built: the bare Tk root otherwise
        # flashes as a small default-size stub window during construction.
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(_asset_path("app_icon.ico"))
            self._icon_photos = [
                tk.PhotoImage(file=_asset_path("app_icon_64.png")),
                tk.PhotoImage(file=_asset_path("app_icon_32.png")),
            ]
            self.root.iconphoto(True, *self._icon_photos)
        except Exception:
            pass  # icon is cosmetic; never block launch
        self.saved_settings = _load_settings()

        # A provisional floor; replaced by a content-derived minimum once the
        # UI is built (see _apply_min_size, called after _build_ui).
        self._min_w, self._min_h = 760, 560
        self.root.minsize(self._min_w, self._min_h)

        self.files: list[str] = []
        self.running = False
        self.run_started_at = 0.0
        self.log_queue = queue.Queue()

        # Progress / pause state (see the progress-model section below)
        self._paused = False
        self._pause_requested = False
        self._pause_started_at = 0.0
        self._resume_evt = threading.Event()
        self._resume_evt.set()
        self.doc_views: list[DocProgressView] = []   # one per article this run
        self._thread_docs: dict[int, DocProgressView] = {}  # worker thread -> article
        self._eta_ema = None    # smoothed time-remaining estimate, seconds
        self._analyze_started_at = 0.0  # when the run's first footnote loop began
        self._fn_samples = []   # recent (footnotes_done_across_articles, monotonic_t)
        self._status_before_pause = "Ready"
        self._spin_i = 0
        self._starting = False   # Run pressed, first article not yet underway
        # Parallel-batch plumbing (see _run_worker)
        self._gov = None                 # shared RateLimitGovernor, if any
        self._gov_announced = False
        self._slots = None               # semaphore staggering article starts
        self._slots_extra_pending = 0    # permits held back until limits are known
        self._extra_released = 0

        self._init_style()
        self._build_ui()
        # The layout now exists: lock in a minimum size that fits the content
        # (scales with the display's font/DPI), then place the window.
        self._apply_min_size()
        win_geo = self.saved_settings.get("window_geometry", DEFAULT_GUI_SETTINGS["window_geometry"])
        self._apply_initial_geometry(win_geo)
        self._restore_settings()
        self._wire_settings_persistence()
        self._refresh_a2aj_corpus_ui()
        self.root.after(1500, self._check_a2aj_updates)
        aqv.PAUSE_GATE = self._pause_gate  # pause takes effect before the next slow operation
        self._setup_drop()
        self.root.after(100, self._poll_log)
        self.root.after(110, self._pv_spin)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._maybe_show_onboarding)
        # First launch of a build extracts the reference databases from the
        # exe overlay; do it in the background so the window is instant.
        threading.Thread(target=self._prewarm_dbs, daemon=True).start()

        # Everything is built: retire the PyInstaller boot splash (frozen
        # builds only) and reveal the finished window in one motion.
        try:
            import pyi_splash  # type: ignore  # noqa: PLC0415
            pyi_splash.close()
        except Exception:
            pass
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def _apply_min_size(self):
        """Derive the window's minimum size from the actual laid-out content
        so no element is ever occluded or clipped, whatever the display's
        font size / DPI scaling. Measured across both tabs — the largest in
        each dimension wins — because the Settings tab has no scroll region,
        so every one of its rows must fit within the minimum height."""
        self.root.update_idletasks()
        current = self.notebook.select()
        req_w = req_h = 0
        for tab in (self.verify_tab, self.settings_tab):
            self.notebook.select(tab)
            self.root.update_idletasks()
            req_w = max(req_w, self.root.winfo_reqwidth())
            req_h = max(req_h, self.root.winfo_reqheight())
        if current:
            self.notebook.select(current)
            self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Never demand more than the screen can show (leave room for the
        # taskbar / title bar).
        self._min_w = min(req_w, sw)
        self._min_h = min(req_h, max(400, sh - 80))
        self.root.minsize(self._min_w, self._min_h)

    def _apply_initial_geometry(self, win_geo):
        """Restore the saved window size, but re-center whenever the saved
        position is missing or would place the window off-screen (e.g. a
        negative offset left over from a near-maximized session). The size is
        clamped up to the content minimum and down to the screen."""
        m = re.match(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$", (win_geo or "").strip())
        if not m:
            win_geo = DEFAULT_GUI_SETTINGS["window_geometry"]
            m = re.match(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$", win_geo)
        w, h = int(m.group(1)), int(m.group(2))

        # Clamp to the screen work area, but never below the content minimum.
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(max(w, getattr(self, "_min_w", 0)), sw)
        h = min(max(h, getattr(self, "_min_h", 0)), sh)

        x = m.group(3)
        y = m.group(4)
        on_screen = (
            x is not None
            and 0 <= int(x) <= sw - w
            and 0 <= int(y) <= sh - h
        )
        if on_screen:
            px, py = int(x), int(y)
        else:
            # Center on the primary screen; bias slightly up so the title
            # bar clears the top and the taskbar doesn't clip the bottom.
            px = max(0, (sw - w) // 2)
            py = max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{px}+{py}")

    def _center_over_root(self, dlg, y_offset=None):
        """Place a dialog centered over the main window (or at a fixed
        vertical offset from its top). Clamps on-screen."""
        dlg.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        dw, dh = dlg.winfo_width(), dlg.winfo_height()
        x = rx + (rw - dw) // 2
        y = ry + (y_offset if y_offset is not None else (rh - dh) // 2)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = min(max(0, x), max(0, sw - dw))
        y = min(max(0, y), max(0, sh - dh))
        dlg.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------
    def _init_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Combobox dropdown lists are plain tk widgets — brand them too.
        self.root.option_add("*TCombobox*Listbox.background", "white")
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", GREEN)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")

        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background=CARD, foreground=INK)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Info.TLabel", background=CARD, foreground=GREEN_SOFT, font=("Segoe UI", 10))
        style.configure("InfoBg.TLabel", background=BG, foreground=GREEN_SOFT, font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))

        # Notebook tabs
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(12, 8, 12, 0))
        style.configure(
            "TNotebook.Tab",
            background="#E3EBE6", foreground=MUTED,
            padding=(20, 8), font=("Segoe UI Semibold", 10), borderwidth=0,
            focuscolor=CARD,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD), ("active", "#EDF4EF")],
            foreground=[("selected", GREEN_DARK), ("active", GREEN)],
        )

        # Settings group cards
        style.configure(
            "Card.TLabelframe",
            background=CARD, borderwidth=1, relief="solid",
            bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=CARD, foreground=GREEN_SOFT, font=("Segoe UI Semibold", 8),
        )

        # Buttons
        style.configure(
            "Secondary.TButton",
            background=CARD, foreground=GREEN_DARK,
            bordercolor=LINE, lightcolor=CARD, darkcolor=CARD,
            focuscolor=GREEN_SOFT, padding=(12, 5), font=("Segoe UI", 9),
        )
        style.map(
            "Secondary.TButton",
            background=[("pressed", "#E4EDE7"), ("active", "#EDF4EF")],
            bordercolor=[("active", GREEN_SOFT)],
        )
        style.configure(
            "Link.TLabel",
            background=CARD, foreground=GREEN, font=("Segoe UI", 9, "underline"),
        )
        style.configure(
            "StatusBar.TButton",
            background=BG, foreground=GREEN_DARK,
            bordercolor=LINE, lightcolor=BG, darkcolor=BG,
            focuscolor=GREEN_SOFT, padding=(10, 2), font=("Segoe UI", 9),
        )
        style.map(
            "StatusBar.TButton",
            background=[("pressed", "#E4EDE7"), ("active", "#EDF4EF")],
            bordercolor=[("active", GREEN_SOFT)],
        )

        # Checkbuttons on cards: clam's indicator still reads as a grey
        # Win-9x box, so draw our own (PIL isn't shipped in the exe —
        # PhotoImage pixel fills suffice for flat squares + a checkmark).
        s = 16
        gap = 8  # transparent pixels baked into the image = label spacing
        chk_off = tk.PhotoImage(width=s + gap, height=s, master=self.root)
        chk_off.put("#9FB4A8", to=(0, 0, s, s))
        chk_off.put("white", to=(1, 1, s - 1, s - 1))
        chk_on = tk.PhotoImage(width=s + gap, height=s, master=self.root)
        chk_on.put(GREEN, to=(0, 0, s, s))
        for i in range(3):   # checkmark: short down-stroke…
            chk_on.put("white", to=(3 + i, 7 + i, 5 + i, 10 + i))
        for i in range(5):   # …then the long up-stroke
            chk_on.put("white", to=(6 + i, 8 - i, 8 + i, 11 - i))
        self._chk_images = (chk_off, chk_on)  # keep refs alive
        try:
            style.element_create(
                "BrandCheck.indicator", "image", chk_off,
                ("selected", chk_on),
            )
        except tk.TclError:
            pass  # already registered (second GUI in the same process)
        style.layout("Card.TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("BrandCheck.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(
            "Card.TCheckbutton",
            background=CARD, foreground=INK, focuscolor=CARD,
        )
        style.map("Card.TCheckbutton", background=[("active", CARD)])
        style.configure(
            "TCombobox",
            fieldbackground="white", padding=4,
            arrowcolor=GREEN_DARK, bordercolor=LINE,
            lightcolor=CARD, darkcolor=CARD, selectbackground=GREEN,
            selectforeground="white",
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", GREEN_SOFT)],
            fieldbackground=[("readonly", "white")],
            foreground=[("readonly", INK)],
        )
        style.configure("TEntry", fieldbackground="white", padding=4, bordercolor=LINE)
        style.map("TEntry", bordercolor=[("focus", GREEN_SOFT)])

        # Scrollbars: flat arrowless thumb on a quiet trough (the clam
        # default — chunky bevels + arrow buttons — is pure Windows 98).
        thumb_layout = lambda orient, sticky: [  # noqa: E731
            (f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ]
        style.layout("Vertical.TScrollbar", thumb_layout("Vertical", "ns"))
        style.layout("Horizontal.TScrollbar", thumb_layout("Horizontal", "ew"))
        style.layout("Log.Vertical.TScrollbar", thumb_layout("Vertical", "ns"))
        style.configure(
            "TScrollbar",
            background="#C3D2C9", troughcolor="#EFF3F0", bordercolor="#EFF3F0",
            lightcolor="#C3D2C9", darkcolor="#C3D2C9",
            gripcount=0, relief="flat", borderwidth=0, width=9,
        )
        style.map("TScrollbar", background=[("active", GREEN_SOFT)])
        # Dark variant for the scrollbar living inside the raw-log terminal
        style.configure(
            "Log.Vertical.TScrollbar",
            background="#2E4A3C", troughcolor=LOG_BG, bordercolor=LOG_BG,
            lightcolor="#2E4A3C", darkcolor="#2E4A3C",
            gripcount=0, relief="flat", borderwidth=0, width=9,
        )
        style.map("Log.Vertical.TScrollbar", background=[("active", GREEN_SOFT)])
        # Invisible ("ghost") variants: AutoHideScrollbar swaps to these
        # while its content fits, keeping the width so nothing shifts.
        for name, surface in (("GhostWhite", "white"), ("GhostHl", HL_BG)):
            style.layout(f"{name}.Vertical.TScrollbar", thumb_layout("Vertical", "ns"))
            style.configure(
                f"{name}.Vertical.TScrollbar",
                background=surface, troughcolor=surface, bordercolor=surface,
                lightcolor=surface, darkcolor=surface,
                gripcount=0, relief="flat", borderwidth=0, width=9,
            )
            style.map(f"{name}.Vertical.TScrollbar", background=[("active", surface)])
        # Visible variant matched to the highlights panel's tinted surface
        style.layout("Hl.Vertical.TScrollbar", thumb_layout("Vertical", "ns"))
        style.configure(
            "Hl.Vertical.TScrollbar",
            background="#C3D2C9", troughcolor=HL_BG, bordercolor=HL_BG,
            lightcolor="#C3D2C9", darkcolor="#C3D2C9",
            gripcount=0, relief="flat", borderwidth=0, width=9,
        )
        style.map("Hl.Vertical.TScrollbar", background=[("active", GREEN_SOFT)])

        # Progress bar
        style.configure(
            "Brand.Horizontal.TProgressbar",
            background=GREEN, troughcolor="#E1E9E4",
            bordercolor="#E1E9E4", lightcolor=GREEN, darkcolor=GREEN, thickness=12,
        )

        style.configure("Sep.TSeparator", background=LINE)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self._build_header()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=0, pady=(4, 0))

        self.verify_tab = ttk.Frame(self.notebook, padding=14)
        self.settings_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.verify_tab, text="  Verify  ")
        self.notebook.add(self.settings_tab, text="  Settings  ")

        self._build_verify_tab()
        self._build_settings_tab()
        self._build_statusbar()

    def _build_header(self):
        header = tk.Frame(self.root, bg=GREEN_DARK)
        header.pack(fill=tk.X)
        inner = tk.Frame(header, bg=GREEN_DARK)
        inner.pack(fill=tk.X, padx=16, pady=5)
        try:
            self._header_icon = tk.PhotoImage(file=_asset_path("app_icon_32.png"))
            tk.Label(inner, image=self._header_icon, bg=GREEN_DARK).pack(side=tk.LEFT, padx=(0, 9))
        except Exception:
            pass
        title_box = tk.Frame(inner, bg=GREEN_DARK)
        title_box.pack(side=tk.LEFT)
        row = tk.Frame(title_box, bg=GREEN_DARK)
        row.pack(anchor=tk.W)
        tk.Label(
            row, text=APP_TITLE, bg=GREEN_DARK, fg="white",
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT)
        tk.Label(
            title_box, text="Citation links and quotation checking for law review editing",
            bg=GREEN_DARK, fg="#B5D2C2", font=("Segoe UI", 8),
        ).pack(anchor=tk.W)
        tk.Frame(self.root, bg=GOLD, height=2).pack(fill=tk.X)

    # ------------------------------ Verify tab -------------------------
    def _build_verify_tab(self):
        tab = self.verify_tab

        cols = ttk.Frame(tab)
        cols.pack(fill=tk.BOTH, expand=True)
        cols.columnconfigure(0, weight=2, uniform="verify")
        cols.columnconfigure(1, weight=3, uniform="verify")
        cols.rowconfigure(0, weight=1)
        left_col = ttk.Frame(cols)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right_col = ttk.Frame(cols)
        right_col.grid(row=0, column=1, sticky="nsew")

        # --- Documents card (left column) ---
        doc_card = ttk.Labelframe(left_col, text=_card_caption("Documents"), style="Card.TLabelframe", padding=10)
        doc_card.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(doc_card, style="Card.TFrame")
        toolbar.pack(fill=tk.X, pady=(0, 8))
        self.btn_add = ttk.Button(toolbar, text="Add files…", style="Secondary.TButton", command=self._add_files)
        self.btn_add.pack(side=tk.LEFT)
        self.btn_remove = ttk.Button(toolbar, text="Remove", style="Secondary.TButton", command=self._remove_selected)
        self.btn_remove.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_clear = ttk.Button(toolbar, text="Clear", style="Secondary.TButton", command=self._clear_files)
        self.btn_clear.pack(side=tk.LEFT, padx=(8, 0))
        self.lbl_file_count = ttk.Label(toolbar, text="No files", style="CardMuted.TLabel")
        self.lbl_file_count.pack(side=tk.RIGHT)

        list_holder = tk.Frame(doc_card, bg=LINE, bd=0, highlightthickness=0)
        list_holder.pack(fill=tk.BOTH, expand=True)
        scrollbar = AutoHideScrollbar(
            list_holder, shown_style="Vertical.TScrollbar",
            ghost_style="GhostWhite.Vertical.TScrollbar")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox = tk.Listbox(
            list_holder,
            yscrollcommand=scrollbar.set,
            selectmode=tk.EXTENDED,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=GREEN_SOFT,
            bg="white",
            fg=INK,
            selectbackground=GREEN,
            selectforeground="white",
            activestyle="none",
        )
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.file_listbox.yview)
        self.out_var = tk.StringVar(value=_default_output_folder())

        # Empty-state drop affordance, centered over the (blank) list area.
        # The whole window is a drop target; this is the visual cue. It hides
        # itself as soon as any file is added.
        self._drop_hint = tk.Frame(self.file_listbox, bg="white", cursor="hand2")
        tk.Label(
            self._drop_hint, text="⬇", bg="white", fg=GREEN_SOFT,
            font=("Segoe UI", 30),
        ).pack()
        tk.Label(
            self._drop_hint,
            text="Add .docx files to get started",
            bg="white", fg=MUTED, font=("Segoe UI Semibold", 10),
        ).pack(pady=(2, 0))
        # Clicking the empty-state hint opens the file picker.
        for w in (self._drop_hint, *self._drop_hint.winfo_children()):
            w.bind("<Button-1>", lambda _e: self._add_files())
        self._update_drop_hint()

        # --- Run bar (spans both columns; fixed footprint so nothing
        #     jumps when a run starts) ---
        # Packed BEFORE `cols` in the packing order (via before=) so pack
        # reserves the run bar's space first; when the window shrinks the
        # document/activity area gives up room instead of the Run button
        # ever being clipped off the bottom.
        run_bar = ttk.Frame(tab)
        run_bar.pack(fill=tk.X, side=tk.BOTTOM, pady=(12, 0), before=cols)

        btns = ttk.Frame(run_bar)
        btns.pack(side=tk.RIGHT, anchor=tk.S)
        self.pause_btn = tk.Button(
            btns,
            text="⏸   Pause",
            width=11,  # widest label ("▶   Resume") so state changes don't shift layout
            command=self._toggle_pause,
            bg=CARD,
            fg=GREEN_DARK,
            activebackground="#EDF4EF",
            activeforeground=GREEN_DARK,
            disabledforeground="#AFC3B6",
            font=("Segoe UI Semibold", 11),
            relief=tk.FLAT,
            cursor="hand2",
            padx=18,
            pady=8,
            bd=0,
            highlightthickness=1,
            highlightbackground=LINE,
            state=tk.DISABLED,
        )
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.run_btn = tk.Button(
            btns,
            text="▶   Run verification",
            width=17,  # widest label so Running…/Paused don't resize the button
            command=self._run,
            bg=GREEN_DARK,
            fg="white",
            activebackground=GREEN,
            activeforeground="white",
            disabledforeground="#9DB5A8",
            font=("Segoe UI Semibold", 11),
            relief=tk.FLAT,
            cursor="hand2",
            padx=26,
            pady=8,
            bd=0,
        )
        self.run_btn.pack(side=tk.LEFT)
        self.run_btn.bind(
            "<Enter>",
            lambda _e: self.run_btn.config(bg=GREEN)
            if self.run_btn["state"] != tk.DISABLED else None,
        )
        self.run_btn.bind("<Leave>", lambda _e: self.run_btn.config(bg=GREEN_DARK))

        prog_box = ttk.Frame(run_bar)
        prog_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 16))
        # Headline is a row of segments so a concurrency ⓘ can sit inline:
        #   "N in progress | R running in parallel ⓘ | M of T finished"
        # The middle segment and the ⓘ appear only during a multi-article
        # run; see _set_running_headline for the placement rules.
        head_row = ttk.Frame(prog_box)
        head_row.pack(anchor=tk.W)
        self.status_main_var = tk.StringVar(value="Ready")
        ttk.Label(
            head_row, textvariable=self.status_main_var,
            font=("Segoe UI Semibold", 10), foreground=INK, background=BG,
        ).pack(side=tk.LEFT)
        self.status_par_var = tk.StringVar(value="")
        self._hl_par = ttk.Label(
            head_row, textvariable=self.status_par_var,
            font=("Segoe UI Semibold", 10), foreground=GREEN_DARK, background=BG,
        )
        self._conc_dot = ttk.Label(
            head_row, text="ⓘ", style="Info.TLabel", cursor="question_arrow")
        self._conc_tip = ToolTip(self._conc_dot, "")
        self.status_tail_var = tk.StringVar(value="")
        self._hl_tail = ttk.Label(
            head_row, textvariable=self.status_tail_var,
            font=("Segoe UI Semibold", 10), foreground=INK, background=BG,
        )
        self._hl_mode = None  # last-applied layout, so we only re-pack on change
        sub_row = ttk.Frame(prog_box)
        sub_row.pack(anchor=tk.W)
        self.status_sub_var = tk.StringVar(value="Add documents, then press Run verification.")
        ttk.Label(
            sub_row, textvariable=self.status_sub_var, style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        _info_dot(
            sub_row,
            "The time estimate appears once enough footnotes have been "
            "measured to project this run's own pace, and it stays a "
            "ballpark: footnotes vary a lot in complexity, previously "
            "cached footnotes finish almost instantly, and CanLII "
            "sometimes forces waits between page fetches. When several "
            "articles run at once, the estimate covers the articles under "
            "way. When there isn't enough signal for a reasonable guess, "
            "no estimate is shown.",
            style="InfoBg.TLabel",
        ).pack(side=tk.LEFT, padx=(6, 0))
        self.progress = ttk.Progressbar(
            prog_box, mode="determinate", value=0, maximum=1000,
            style="Brand.Horizontal.TProgressbar",
        )
        self.progress.pack(fill=tk.X, pady=(4, 0))

        # --- Activity log card (right column) ---
        log_card = ttk.Labelframe(right_col, text=_card_caption("Activity"), style="Card.TLabelframe", padding=10)
        log_card.pack(fill=tk.BOTH, expand=True)

        # Two interchangeable views share this area: the streamlined
        # progress view (default) and the raw engine log (toggle below or
        # in Advanced settings — same persisted switch).
        self.detail_log_var = tk.BooleanVar(value=DEFAULT_GUI_SETTINGS["detailed_log"])

        log_bottom = ttk.Frame(log_card, style="Card.TFrame")
        log_bottom.pack(fill=tk.X, side=tk.BOTTOM, pady=(6, 0))
        ttk.Checkbutton(
            log_bottom, text="Raw log", variable=self.detail_log_var,
            style="Card.TCheckbutton",
        ).pack(side=tk.LEFT)
        ttk.Button(log_bottom, text="Copy", style="Secondary.TButton", command=self._copy_log, width=8).pack(side=tk.RIGHT)
        log_stack = ttk.Frame(log_card, style="Card.TFrame")
        log_stack.pack(fill=tk.BOTH, expand=True)
        self.log_stack = log_stack
        # One subtab per article; parallel runs fill several at once. Until
        # the first run there's a single placeholder tab.
        self.doc_nb = ttk.Notebook(log_stack)
        self._make_idle_view()

        self.log_text = tk.Text(
            log_stack,
            height=9,
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            bg=LOG_BG,
            fg=LOG_FG,
            insertbackground=LOG_FG,
            selectbackground=GREEN,
            selectforeground="white",
            padx=10,
            pady=8,
            spacing1=1,
            spacing3=1,
        )
        # Hanging indent: wrapped continuations (long URLs) line up under
        # the body column instead of snapping back to the left edge.
        from tkinter import font as tkfont
        body_indent = tkfont.Font(font=("Consolas", 9)).measure(" " * 7)
        self.log_text.tag_configure("line", lmargin2=body_indent)
        self.log_text.tag_configure(
            "cont", lmargin1=body_indent, lmargin2=body_indent)
        self.log_text.tag_configure("dim", foreground=LOG_DIM)
        self.log_text.tag_configure("ok", foreground=LOG_OK)
        self.log_text.tag_configure("warn", foreground=LOG_WARN)
        self.log_text.tag_configure("err", foreground=LOG_ERR)
        self.log_text.tag_configure("head", foreground=LOG_HEAD, font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("rule", foreground=GREEN_SOFT)

        log_scroll = ttk.Scrollbar(
            self.log_text, command=self.log_text.yview,
            style="Log.Vertical.TScrollbar")
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text["yscrollcommand"] = log_scroll.set
        self.doc_nb.bind(
            "<<NotebookTabChanged>>", lambda _event: self._filter_raw_log()
        )

        self.detail_log_var.trace_add("write", lambda *_: self._apply_log_view())
        self._apply_log_view()

    # ------------------------------ Progress view ----------------------
    # The default Activity view: a notebook with one DocProgressView subtab
    # per article (see the module-level class); parallel articles fill
    # several tabs at once. The raw log still receives everything (Copy
    # always exports the full transcript).
    PV_PHASES = PV_PHASES
    _PV_IDLE_HINT = _PV_IDLE_HINT

    def _make_idle_view(self):
        dv = DocProgressView(self, self.doc_nb, "Ready", 0, 1)
        dv.doc_var.set("Ready")
        self.doc_nb.tab(dv.frame, text=" Ready ")
        dv.highlight(self._PV_IDLE_HINT, "dim")
        self._idle_view = dv

    def _setup_run_views(self, names):
        """Fresh tabs for this run's articles (last run's tabs retire)."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        for tab_id in list(self.doc_nb.tabs()):
            frame = self.doc_nb.nametowidget(tab_id)
            self.doc_nb.forget(tab_id)
            frame.destroy()
        self._idle_view = None
        self.doc_views = [
            DocProgressView(self, self.doc_nb, name, i, len(names))
            for i, name in enumerate(names)
        ]
        try:
            self.doc_nb.select(self.doc_views[0].frame)
        except Exception:
            pass
        self._filter_raw_log()

    def _apply_log_view(self):
        if bool(self.detail_log_var.get()):
            self._filter_raw_log()
            self.doc_nb.pack_forget()
            self.log_text.pack(fill=tk.BOTH, expand=True)
        else:
            self.log_text.pack_forget()
            self.doc_nb.pack(fill=tk.BOTH, expand=True)

    def _filter_raw_log(self):
        """Show raw lines for the selected article plus shared run messages."""
        try:
            selected = self.doc_nb.index("current")
        except Exception:
            selected = 0
        for doc in self.doc_views:
            self.log_text.tag_configure(
                f"doc_{doc.index}", elide=doc.index != selected
            )

    _SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _pv_spin(self):
        """Claude-Code-style braille spinner on each running article's
        active phase marker, and on the Run button while the run spins up."""
        if self.running and not self._paused:
            self._spin_i = (self._spin_i + 1) % len(self._SPIN_FRAMES)
            ch = self._SPIN_FRAMES[self._spin_i]
            for dv in self.doc_views:
                dv.spin_tick(ch)
            # Animate the Run button from the moment it's pressed until the
            # first article is actually underway (DB warm-up, config, thread
            # spin-up can take a beat, especially on first launch).
            if self._starting:
                if any(dv.status != "queued" for dv in self.doc_views):
                    self._starting = False
                    self.run_btn.config(text="Running…")
                else:
                    self.run_btn.config(text=f"{ch}  Starting…")
        self.root.after(110, self._pv_spin)

    # ---- aggregate footnote progress across all articles ----
    def _agg_fn(self):
        """(footnotes done, known total, articles without a total yet)."""
        done = sum(dv.fn_done for dv in self.doc_views)
        known = sum(dv.fn_total for dv in self.doc_views if dv.fn_total)
        unknown = sum(
            1 for dv in self.doc_views
            if not dv.fn_total and dv.status in ("queued", "running"))
        return done, known, unknown

    def _note_analyze_started(self):
        if not self._analyze_started_at:
            self._analyze_started_at = time.monotonic()

    def _note_fn_progress(self):
        done = sum(dv.fn_done for dv in self.doc_views)
        self._fn_samples.append((done, time.monotonic()))
        if len(self._fn_samples) > 25:
            self._fn_samples.pop(0)

    # ------------------------------ Settings tab -----------------------
    def _grid_group(self, parent, title):
        card = ttk.Labelframe(parent, text=_card_caption(title), style="Card.TLabelframe", padding=10)
        card.pack(fill=tk.X, pady=(0, 8))
        card.columnconfigure(1, weight=1)
        return card

    def _build_settings_tab(self):
        tab = self.settings_tab

        wrap = ttk.Frame(tab)
        wrap.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(wrap)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        right = ttk.Frame(wrap)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- OpenAI key group ---
        key_card = self._grid_group(left, "OpenAI API key")
        ttk.Label(
            key_card,
            text="The verifier uses OpenAI to read footnotes. It needs your API "
                 "key, which it stores encrypted on this computer and only ever "
                 "sends to OpenAI. Treat the key like a password — never share it.",
            style="CardMuted.TLabel", wraplength=380, justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        ttk.Label(key_card, text="Status:", style="Card.TLabel").grid(row=1, column=0, sticky=tk.W)
        self.key_status_var = tk.StringVar(value="")
        ttk.Label(key_card, textvariable=self.key_status_var, style="Card.TLabel").grid(
            row=1, column=1, columnspan=2, sticky=tk.W, padx=(8, 0))
        btn_row = ttk.Frame(key_card, style="Card.TFrame")
        btn_row.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(10, 0))
        ttk.Button(btn_row, text="Set key…", style="Secondary.TButton", command=self._prompt_api_key).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Clear saved key", style="Secondary.TButton", command=self._clear_api_key).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            btn_row, text="First-time setup guide…", style="Secondary.TButton",
            command=lambda: self._show_onboarding(from_settings=True),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_key_status()

        # --- Processing group ---
        proc_card = self._grid_group(left, "Processing")
        ttk.Label(proc_card, text="Mode:", style="Card.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.run_mode_var = tk.StringVar(value=RUN_MODE_BY_ID["high_accuracy"])
        mode_row = ttk.Frame(proc_card, style="Card.TFrame")
        mode_row.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        self.run_mode_combo = ttk.Combobox(
            mode_row, textvariable=self.run_mode_var,
            values=tuple(RUN_MODE_LABELS), width=15, state="readonly",
        )
        self.run_mode_combo.pack(side=tk.LEFT)
        _info_dot(
            mode_row,
            "High accuracy (default): uses AI to read every footnote.\n"
            "Economy: handles straightforward supra and ibid footnotes without AI.\n"
            "Ultra economy: also handles clearly structured citations without AI, "
            "and uses AI whenever anything important is uncertain.\n"
            "Free: makes no AI calls; uncertain footnotes are kept together for review.\n"
            "All modes try to connect supra and ibid references to earlier citations.",
        ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(proc_card, text="Supra linking:", style="Card.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.supra_linking_var = tk.StringVar(value=SUPRA_LINKING_BY_ID["safe"])
        supra_linking_row = ttk.Frame(proc_card, style="Card.TFrame")
        supra_linking_row.grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Combobox(
            supra_linking_row, textvariable=self.supra_linking_var,
            values=tuple(SUPRA_LINKING_LABELS), width=15, state="readonly",
        ).pack(side=tk.LEFT)
        _info_dot(
            supra_linking_row,
            "Safe links only references it can identify with high confidence.\n"
            "Aggressive also tries two carefully limited options: it can use a "
            "cited note number when that note contains just one source, and it can "
            "recognize predictable short names from earlier cases, laws, books, "
            "and articles. Conflicting matches are rejected, and every recovered "
            "link records how it was found.",
        ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(proc_card, text="Articles at once:", style="Card.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0))
        self.parallel_var = tk.StringVar(
            value=PARALLEL_BY_ID[DEFAULT_GUI_SETTINGS["parallel_files"]])
        par_row = ttk.Frame(proc_card, style="Card.TFrame")
        par_row.grid(row=2, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Combobox(
            par_row, textvariable=self.parallel_var,
            values=tuple(PARALLEL_LABELS), width=24, state="readonly",
        ).pack(side=tk.LEFT)
        _info_dot(
            par_row,
            "How many documents verify at the same time. Auto starts with "
            "one, reads your OpenAI plan's live rate limits from the first "
            "response, and opens additional articles only when the plan has "
            "comfortable headroom — so parallel runs never trip your API "
            "quota. Each article gets its own tab in Activity.",
        ).pack(side=tk.LEFT, padx=(6, 0))

        self.a2aj_var = tk.BooleanVar(value=True)
        a2aj_row = ttk.Frame(proc_card, style="Card.TFrame")
        a2aj_row.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(
            a2aj_row, text="Verify quotes against source text (A2AJ)",
            variable=self.a2aj_var, style="Card.TCheckbutton",
        ).pack(side=tk.LEFT)
        _info_dot(
            a2aj_row,
            "Fetches judgment/legislation text from A2AJ (a free public API — "
            "no key needed) and checks quoted passages against the original. "
            "Turning this off skips quote verification.",
        ).pack(side=tk.LEFT, padx=(6, 0))

        # --- Sources group ---
        src_card = self._grid_group(left, "Sources")
        self.us_uk_case_lookup_var = tk.BooleanVar(
            value=DEFAULT_GUI_SETTINGS["us_uk_case_lookup"]
        )
        ttk.Checkbutton(
            src_card, text="Find US/UK case URLs (free public sources)",
            variable=self.us_uk_case_lookup_var, style="Card.TCheckbutton",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Button(
            src_card, text="Optional API keys…", style="Secondary.TButton",
            command=self._manage_provider_keys,
        ).grid(row=1, column=0, sticky=tk.W, pady=(8, 0))

        # --- Local A2AJ corpus group ---
        corpus_card = self._grid_group(right, "A2AJ local corpus")
        ttk.Label(
            corpus_card,
            text="Downloads all cases and legislation from A2AJ. You can "
                 "update it later without downloading everything again.",
            style="CardMuted.TLabel", wraplength=380, justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 7))
        self.a2aj_corpus_status_var = tk.StringVar(value="Checking local status…")
        ttk.Label(
            corpus_card, textvariable=self.a2aj_corpus_status_var,
            style="Card.TLabel", wraplength=310, justify=tk.LEFT,
        ).grid(row=1, column=0, sticky=tk.W)
        self.a2aj_corpus_btn = ttk.Button(
            corpus_card, text="Install…", style="Secondary.TButton",
            command=self._install_or_cancel_a2aj,
        )
        self.a2aj_corpus_btn.grid(row=1, column=1, sticky=tk.E, padx=(8, 0))

        # --- Local-only privacy group ---
        privacy_card = self._grid_group(right, "Local only")
        ttk.Label(
            privacy_card,
            text="Local only makes no network requests to any provider. It "
                 "requires downloading all A2AJ cases and legislation ahead "
                 "of time, and US/UK cases won't work.",
            style="CardMuted.TLabel", wraplength=380, justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 7))
        self.local_only_var = tk.BooleanVar(value=False)
        self.local_only_check = ttk.Checkbutton(
            privacy_card, text="Run entirely locally",
            variable=self.local_only_var, style="Card.TCheckbutton",
            command=self._on_local_only_toggle,
        )
        self.local_only_check.grid(row=1, column=0, sticky=tk.W)
        self._a2aj_installing = False
        self._a2aj_checking = False
        self._a2aj_cancel = threading.Event()
        self._enable_local_only_after_install = False

        # --- Output group ---
        out_card = self._grid_group(right, "Output")
        ttk.Label(out_card, text="Excel detail:", style="Card.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.export_detail_var = tk.StringVar(
            value=EXPORT_DETAIL_BY_MODE[DEFAULT_GUI_SETTINGS["export_detail"]]
        )
        ttk.Combobox(
            out_card, textvariable=self.export_detail_var,
            values=tuple(EXPORT_DETAIL_LABELS.keys()), width=30, state="readonly",
        ).grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

        self.open_after_var = tk.BooleanVar(value=DEFAULT_GUI_SETTINGS["open_workbook"])
        ttk.Checkbutton(
            out_card, text="Open each workbook when finished",
            variable=self.open_after_var, style="Card.TCheckbutton",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        self.open_folder_var = tk.BooleanVar(value=DEFAULT_GUI_SETTINGS["open_folder"])
        ttk.Checkbutton(
            out_card, text="Open the output folder when done",
            variable=self.open_folder_var, style="Card.TCheckbutton",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        # --- Advanced group ---
        self.llm_cache_var = tk.BooleanVar(value=True)
        self.frag_mode_var = tk.StringVar(value=DEFAULT_GUI_SETTINGS["frag_mode"])
        self.fn_filter_var = tk.StringVar()
        self.term_only_var = tk.BooleanVar(value=False)
        adv_card = self._grid_group(right, "Advanced")
        ttk.Label(adv_card, text="Text fragments:", style="Card.TLabel").grid(row=0, column=0, sticky=tk.W)
        frag_row = ttk.Frame(adv_card, style="Card.TFrame")
        frag_row.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        ttk.Combobox(
            frag_row, textvariable=self.frag_mode_var,
            values=("all", "pinpointless", "off"), width=14, state="readonly",
        ).pack(side=tk.LEFT)
        _info_dot(
            frag_row,
            "Adds #:~:text= highlight fragments to suggested links so the "
            "cited passage is highlighted when the link opens. "
            "“pinpointless” only adds them to citations without a "
            "paragraph pinpoint; “off” disables them.",
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(adv_card, text="Footnote filter:", style="Card.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        fn_row = ttk.Frame(adv_card, style="Card.TFrame")
        fn_row.grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Entry(fn_row, textvariable=self.fn_filter_var, width=16).pack(side=tk.LEFT)
        _info_dot(fn_row, "Only process these footnotes, e.g. 1,4,10-12. Leave empty for all.").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            adv_card, text="Cache model responses (LLM cache)",
            variable=self.llm_cache_var, style="Card.TCheckbutton",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(
            adv_card, text="Log to terminal only (quiet GUI log)",
            variable=self.term_only_var, style="Card.TCheckbutton",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        detail_row = ttk.Frame(adv_card, style="Card.TFrame")
        detail_row.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        ttk.Checkbutton(
            detail_row, text="Show the raw engine log in Activity",
            variable=self.detail_log_var, style="Card.TCheckbutton",
        ).pack(side=tk.LEFT)
        _info_dot(
            detail_row,
            "The Activity panel normally shows a simplified progress view. "
            "Turn this on to see every engine message as it happens "
            "(the full log is always captured either way — Copy exports it).",
        ).pack(side=tk.LEFT, padx=(6, 0))

        # --- Maintenance row ---
        maint = ttk.Frame(right)
        maint.pack(fill=tk.X, pady=(2, 0))
        self.reset_settings_btn = ttk.Button(
            maint, text="Reset all settings to defaults",
            style="Secondary.TButton", command=self._reset_to_defaults,
        )
        self.reset_settings_btn.pack(side=tk.RIGHT)

    def _build_statusbar(self):
        # (left, top, right, bottom) — extra bottom padding so the Output
        # folder button isn't flush against the very bottom of the window.
        bar = ttk.Frame(self.root, padding=(16, 6, 16, 16))
        # Packed before the notebook in the packing order (via before=) so
        # pack reserves the status bar's space first; a short window shrinks
        # the notebook instead of clipping the status bar (and its Output
        # folder button) off the bottom.
        bar.pack(fill=tk.X, side=tk.BOTTOM, before=self.notebook)
        ttk.Button(
            bar, text="📂  Output folder", style="StatusBar.TButton",
            command=self._open_output_folder,
        ).pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value=_default_output_folder())
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(
            side=tk.LEFT, padx=(10, 0))

    # ------------------------------------------------------------------
    # Log rendering
    # ------------------------------------------------------------------
    def _copy_log(self):
        try:
            content = self.log_text.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
        except Exception:
            pass

    def _log(self, message, tag="", doc=None):
        """Insert engine/GUI text into the log, humanizing timestamps and
        coloring lines by kind. Every line gets the same left margin: the
        timestamp column for stamped lines, matching padding for
        continuations (wrapped URLs etc.), so the log reads as one grid.
        `doc` is the article the lines belong to: they feed that article's
        Activity tab, and when several articles run at once the raw log
        marks each line with the article's number."""
        prefix = (
            f"[{doc.index + 1}] "
            if doc is not None and len(self.doc_views) > 1 else ""
        )
        doc_tag = (f"doc_{doc.index}",) if doc is not None else ()
        insert_args = []
        for raw_line in message.split("\n"):
            if raw_line == "":
                continue
            line = raw_line.rstrip()
            m = _TS_LINE_RE.match(line)
            if m:
                stamp, body = m.group(1), m.group(2)
            else:
                stamp, body = "", line.strip()
            line_tag = tag or _classify_log_line(body)
            if doc is not None:
                try:
                    doc.feed(body, line_tag)
                except Exception:
                    pass  # the progress view must never break the log of record
            body = prefix + body
            if stamp:
                # "15:04  " sets the body column; wraps hang at that margin.
                insert_args.extend((f"{stamp}  ", ("dim", "line") + doc_tag))
                insert_args.extend((
                    body + "\n",
                    ((line_tag, "line") if line_tag else ("line",)) + doc_tag,
                ))
            else:
                # Continuations indent via the margin (not literal spaces,
                # which would waste a display row when a long URL wraps).
                insert_args.extend((
                    body + "\n",
                    ((line_tag, "cont") if line_tag else ("cont",)) + doc_tag,
                ))
        if not insert_args:
            return
        self.log_text.config(state=tk.NORMAL)
        # One Tcl call per queue batch is dramatically cheaper than one or
        # two calls per engine line during busy local-only runs.
        self.log_text.insert(tk.END, *insert_args)
        # Cap the buffer: tk.Text appends slow down noticeably past a few
        # thousand lines, and multi-hour runs produce tens of thousands.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 5500:
            self.log_text.delete("1.0", f"{line_count - 5000}.0")
        detail_var = getattr(self, "detail_log_var", None)
        if detail_var is None or detail_var.get():
            self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _log_rule(self, label=""):
        text = f"── {label} " if label else "──"
        pad = max(0, 74 - len(text))
        self._log(text + "─" * pad, tag="rule")

    def _poll_log(self):
        batches = []
        for _ in range(100):
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                doc, text = item
            else:
                doc, text = None, item
            text = str(text or "")
            for batch_doc, chunks in batches:
                if batch_doc is doc:
                    chunks.append(text)
                    break
            else:
                batches.append((doc, [text]))
        for doc, chunks in batches:
            self._log("".join(chunks), doc=doc)
        self._release_parallel_slots()
        if self.running and self.run_started_at and not self._paused:
            elapsed = int(time.monotonic() - self.run_started_at)
            done, known, unknown = self._agg_fn()
            fn_part = (
                f"Footnote {done} of {known}{'+' if unknown else ''}   ·   "
                if known else ""
            )
            self.status_sub_var.set(
                f"{fn_part}{elapsed // 60}:{elapsed % 60:02d} elapsed"
                f"{self._eta_text()}")
            self._set_running_headline()
        self.root.after(10 if not self.log_queue.empty() else 100, self._poll_log)

    _PARALLEL_INFO = (
        "Several articles are being verified at once. How many run in "
        "parallel is decided automatically from your OpenAI plan's live "
        "rate limits — the app reads your requests-per-minute and "
        "tokens-per-minute quota from the API and runs only as many "
        "articles as the plan can comfortably sustain, holding back if "
        "the remaining quota runs low."
    )
    _SERIAL_INFO = (
        "Articles are being verified one at a time. Your OpenAI plan's "
        "rate limits (mainly tokens per minute) don't leave enough "
        "headroom to run more than one at once safely, so the app is "
        "processing them in sequence to avoid exceeding your quota. A "
        "plan with a higher tokens-per-minute limit would run more in "
        "parallel."
    )

    def _clear_headline_extras(self):
        """Collapse the headline back to a single-segment line."""
        for w in (self._hl_par, self._conc_dot, self._hl_tail):
            w.pack_forget()
        self.status_par_var.set("")
        self.status_tail_var.set("")
        self._hl_mode = None

    def _set_running_headline(self):
        views = self.doc_views
        if not views:
            return
        if len(views) == 1:
            self.status_main_var.set(
                f"Verifying {DocProgressView._shorten(views[0].name, 52)}")
            if self._hl_mode is not None:
                self._clear_headline_extras()
            return

        total = len(views)
        running = sum(1 for dv in views if dv.status == "running")
        finished = sum(1 for dv in views if dv.status in ("done", "failed"))
        in_progress = total - finished

        self.status_main_var.set(
            f"{in_progress} article{'s' if in_progress != 1 else ''} in progress")
        self.status_tail_var.set(f"   |   {finished} of {total} finished")

        if running >= 2:
            mode = "parallel"
            self.status_par_var.set(f"   |   {running} running in parallel")
        elif in_progress >= 2:
            mode = "serial"
        else:
            mode = "plain"

        # Only touch the packing (and tooltip) when the layout actually
        # changes — the text vars update every poll without re-packing.
        if mode == self._hl_mode:
            return
        self._hl_mode = mode
        for w in (self._hl_par, self._conc_dot, self._hl_tail):
            w.pack_forget()
        if mode == "parallel":
            self._conc_tip.text = self._PARALLEL_INFO
            self._hl_par.pack(side=tk.LEFT)
            self._conc_dot.pack(side=tk.LEFT, padx=(5, 0))
            self._hl_tail.pack(side=tk.LEFT)
        elif mode == "serial":
            self._conc_tip.text = self._SERIAL_INFO
            self._hl_tail.pack(side=tk.LEFT)
            self._conc_dot.pack(side=tk.LEFT, padx=(5, 0))
        else:  # plain: one article left in flight, no concurrency note
            self._hl_tail.pack(side=tk.LEFT)

    def _release_parallel_slots(self):
        """Open up additional parallel articles once the governor has read
        the account's rate limits from the first model response."""
        if not (self.running and self._slots is not None
                and self._slots_extra_pending > 0):
            return
        gov = self._gov
        if gov is None or not gov.suggested_parallel:
            return
        want = max(0, gov.suggested_parallel - 1 - self._extra_released)
        n = min(want, self._slots_extra_pending)
        for _ in range(n):
            self._slots.release()
        self._extra_released += n
        self._slots_extra_pending -= n
        if not self._gov_announced:
            self._gov_announced = True
            line = gov.limits_line()
            if line:
                self._log(
                    f"{line} — running up to {gov.suggested_parallel} "
                    f"article{'s' if gov.suggested_parallel != 1 else ''} at once.\n")

    def _eta_text(self):
        """Time-remaining estimate from the run's measured footnote rate,
        aggregated across every article in flight. Deliberately
        conservative: it waits for real evidence (25 footnotes and 90s of
        analysis), uses the slower of the whole-run and recent rates
        (cache-hit bursts early in a run otherwise make it wildly
        optimistic), pads for the post-analysis phases, and reacts fast to
        slowdowns but slowly to speedups. No estimate beats a bad one."""
        if not self._analyze_started_at:
            return ""
        if not any(dv._active_key == "analyze" and dv.status == "running"
                   for dv in self.doc_views):
            return ""
        done, known, unknown = self._agg_fn()
        if not known:
            return ""
        span = time.monotonic() - self._analyze_started_at
        if done < 25 or span < 90:
            return ""
        rates = [done / span]
        if len(self._fn_samples) >= 8:
            d_fn = self._fn_samples[-1][0] - self._fn_samples[0][0]
            d_t = self._fn_samples[-1][1] - self._fn_samples[0][1]
            if d_fn > 0 and d_t > 0:
                rates.append(d_fn / d_t)
        rate = min(rates)
        # 30% pad for the post-analysis phases (journals, supra, quotes,
        # workbook) and general variance — err on the slow side.
        remaining = (known - done) / max(rate, 1e-6) * 1.3
        if self._eta_ema is None:
            self._eta_ema = remaining
        elif remaining > self._eta_ema:
            self._eta_ema += 0.5 * (remaining - self._eta_ema)   # slowdowns: adopt fast
        else:
            self._eta_ema += 0.02 * (remaining - self._eta_ema)  # speedups: barely believe
        if unknown:
            # Some articles haven't reported a footnote count yet; the
            # estimate only covers the ones under way.
            running_n = sum(1 for dv in self.doc_views if dv.status == "running")
            more = " on this article" if running_n <= 1 else " on the current articles"
        else:
            more = ""
        return f"   ·   about {self._fmt_eta(self._eta_ema)} left{more}"

    @staticmethod
    def _fmt_eta(seconds):
        s = max(0, int(seconds))
        if s < 90:
            return f"{max(s, 10)} sec"
        m = -(-s // 60)  # round minutes UP — never promise the faster time
        if m < 60:
            return f"{m} min"
        h, m = divmod(m, 60)
        return f"{h} h {m:02d} min" if m else f"{h} h"

    # ------------------------------------------------------------------
    # Progress model: every article contributes an equal share of the
    # bottom bar; each share fills from the article's own phase progress
    # (footnote analysis spans 2%..85% of it, the post-phases the rest).
    # ------------------------------------------------------------------
    def _update_bar(self):
        views = self.doc_views
        if not views:
            return
        frac = sum(dv.overall_frac() for dv in views) / len(views)
        self.progress.config(value=int(min(frac, 1.0) * 1000))

    # ------------------------------------------------------------------
    # Reference database prewarm (first launch of a build only)
    # ------------------------------------------------------------------
    def _prewarm_dbs(self):
        try:
            pending = overlay_store.pending_names()
            if pending:
                self.root.after(
                    0, self._log,
                    "First launch: preparing reference databases "
                    "(one-time step, may take a minute)…\n",
                )
            overlay_store.ensure_all()
            if pending:
                self.root.after(0, self._log, "✓ Reference databases ready.\n", "ok")
        except Exception as e:
            self.root.after(0, self._log, f"Preparing databases failed: {e}\n", "err")

    # ------------------------------------------------------------------
    # Optional full A2AJ corpus / local-only mode
    # ------------------------------------------------------------------
    def _a2aj_statuses(self):
        corpus = aqv.a2aj_client.get_local_corpus()
        return corpus.status("cases"), corpus.status("laws")

    def _a2aj_corpus_installed(self) -> bool:
        return all(status.installed for status in self._a2aj_statuses())

    @staticmethod
    def _format_gb(size: int) -> str:
        return f"{size / 1_000_000_000:.1f} GB"

    def _refresh_a2aj_corpus_ui(self, remote_statuses=None, message=""):
        statuses = self._a2aj_statuses()
        installed = all(status.installed for status in statuses)
        size = sum(status.size for status in statuses)
        if message:
            label = message
        elif installed:
            stale = remote_statuses and any(status.stale for status in remote_statuses)
            label = f"Installed · {self._format_gb(size)}"
            if stale:
                label += " · update available"
        elif any(status.installed for status in statuses):
            label = "Partly installed · resume to finish"
        else:
            label = "Not installed · approximately 4.9 GB"
        self.a2aj_corpus_status_var.set(label)
        if not self._a2aj_installing:
            stale = remote_statuses and any(status.stale for status in remote_statuses)
            self.a2aj_corpus_btn.config(
                text=("Update…" if stale else "Check for updates…")
                if installed else "Install…",
                state=tk.NORMAL,
            )
        if not installed and self.local_only_var.get():
            self.local_only_var.set(False)
        self._apply_local_only_ui()

    def _check_a2aj_updates(self):
        if (self._a2aj_installing or self._a2aj_checking
                or self.local_only_var.get() or not self._a2aj_corpus_installed()):
            return
        self._a2aj_checking = True
        self.a2aj_corpus_status_var.set("Checking for updates…")

        def worker():
            try:
                corpus = aqv.a2aj_client.get_local_corpus()
                statuses = (
                    corpus.check_for_updates("cases"),
                    corpus.check_for_updates("laws"),
                )
                self.root.after(0, self._finish_a2aj_update_check, statuses, "")
            except Exception as exc:
                self.root.after(
                    0, self._finish_a2aj_update_check, None,
                    f"Installed · update check failed ({type(exc).__name__})",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_a2aj_update_check(self, statuses, message):
        self._a2aj_checking = False
        self._refresh_a2aj_corpus_ui(statuses, message)

    def _offer_local_corpus_install(self) -> bool:
        result = {"install": False}
        dlg = tk.Toplevel(self.root)
        dlg.withdraw()
        dlg.title("Install the A2AJ local corpus?")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        frame = ttk.Frame(dlg, padding=16, style="Card.TFrame")
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame, text="Local only needs a complete local copy of A2AJ.",
            style="Card.TLabel", font=("Segoe UI Semibold", 11),
        ).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="Install downloads about 4.9 GB of case law and legislation. "
                 "The documents are unofficial and retain their upstream terms. "
                 "Interrupted downloads can be resumed.",
            style="CardMuted.TLabel", wraplength=430, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        def install():
            result["install"] = True
            dlg.destroy()

        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(
            buttons, text="Cancel", style="Secondary.TButton",
            command=dlg.destroy,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            buttons, text="Install", style="Primary.TButton", command=install,
        ).pack(side=tk.RIGHT)
        self._center_over_root(dlg)
        dlg.deiconify()
        self.root.wait_window(dlg)
        return result["install"]

    def _on_local_only_toggle(self):
        if not self.local_only_var.get():
            self._apply_local_only_ui()
            return
        if not self._a2aj_corpus_installed():
            self.local_only_var.set(False)
            if self._offer_local_corpus_install():
                self._start_a2aj_install(
                    enable_local_only=True, confirmed=True
                )
            return
        self.run_mode_var.set(RUN_MODE_BY_ID["free"])
        self.a2aj_var.set(True)
        self._apply_local_only_ui()

    def _apply_local_only_ui(self):
        if not hasattr(self, "run_mode_combo"):
            return
        self.run_mode_combo.config(
            state=tk.DISABLED if self.local_only_var.get() else "readonly"
        )

    def _install_or_cancel_a2aj(self):
        if self._a2aj_installing:
            self._a2aj_cancel.set()
            self.a2aj_corpus_btn.config(text="Cancelling…", state=tk.DISABLED)
            return
        self._start_a2aj_install()

    def _start_a2aj_install(self, enable_local_only=False, confirmed=False):
        if self._a2aj_installing:
            return
        if not self._a2aj_corpus_installed() and not confirmed:
            if not self._offer_local_corpus_install():
                return
        self._a2aj_installing = True
        self._enable_local_only_after_install = bool(enable_local_only)
        self._a2aj_cancel.clear()
        self.a2aj_corpus_btn.config(text="Cancel", state=tk.NORMAL)
        self.a2aj_corpus_status_var.set("Reading current A2AJ inventory…")

        def worker():
            try:
                corpus = aqv.a2aj_client.get_local_corpus()
                snapshots = (
                    corpus.fetch_metadata("cases"),
                    corpus.fetch_metadata("laws"),
                )
                total = sum(snapshot.size for snapshot in snapshots)
                offset = 0
                for snapshot in snapshots:
                    def progress(item, base=offset):
                        self.root.after(
                            0, self._show_a2aj_progress,
                            base + item.completed, total, item.message,
                        )
                    corpus.install_or_update(
                        snapshot.kind, remote=snapshot, progress=progress,
                        cancelled=self._a2aj_cancel.is_set,
                    )
                    aqv.a2aj_client.clear_memory_cache()
                    offset += snapshot.size
                self.root.after(0, self._finish_a2aj_install, True, "")
            except InstallCancelled:
                self.root.after(
                    0, self._finish_a2aj_install, False,
                    "Download paused · press Install to resume",
                )
            except Exception as exc:
                self.root.after(
                    0, self._finish_a2aj_install, False,
                    f"Install failed: {type(exc).__name__}: {exc}",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _show_a2aj_progress(self, completed, total, message):
        percent = int(completed * 100 / total) if total else 0
        partition = str(message or "").split("/", 1)[0]
        self.a2aj_corpus_status_var.set(
            f"{percent}% · {self._format_gb(completed)} of "
            f"{self._format_gb(total)} · {partition}"
        )

    def _finish_a2aj_install(self, success, message):
        enable = success and self._enable_local_only_after_install
        self._a2aj_installing = False
        self._enable_local_only_after_install = False
        self.a2aj_corpus_btn.config(state=tk.NORMAL)
        self._refresh_a2aj_corpus_ui(message=message)
        if enable:
            self.local_only_var.set(True)
            self._on_local_only_toggle()
            self._save_current_settings()

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------
    def _setup_drop(self):
        if not DRAG_DROP_AVAILABLE:
            return
        try:
            self._drop_handler = DropHandler(self.root, self._on_drop)
        except Exception:
            self._log("Drag-and-drop not available on this system.\n")

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            p = (p or "").strip()
            if p.lower().endswith(".docx") and os.path.isfile(p) and p not in self.files:
                self.files.append(p)
                idx = self.file_listbox.size()
                self.file_listbox.insert(tk.END, f"  {os.path.basename(p)}")
                if idx % 2 == 1:
                    self.file_listbox.itemconfigure(idx, background=STRIPE)
                added += 1
        if added:
            self._update_file_count()

    def _on_drop(self, paths):
        if self.running:
            # The worker iterates self.files live; adding mid-run would
            # silently extend the run and skew the file-count/progress math.
            self._log("Files can't be added while a run is in progress — "
                      "drop them again when it finishes.\n", "warn")
            return
        self._add_paths(paths)

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select .docx files",
            filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
        )
        self._add_paths(paths)

    def _remove_selected(self):
        sel = list(self.file_listbox.curselection())
        for idx in reversed(sel):
            self.file_listbox.delete(idx)
            del self.files[idx]
        self._restripe()
        self._update_file_count()

    def _clear_files(self):
        self.files.clear()
        self.file_listbox.delete(0, tk.END)
        self._update_file_count()

    def _restripe(self):
        for i in range(self.file_listbox.size()):
            self.file_listbox.itemconfigure(i, background=STRIPE if i % 2 == 1 else "white")

    def _update_file_count(self):
        n = len(self.files)
        self.lbl_file_count.config(
            text="No files" if n == 0 else ("1 file" if n == 1 else f"{n} files")
        )
        self._update_drop_hint()

    def _update_drop_hint(self):
        """Show the centered drop affordance only while the list is empty."""
        try:
            if len(self.files) == 0:
                self._drop_hint.place(relx=0.5, rely=0.5, anchor="center")
            else:
                self._drop_hint.place_forget()
        except Exception:
            pass

    def _open_output_folder(self):
        folder = Path(_default_output_folder())
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _open_path(str(folder))
        except Exception as e:
            self._log(f"Could not open output folder: {e}\n", tag="err")

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _restore_settings(self):
        s = _settings_with_defaults(self.saved_settings)
        self.run_mode_var.set(RUN_MODE_BY_ID.get(s["run_mode"], RUN_MODE_BY_ID["high_accuracy"]))
        self.supra_linking_var.set(
            SUPRA_LINKING_BY_ID.get(s["supra_linking"], SUPRA_LINKING_BY_ID["safe"])
        )
        self.llm_cache_var.set(bool(s["llm_cache"]))
        self.frag_mode_var.set(s["frag_mode"])
        self.us_uk_case_lookup_var.set(bool(s["us_uk_case_lookup"]))
        self.a2aj_var.set(bool(s["a2aj"]))
        self.local_only_var.set(
            bool(s["local_only"]) and self._a2aj_corpus_installed()
        )
        if self.local_only_var.get():
            self.run_mode_var.set(RUN_MODE_BY_ID["free"])
            self.a2aj_var.set(True)
        self._apply_local_only_ui()
        self.open_after_var.set(bool(s["open_workbook"]))
        self.open_folder_var.set(bool(s["open_folder"]))
        self.export_detail_var.set(
            EXPORT_DETAIL_BY_MODE.get(
                s["export_detail"],
                EXPORT_DETAIL_BY_MODE[DEFAULT_GUI_SETTINGS["export_detail"]],
            )
        )
        self.term_only_var.set(bool(s["term_only"]))
        self.detail_log_var.set(bool(s["detailed_log"]))
        self.parallel_var.set(
            PARALLEL_BY_ID.get(str(s["parallel_files"]),
                               PARALLEL_BY_ID[DEFAULT_GUI_SETTINGS["parallel_files"]]))
        self.out_var.set(_default_output_folder())
        self.fn_filter_var.set(s["fn_filter"])

    def _reset_to_defaults(self):
        onboarding = self.saved_settings.get("onboarding_done", False)
        self.saved_settings = dict(DEFAULT_GUI_SETTINGS)
        self.saved_settings["onboarding_done"] = onboarding
        self._restore_settings()
        self._save_current_settings()

    def _wire_settings_persistence(self):
        """Save on every settings change, not only on clean exit — a crash
        or force-kill must not lose the user's choices."""
        vars = [
            self.run_mode_var, self.supra_linking_var,
            self.llm_cache_var, self.frag_mode_var,
            self.a2aj_var, self.local_only_var,
            self.open_after_var, self.open_folder_var, self.export_detail_var,
            self.term_only_var, self.detail_log_var, self.parallel_var,
            self.fn_filter_var,
        ]
        vars.append(self.us_uk_case_lookup_var)
        for var in vars:
            var.trace_add("write", lambda *_: self._save_current_settings())

    def _save_current_settings(self):
        s = {
            "settings_rev": _SETTINGS_REV,
            "window_geometry": self.root.geometry(),
            "run_mode": RUN_MODE_LABELS.get(self.run_mode_var.get(), "high_accuracy"),
            "supra_linking": SUPRA_LINKING_LABELS.get(
                self.supra_linking_var.get(), "safe"
            ),
            "llm_cache": self.llm_cache_var.get(),
            "frag_mode": self.frag_mode_var.get(),
            "a2aj": self.a2aj_var.get(),
            "local_only": self.local_only_var.get(),
            "us_uk_case_lookup": self.us_uk_case_lookup_var.get(),
            "open_workbook": self.open_after_var.get(),
            "open_folder": self.open_folder_var.get(),
            "export_detail": EXPORT_DETAIL_LABELS.get(
                self.export_detail_var.get(),
                DEFAULT_GUI_SETTINGS["export_detail"],
            ),
            "term_only": self.term_only_var.get(),
            "detailed_log": self.detail_log_var.get(),
            "parallel_files": PARALLEL_LABELS.get(
                self.parallel_var.get(), DEFAULT_GUI_SETTINGS["parallel_files"]),
            "fn_filter": self.fn_filter_var.get(),
            "onboarding_done": self.saved_settings.get("onboarding_done", False),
        }
        _save_settings(s)
        self.saved_settings.update(s)

    # ------------------------------------------------------------------
    # API key handling
    # ------------------------------------------------------------------
    def _refresh_key_status(self):
        if aqv._get_key("ALT_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
            self.key_status_var.set(
                "✓ using the key from your environment (OPENAI_API_KEY)"
            )
            return
        stored = api_key_store.get_key()
        if stored:
            self.key_status_var.set(f"✓ saved on this computer (…{api_key_store.last4(stored)})")
        elif aqv.LLM_API_KEY:
            self.key_status_var.set(f"✓ set for this session only (…{api_key_store.last4(aqv.LLM_API_KEY)})")
        else:
            self.key_status_var.set("✗ not set — you will be asked when you run")

    def _clear_api_key(self):
        if not api_key_store.has_key() and not aqv.LLM_API_KEY:
            messagebox.showinfo("OpenAI key", "No saved key on this computer.")
            return
        if not messagebox.askyesno(
            "Remove saved key?",
            "This removes the OpenAI API key saved on this computer.\n\n"
            "You will need to enter a key again before the next run. "
            "Your key itself is not deleted at OpenAI — you can paste the "
            "same one back in later.\n\nRemove it?",
            icon="warning",
            default="no",
        ):
            return
        removed = api_key_store.clear_key()
        aqv.LLM_API_KEY = ""
        aqv.client = None
        self._refresh_key_status()
        messagebox.showinfo(
            "OpenAI key",
            "Saved key removed." if removed else "Session key cleared.",
        )

    _PROVIDER_KEY_FIELDS = (
        ("courtlistener", "COURTLISTENER_API_TOKEN", "CourtListener API token",
         "Better US case URL lookup. Free account at courtlistener.com "
         "(profile → API token). Without one, only the public search "
         "endpoint is used, and it is heavily rate-limited."),
        ("govinfo", "GOVINFO_API_KEY", "GovInfo API key",
         "Better US federal docket lookup. Free key at api.data.gov/signup. "
         "Without one, the shared DEMO_KEY allows about 30 requests/hour."),
    )

    def _manage_provider_keys(self):
        """Optional per-user keys for the US/UK case URL providers."""
        dlg = tk.Toplevel(self.root)
        dlg.withdraw()
        dlg.title("Optional API keys")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        frame = ttk.Frame(dlg, padding=14, style="Card.TFrame")
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text="US/UK case URL lookup works without keys. Adding free keys "
                 "raises the providers' rate limits; keys are saved encrypted "
                 "for your account on this computer.",
            style="CardMuted.TLabel", wraplength=460, justify=tk.LEFT,
        ).pack(anchor=tk.W)

        def status_text(name: str, env_var: str) -> str:
            stored = api_key_store.get_key(name)
            if stored:
                return f"✓ saved on this computer ({api_key_store.last4(stored)})"
            if os.environ.get(env_var):
                return f"✓ using the key from your environment ({env_var})"
            return "✗ not set — using free public access"

        for name, env_var, label, help_text in self._PROVIDER_KEY_FIELDS:
            box = ttk.Frame(frame, style="Card.TFrame")
            box.pack(fill=tk.X, pady=(12, 0))
            head = ttk.Frame(box, style="Card.TFrame")
            head.pack(fill=tk.X)
            ttk.Label(head, text=label, style="Card.TLabel").pack(side=tk.LEFT)
            _info_dot(head, help_text).pack(side=tk.LEFT, padx=(6, 0))
            status_var = tk.StringVar(value=status_text(name, env_var))
            ttk.Label(box, textvariable=status_var, style="CardMuted.TLabel").pack(anchor=tk.W, pady=(2, 0))
            row = ttk.Frame(box, style="Card.TFrame")
            row.pack(fill=tk.X, pady=(4, 0))
            key_var = tk.StringVar()
            ttk.Entry(row, textvariable=key_var, show="•", width=42).pack(side=tk.LEFT)

            def save(name=name, env_var=env_var, key_var=key_var, status_var=status_var):
                value = key_var.get().strip()
                if not value:
                    return
                try:
                    api_key_store.set_key(value, name)
                except Exception as exc:
                    messagebox.showwarning(
                        "Optional API keys",
                        f"Saving failed ({type(exc).__name__}); the key will "
                        f"still be used for this session.",
                        parent=dlg,
                    )
                os.environ[env_var] = value
                key_var.set("")
                status_var.set(status_text(name, env_var))

            def clear(name=name, env_var=env_var, status_var=status_var):
                stored = api_key_store.get_key(name)
                api_key_store.clear_key(name)
                if stored and os.environ.get(env_var) == stored:
                    del os.environ[env_var]
                status_var.set(status_text(name, env_var))

            ttk.Button(row, text="Save", style="Secondary.TButton", command=save).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(row, text="Remove", style="Secondary.TButton", command=clear).pack(side=tk.LEFT, padx=(6, 0))

        btns = ttk.Frame(frame, style="Card.TFrame")
        btns.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(btns, text="Close", style="Secondary.TButton", command=dlg.destroy).pack(side=tk.RIGHT)
        self._center_over_root(dlg)
        dlg.deiconify()
        self.root.wait_window(dlg)

    @staticmethod
    def _validate_api_key(key: str) -> str:
        """Return "" when the key works, else a short error message."""
        try:
            from openai import OpenAI
            OpenAI(api_key=key).models.list()
            return ""
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "invalid_api_key" in msg.lower():
                return "That key was rejected by OpenAI (invalid key)."
            if "insufficient_quota" in msg or "429" in msg:
                return "The key works but has no remaining quota (check billing)."
            return f"Could not verify the key: {type(exc).__name__}."

    def _prompt_api_key(self) -> bool:
        """Modal masked prompt; validates before accepting. Returns True when
        a working key is in place afterwards."""
        dlg = tk.Toplevel(self.root)
        dlg.withdraw()  # positioned before it's shown, so it never flashes top-left
        dlg.title("OpenAI API key")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        frame = ttk.Frame(dlg, padding=14, style="Card.TFrame")
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Paste your OpenAI API key (input is hidden):",
                  style="Card.TLabel").pack(anchor=tk.W)
        key_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=key_var, show="•", width=58)
        entry.pack(fill=tk.X, pady=(6, 8))
        remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Remember this key on this computer (encrypted for your Windows account)",
            variable=remember_var, style="Card.TCheckbutton",
        ).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="Treat your API key like a password: anyone who has it can spend "
                 "on your OpenAI account. Never email it or paste it into shared "
                 "documents. This app only ever sends it to OpenAI.",
            style="CardMuted.TLabel", wraplength=430, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=status_var, foreground="#B00020",
                  background=CARD).pack(anchor=tk.W, pady=(6, 0))
        result = {"ok": False}

        def on_ok():
            key = key_var.get().strip()
            if not key:
                status_var.set("Enter a key first.")
                return
            status_var.set("Checking the key with OpenAI …")
            dlg.update_idletasks()
            err = self._validate_api_key(key)
            if err:
                status_var.set(err)
                return
            aqv.LLM_API_KEY = key
            aqv.client = None
            if remember_var.get():
                try:
                    api_key_store.set_key(key)
                except Exception as exc:
                    messagebox.showwarning(
                        "OpenAI key",
                        f"The key works and will be used for this session, but "
                        f"saving it failed ({type(exc).__name__}).",
                        parent=dlg,
                    )
            result["ok"] = True
            dlg.destroy()

        btns = ttk.Frame(frame, style="Card.TFrame")
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="Cancel", style="Secondary.TButton", command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="OK", style="Secondary.TButton", command=on_ok).pack(side=tk.RIGHT)
        entry.bind("<Return>", lambda _e: on_ok())
        self._center_over_root(dlg)
        dlg.deiconify()
        entry.focus_set()
        self.root.wait_window(dlg)
        self._refresh_key_status()
        return result["ok"]

    def _ensure_api_key(self) -> bool:
        if aqv._resolve_api_key():
            return True
        messagebox.showinfo(
            "OpenAI key needed",
            "No OpenAI API key is configured. Enter one to run verification.",
        )
        return self._prompt_api_key()

    # ------------------------------------------------------------------
    # First-time setup guide
    # ------------------------------------------------------------------
    def _maybe_show_onboarding(self):
        if self.saved_settings.get("onboarding_done"):
            return
        if aqv._resolve_api_key():
            return  # already usable; skip the tour
        self._show_onboarding()

    def _show_onboarding(self, from_settings: bool = False):
        dlg = tk.Toplevel(self.root)
        dlg.title("Welcome — first-time setup")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        head = tk.Frame(dlg, bg=GREEN_DARK)
        head.pack(fill=tk.X)
        tk.Label(
            head, text="Welcome to the ALR Quote Verifier",
            bg=GREEN_DARK, fg="white", font=("Segoe UI Semibold", 12),
            padx=16, pady=10,
        ).pack(anchor=tk.W)
        tk.Frame(dlg, bg=GOLD, height=2).pack(fill=tk.X)

        body = ttk.Frame(dlg, padding=16, style="Card.TFrame")
        body.pack(fill=tk.BOTH, expand=True)

        def step(n, title, text):
            row = ttk.Frame(body, style="Card.TFrame")
            row.pack(fill=tk.X, pady=(0, 10), anchor=tk.W)
            badge = tk.Label(
                row, text=str(n), bg=GREEN, fg="white",
                font=("Segoe UI Semibold", 10), width=2, pady=1,
            )
            badge.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 10), pady=(1, 0))
            txt = ttk.Frame(row, style="Card.TFrame")
            txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Label(txt, text=title, style="Card.TLabel",
                      font=("Segoe UI Semibold", 10)).pack(anchor=tk.W)
            ttk.Label(txt, text=text, style="CardMuted.TLabel",
                      wraplength=430, justify=tk.LEFT).pack(anchor=tk.W)
            return txt

        step1 = step(
            1, "Get an OpenAI API key",
            "The verifier reads footnotes with OpenAI's models, billed to your "
            "own OpenAI account (a typical article costs well under a dollar). "
            "Create a key at platform.openai.com — you will need an account "
            "with a payment method. Tip: set a monthly spending limit there "
            "so there can never be a billing surprise.",
        )
        link = ttk.Label(step1, text="Open platform.openai.com/api-keys ↗",
                         style="Link.TLabel", cursor="hand2")
        link.pack(anchor=tk.W, pady=(3, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(KEY_SIGNUP_URL))

        step2 = step(
            2, "Enter the key here",
            "Paste the key once; it is stored encrypted for your Windows "
            "account, so you won't be asked again on this computer. Treat "
            "the key like a password — don't share it or send it by email; "
            "this app only ever sends it to OpenAI.",
        )
        self._onboard_key_status = tk.StringVar()
        ttk.Label(step2, textvariable=self._onboard_key_status,
                  style="CardMuted.TLabel").pack(anchor=tk.W, pady=(3, 0))

        def enter_key():
            self._prompt_api_key()
            self._onboard_key_status.set(self.key_status_var.get())

        ttk.Button(step2, text="Enter key now…", style="Secondary.TButton",
                   command=enter_key).pack(anchor=tk.W, pady=(4, 0))
        self._onboard_key_status.set(self.key_status_var.get())

        step(
            3, "Verify your documents",
            "Add or drag .docx files into the window and press Run "
            "verification. For each document you get an Excel workbook in the "
            "CHECKED_EDITS folder with suggested citation links, corrections, "
            "and quotation checks.",
        )

        foot = ttk.Frame(dlg, padding=(16, 8, 16, 12), style="Card.TFrame")
        foot.pack(fill=tk.X)
        dont_var = tk.BooleanVar(value=False)
        if not from_settings:
            ttk.Checkbutton(
                foot, text="Don't show this again",
                variable=dont_var, style="Card.TCheckbutton",
            ).pack(side=tk.LEFT)

        def close():
            if dont_var.get() or aqv._resolve_api_key():
                self.saved_settings["onboarding_done"] = True
                _save_settings({**_settings_with_defaults(self.saved_settings),
                                "window_geometry": self.root.geometry(),
                                "onboarding_done": True})
            dlg.destroy()

        ttk.Button(foot, text="Get started", style="Secondary.TButton",
                   command=close).pack(side=tk.RIGHT)
        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + 60
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def _set_busy(self, busy):
        self.running = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.run_btn.config(state=state, text="Running…" if busy else "▶   Run verification")
        self.btn_add.config(state=state)
        self.btn_remove.config(state=state)
        self.btn_clear.config(state=state)
        self.pause_btn.config(
            state=tk.NORMAL if busy else tk.DISABLED, text="⏸   Pause")
        if not busy:
            self.run_started_at = 0.0
            self._paused = False
            self._pause_requested = False
            self._starting = False
            self._resume_evt.set()
            self._clear_headline_extras()

    # ------------------------------------------------------------------
    # Pause / resume (takes effect before the next slow operation; with
    # parallel articles, every worker blocks on the same gate)
    # ------------------------------------------------------------------
    def _pause_gate(self):
        """Runs on the worker thread between footnotes/documents; blocks
        there while the user has the run paused."""
        if self._pause_requested:
            self._pause_requested = False
            self._resume_evt.clear()
            self.root.after(0, self._enter_paused_ui)
        self._resume_evt.wait()

    def _toggle_pause(self):
        if not self.running:
            return
        if self._paused:
            self._paused = False
            # keep the elapsed clock and footnote-rate ETA honest: don't
            # count the paused time
            pause_delta = time.monotonic() - self._pause_started_at
            self.run_started_at += pause_delta
            if self._analyze_started_at:
                self._analyze_started_at += pause_delta
            self._fn_samples = [
                (n, t + pause_delta) for n, t in self._fn_samples]
            self._resume_evt.set()
            self.pause_btn.config(text="⏸   Pause", state=tk.NORMAL)
            self.run_btn.config(text="Running…")
            self.status_main_var.set(self._status_before_pause)
            for dv in self.doc_views:
                dv.on_resumed()
            self._log("▶ Resumed.\n")
            return
        if self._pause_requested:
            return
        self._pause_requested = True
        self.pause_btn.config(text="Pausing…", state=tk.DISABLED)
        for dv in self.doc_views:
            dv.on_pausing()
        self._log("⏸ Pausing — finishing the current operation…\n")

    def _enter_paused_ui(self):
        self._paused = True
        self._pause_started_at = time.monotonic()
        self._status_before_pause = self.status_main_var.get()
        self.status_main_var.set("Paused")
        done, known, unknown = self._agg_fn()
        fn_part = (
            f"Footnote {done} of {known}{'+' if unknown else ''}   ·   "
            if known else ""
        )
        self.status_sub_var.set(f"{fn_part}Paused — press Resume to continue.")
        for dv in self.doc_views:
            dv.on_paused()
        self.pause_btn.config(text="▶   Resume", state=tk.NORMAL)
        self.run_btn.config(text="Paused")
        self._log("⏸ Paused — press Resume to continue.\n")

    def _finish_run(self):
        self._set_busy(False)

    def _run(self):
        if self.running:
            return

        docx_files = [p for p in self.files if p.lower().endswith(".docx")]
        if not docx_files:
            messagebox.showwarning("No files", "Please add at least one .docx file.")
            return

        local_only_var = getattr(self, "local_only_var", None)
        local_only = bool(local_only_var and local_only_var.get())
        if local_only and not self._a2aj_corpus_installed():
            messagebox.showwarning(
                "Local corpus required",
                "Local only needs the complete A2AJ corpus. Install or resume "
                "it in Settings before starting this run.",
            )
            return
        selected_run_mode = (
            "free" if local_only else
            RUN_MODE_LABELS.get(self.run_mode_var.get(), "high_accuracy")
        )
        if selected_run_mode != "free" and not self._ensure_api_key():
            return

        out_folder = _default_output_folder()
        self.out_var.set(out_folder)

        fn_ids_raw = self.fn_filter_var.get().strip() or None
        if fn_ids_raw and not messagebox.askyesno(
            "Partial run",
            "The footnote filter is set to "
            f"“{fn_ids_raw}”, so only those footnotes will be "
            "verified.\n\nRun anyway?\n\nTo check every footnote, choose "
            "No and clear the Footnote filter field in Settings.",
        ):
            return
        dry_fire, use_db_search = _source_run_flags(self)
        aqv._configure_from_args(_build_args(
            dry_fire,
            fn_ids_raw,
            use_a2aj=self.a2aj_var.get(),
            use_db_search=use_db_search,
            text_fragment_mode=self.frag_mode_var.get(),
            export_detail=EXPORT_DETAIL_LABELS.get(
                self.export_detail_var.get(),
                DEFAULT_GUI_SETTINGS["export_detail"],
            ),
            llm_cache=self.llm_cache_var.get(),
            run_mode=selected_run_mode,
            local_only=local_only,
            supra_linking=SUPRA_LINKING_LABELS.get(
                self.supra_linking_var.get(), "safe"
            ),
        ))

        self.run_started_at = time.monotonic()
        self._eta_ema = None
        self._analyze_started_at = 0.0
        self._fn_samples = []
        self._thread_docs = {}
        self._gov = None
        self._gov_announced = False
        self._slots = None
        self._slots_extra_pending = 0
        self._extra_released = 0
        self._setup_run_views([Path(p).name for p in docx_files])
        self.status_main_var.set("Starting…")
        self.status_sub_var.set("")
        self.progress.config(value=0)
        self._log_rule("Verification started")
        self._set_busy(True)
        self._starting = True  # spin the Run button until the first article begins
        self.run_btn.config(text=f"{self._SPIN_FRAMES[0]}  Starting…")
        worker_settings = (
            fn_ids_raw,
            self._parallel_cap(),
            bool(self.term_only_var.get()),
            bool(self.open_after_var.get()),
            bool(self.open_folder_var.get()),
        )
        threading.Thread(
            target=self._run_worker,
            args=(out_folder, docx_files, *worker_settings),
            daemon=True).start()

    def _run_worker(
        self, out_folder, docx_files, fn_ids_raw, requested_cap,
        quiet_log, open_after, open_folder,
    ):
        old_stdout = sys.stdout
        try:
            if overlay_store.pending_names():
                self.root.after(
                    0, self._log,
                    "Waiting for the one-time database preparation to finish…\n",
                )
            overlay_store.ensure_all()  # already available after the first launch
            fn_ids = aqv._parse_footnote_ids(fn_ids_raw)
            os.makedirs(out_folder, exist_ok=True)

            # Reserve every workbook name up front: with articles running in
            # parallel, the exists-check alone can no longer prevent two
            # documents with the same stem from claiming one output path.
            jobs = []
            reserved: set[str] = set()
            for i, docx_path in enumerate(docx_files):
                out_name, out_path = self._reserve_out_path(
                    out_folder, docx_path, reserved)
                reserved.add(out_path.lower())
                jobs.append((self.doc_views[i], docx_path, out_path, out_name))

            total = len(jobs)
            requested = min(requested_cap, total)
            gov = None
            if requested > 1:
                from verifier_core.rate_governor import RateLimitGovernor
                gov = RateLimitGovernor(max_parallel=requested)
            self._gov = gov
            aqv._LLM_GOVERNOR = gov
            # Articles start staggered: the first immediately, the rest once
            # the governor has read the account's rate limits from a real
            # response and confirmed there is headroom (see _poll_log).
            self._slots = threading.Semaphore(1)
            self._extra_released = 0
            self._slots_extra_pending = requested - 1

            # Redirect stdout so engine messages appear in the GUI log; the
            # redirect resolves the printing thread to its article as each
            # line is written, which routes it to the right Activity tab.
            sys.stdout = _LogRedirect(
                self.log_queue.put, quiet=quiet_log,
                resolve=self._thread_docs.get)

            results: dict[int, tuple[bool, str]] = {}
            with ThreadPoolExecutor(max_workers=max(requested, 1)) as pool:
                futures = [
                    pool.submit(self._verify_one, dv, path, out_path, out_name,
                                fn_ids, results, open_after)
                    for dv, path, out_path, out_name in jobs
                ]
                for f in futures:
                    f.result()

            output_paths = [results[k][1] for k in sorted(results) if results[k][0]]
            failures = sum(1 for ok, _p in results.values() if not ok)

            elapsed = int(time.monotonic() - self.run_started_at)
            mins, secs = divmod(elapsed, 60)
            summary = f"{len(output_paths)} of {total} file(s) exported in {mins}:{secs:02d}"
            if failures:
                summary += f" ({failures} failed)"
            self.root.after(0, self._log_rule, "Finished")
            self.root.after(0, self._log, summary + "\n", "err" if failures else "ok")
            self.root.after(0, self.status_main_var.set,
                            "Finished with failures" if failures else "Finished")
            self.root.after(0, self.status_sub_var.set, summary)
            self.root.after(0, self.progress.config, {"value": 1000})

            if open_folder:
                try:
                    folder_to_open = Path(output_paths[-1]).resolve().parent if output_paths else Path(out_folder).resolve()
                    _open_path(str(folder_to_open))
                except Exception as e:
                    self.root.after(0, self._log, f"Could not open output folder: {e}\n", "warn")
        except Exception as e:
            self.root.after(0, self._log, f"Error: {e}\n", "err")
        finally:
            sys.stdout = old_stdout
            aqv._LLM_GOVERNOR = None
            self._gov = None
            self._slots = None
            self._slots_extra_pending = 0
            self.root.after(0, self._finish_run)

    @staticmethod
    def _reserve_out_path(out_folder, docx_path, reserved):
        """Pick a workbook name that collides neither with disk nor with a
        name already promised to another article in this batch."""
        stem = Path(docx_path).stem
        base_stem = f"[CHECKED] {stem}"
        out_name = f"{base_stem}.xlsx"
        out_path = os.path.join(out_folder, out_name)
        suffix = 0
        while out_path.lower() in reserved or os.path.exists(out_path):
            suffix += 1
            if suffix > 99:
                # Last resort: a timestamped name that cannot collide,
                # rather than overwriting the _99_ workbook.
                stem_trunc = f"_{int(time.time())}_{base_stem}"
            else:
                stem_trunc = f"_{suffix}_{base_stem}"
            max_stem = 250 - len(out_folder) - len(".xlsx") - 2
            if len(stem_trunc) > max_stem:
                stem_trunc = stem_trunc[:max_stem]
            out_name = f"{stem_trunc}.xlsx"
            out_path = os.path.join(out_folder, out_name)
            if suffix > 99:
                break
        return out_name, out_path

    def _parallel_cap(self):
        choice = PARALLEL_LABELS.get(
            self.parallel_var.get(), DEFAULT_GUI_SETTINGS["parallel_files"])
        if choice == "auto":
            return MAX_PARALLEL_DOCS
        try:
            return max(1, int(choice))
        except ValueError:
            return 1

    def _verify_one(
        self, dv, docx_path, out_path, out_name, fn_ids, results, open_after,
    ):
        """One article end to end, on its own worker thread."""
        self._slots.acquire()
        ident = threading.get_ident()
        self._thread_docs[ident] = dv
        try:
            self._pause_gate()
            self.root.after(0, self._log, f"▸ {dv.name}\n", "head", dv)
            actual_out_path = run_audit(
                docx_path, out_path, max_lookahead=400, footnote_ids=fn_ids)
            results[dv.index] = (True, actual_out_path or out_path)
            self.root.after(0, self._log, f"✓ {out_name}\n", "ok", dv)
            if open_after:
                _open_path(actual_out_path or out_path)
        except Exception as e:
            results[dv.index] = (False, "")
            self.root.after(0, self._log, f"✗ {dv.name} failed: {e}\n", "err", dv)
        finally:
            self._thread_docs.pop(ident, None)
            self.root.after(0, self._on_doc_finished)
            self._slots.release()

    def _on_doc_finished(self):
        # A finished article frees its slot for the next queued one (the
        # semaphore release above). If the governor never saw a model
        # response — a fully cached run — there is no API pressure to
        # protect against, so open the remaining slots too.
        if (self._slots is not None and self._slots_extra_pending > 0
                and (self._gov is None or self._gov.suggested_parallel is None)):
            for _ in range(self._slots_extra_pending):
                self._slots.release()
            self._slots_extra_pending = 0
        self._update_bar()

    # ------------------------------------------------------------------
    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("Running", "Verification is in progress. Quit anyway?"):
                return
        self._save_current_settings()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _run_cli_smoke_from_env() -> bool:
    """Release-QA hook: run the ordinary CLI through the frozen GUI binary."""
    raw = os.environ.get("ALR_CLI_SMOKE_ARGS")
    if not raw:
        return False
    argv = json.loads(raw)
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("ALR_CLI_SMOKE_ARGS must be a JSON array of strings")

    live_calls = 0
    original_call = aqv._llm_call

    def counted_call(**kwargs):
        nonlocal live_calls
        live_calls += 1
        print(f"CLI smoke: live LLM call {live_calls}", flush=True)
        return original_call(**kwargs)

    aqv._llm_call = counted_call
    try:
        aqv._main(argv)
    finally:
        aqv._llm_call = original_call
    print(f"CLI smoke: live LLM calls={live_calls}", flush=True)
    return True


def _configure_process_log_from_env():
    """Give windowed release-QA runs an explicit, line-buffered text log."""
    path = os.environ.get("ALR_PROCESS_LOG")
    if not path:
        return None
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = destination.open("w", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    return stream


if __name__ == "__main__":
    _process_log = _configure_process_log_from_env()
    if _run_cli_smoke_from_env():
        raise SystemExit(0)
    _startup_probe = os.environ.get("ALR_STARTUP_PROBE")
    if _startup_probe:
        import duckdb
        with duckdb.connect() as _probe_db:
            _probe_db.execute("SELECT TIMESTAMPTZ '2024-01-01 00:00:00+00'").fetchone()
    app = ALRQuoteVerifierGUI()
    if _startup_probe == "layout":
        app.notebook.select(app.settings_tab)
        app.root.update()
        actual = app.reset_settings_btn.winfo_height()
        requested = app.reset_settings_btn.winfo_reqheight()
        print(f"Layout probe: Reset button {actual}/{requested}px", flush=True)
        app.root.destroy()
        raise SystemExit(0 if actual >= requested else 1)
    if _startup_probe:
        # Build-verification hook: exit as soon as the window is up and the
        # one-time overlay extraction (if any) has finished, so launch time
        # can be measured as plain process wall time.
        def _probe_poll():
            if not overlay_store.pending_names():
                app.root.destroy()
            else:
                app.root.after(500, _probe_poll)
        app.root.after(500, _probe_poll)
    app.run()

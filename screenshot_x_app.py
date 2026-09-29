#!/usr/bin/env python3
"""
Xスクショ管理アプリ（画面版）。

    取得   … アカウントと期間を指定して、投稿を1件ずつ開きスクショを貯める
    書き出し … 貯めた中から期間を指定して、提出用フォルダ／sidenote用JSONを作る

起動:
    start_app.bat をダブルクリック（または python screenshot_x_app.py）

保存先はアカウント名から自動で決まる（アーカイブ置き場 ＼ アカウント名）ので、
毎回フォルダを選ぶ必要がなく、別のアカウントと混ざることもない。

    x_archive\\exampleuser\\
        posts\\20260801_0912_2077xxxxxxxxxxxxxxxxx.png
        index.csv / manifest.json
    x_archive\\書き出し\\
        提出用_exampleuser_2026-08-01_2026-08-31\\001_….png ＋ 一覧.csv
        sidenote-exampleuser_20260801-20260831.json

撮影はブラウザのウィンドウに直接描かせる方式(PrintWindow)なので、
実行中に他のウィンドウを使っても、そちらが写り込むことはない。
"""
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk

import screenshot_x_detail as detail
import screenshot_x_export as export
from screenshot_x import normalize_account

# PyInstallerでexe化した場合、__file__は実行のたびに変わる一時展開フォルダを指してしまい
# (そのフォルダは終了時に消える)、設定(settings.json)が保存されなくなる。
# 実行ファイル本体の場所を使うことで、sourceのまま動かした場合と同じ挙動をexeでも保つ。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = SCRIPT_DIR / "settings.json"

# 仕分け画面でのスクショの表示幅(論理px)。返信まで撮ると縦に長くなるため、
# 高さではなく幅を合わせ、足りない分は縦スクロールで見る。
# 論理pxで縮めても画面の拡大率の分だけ拡大されて描かれるので、
# 拡大率125%の環境では 606 の指定が 758実px（原寸875の87%）になり、文字が読める。
TRIAGE_IMAGE_WIDTH = 606
# 表示枠の高さの上限・下限。実際の高さはウィンドウが画面に収まるよう実測して決める。
TRIAGE_VIEW_MAX_HEIGHT = 700
TRIAGE_VIEW_MIN_HEIGHT = 360


# ---- 見た目（サイドノート / pdf-tools と同じデザイントークン） ----
COLOR_TEXT = "#1e2126"
COLOR_MUTED = "#4d4d4d"
COLOR_MUTED2 = "#71717a"
COLOR_BORDER = "#e5e5e5"
COLOR_PAGE = "#f4f5f7"     # 画面全体の背景
COLOR_HERO = "#eef0f3"     # 上部の帯
COLOR_SWATCH = "#f2f2f2"   # 「機能」ボタンの地色
COLOR_SWATCH_HOVER = "#e8e8ea"


def apply_theme(root):
    """ttkの見た目を、サイドノート風（グレー地・白いカード・黒い選択ボタン）に寄せる。
    tkinterは角丸や影を描けないので、配色・余白・枠線・選択中の黒反転だけを合わせている。"""
    root.configure(background=COLOR_PAGE)
    families = set(tkfont.families())
    family = next((f for f in ("Yu Gothic UI", "Meiryo UI", "Segoe UI") if f in families), None)
    if family:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=10)
    fam = family or "TkDefaultFont"

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background="white", foreground=COLOR_TEXT, bordercolor=COLOR_BORDER,
                 lightcolor="white", darkcolor="white", focuscolor="white", troughcolor=COLOR_SWATCH)

    # ページ地（カードの外側）に置く部品用
    st.configure("Page.TFrame", background=COLOR_PAGE)
    st.configure("Page.TLabel", background=COLOR_PAGE, foreground=COLOR_MUTED)
    st.configure("Hero.TFrame", background=COLOR_HERO)
    st.configure("HeroTitle.TLabel", background=COLOR_HERO, foreground=COLOR_TEXT, font=(fam, 20, "bold"))
    st.configure("HeroDesc.TLabel", background=COLOR_HERO, foreground=COLOR_MUTED, font=(fam, 11))
    st.configure("HeroNote.TLabel", background=COLOR_HERO, foreground=COLOR_MUTED2, font=(fam, 9))
    st.configure("HeroHead.TLabel", background=COLOR_HERO, foreground=COLOR_TEXT, font=(fam, 11, "bold"))

    # 通常のボタン＝白抜き、Primary＝黒塗り、Swatch＝「機能」ボタン（選択中は黒に反転）
    st.configure("TButton", background="white", foreground=COLOR_TEXT, bordercolor="#d4d4d8",
                 padding=(14, 6), relief="flat", borderwidth=1)
    st.map("TButton", background=[("disabled", "#f5f6f8"), ("active", "#f4f4f5")],
           foreground=[("disabled", "#b7bac2")], bordercolor=[("disabled", "#eeeeee")])
    st.configure("Primary.TButton", background=COLOR_TEXT, foreground="white", bordercolor=COLOR_TEXT,
                 padding=(16, 6))
    st.map("Primary.TButton", background=[("disabled", "#f5f6f8"), ("active", "#262626")],
           foreground=[("disabled", "#b7bac2")], bordercolor=[("disabled", "#eeeeee")])
    st.configure("Swatch.TButton", background=COLOR_SWATCH, foreground=COLOR_TEXT, bordercolor=COLOR_BORDER,
                 padding=(10, 8))
    st.map("Swatch.TButton", background=[("active", COLOR_SWATCH_HOVER)])
    st.configure("SwatchSel.TButton", background=COLOR_TEXT, foreground="white", bordercolor=COLOR_TEXT,
                 padding=(10, 8), font=(fam, 10, "bold"))
    st.map("SwatchSel.TButton", background=[("active", COLOR_TEXT)])

    for name in ("TCheckbutton", "TRadiobutton"):
        st.configure(name, background="white", indicatorcolor="white", indicatorbackground="white")
        st.map(name, background=[("active", "white")],
               indicatorcolor=[("selected", COLOR_TEXT)], indicatorbackground=[("selected", COLOR_TEXT)])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(name, fieldbackground="white", bordercolor=COLOR_BORDER, padding=5)
        st.map(name, bordercolor=[("focus", COLOR_TEXT)])
    st.configure("Vertical.TScrollbar", background=COLOR_SWATCH, troughcolor="white", bordercolor="white",
                 arrowcolor=COLOR_MUTED2)

    # タブ帯は隠す（切り替えは上部の「機能」ボタンで行う）。中身の枠だけをカード風に残す。
    st.layout("TNotebook", [("Notebook.client", {"sticky": "nswe"})])
    st.layout("TNotebook.Tab", [])
    st.configure("TNotebook", background=COLOR_PAGE, bordercolor=COLOR_BORDER, borderwidth=1, tabmargins=0)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Xスクショ管理")
        root.minsize(700, 620)
        apply_theme(root)

        self.messages = queue.Queue()   # 作業スレッド → 画面 への連絡
        self.worker = None
        self.stop_flag = threading.Event()
        self.login_done = threading.Event()

        settings = self.load_settings()
        today = date.today()
        self.account = tk.StringVar(value=settings.get("account", ""))
        self.archive_root = tk.StringVar(value=settings.get("archive_root", str(detail.DEFAULT_ARCHIVE_ROOT)))
        self.width = tk.StringVar(value=str(settings.get("width", detail.DEFAULT_WINDOW_WIDTH)))
        self.status = tk.StringVar(value="待機中")
        self.save_dir_label = tk.StringVar(value="")

        # 仕分け画面の状態
        self.triage_archive = None
        self.triage_posts = []
        self.triage_index = 0
        self.triage_photo = None
        self.triage_fitted = False
        self.work_height = detail.get_screen_metrics()["work_height"]

        # 取得タブ
        self.period_mode = tk.StringVar(value=settings.get("period_mode", "range"))
        self.get_from = tk.StringVar(value=settings.get("get_from", f"{today - timedelta(days=30):%Y-%m-%d}"))
        self.get_to = tk.StringVar(value=settings.get("get_to", f"{today:%Y-%m-%d}"))
        self.back_count = tk.StringVar(value=settings.get("back_count", "3"))
        self.back_unit = tk.StringVar(value=settings.get("back_unit", "月"))
        self.full_page = tk.BooleanVar(value=settings.get("full_page", True))

        # 書き出しタブ
        self.exp_from = tk.StringVar(value=settings.get("exp_from", f"{today - timedelta(days=30):%Y-%m-%d}"))
        self.exp_to = tk.StringVar(value=settings.get("exp_to", f"{today:%Y-%m-%d}"))
        self.want_images = tk.BooleanVar(value=settings.get("want_images", False))
        self.want_json = tk.BooleanVar(value=settings.get("want_json", True))
        self.want_pdf = tk.BooleanVar(value=settings.get("want_pdf", True))
        self.min_rank = tk.StringVar(value=settings.get("min_rank", "0"))
        saved_items = settings.get("note_items", {})
        self.note_items = {
            key: tk.BooleanVar(value=saved_items.get(key, True)) for key in export.NOTE_ITEMS
        }

        self.build_hero()
        self.build_header()
        self.build_tabs()
        self.build_log()
        self.build_statusbar()

        self.account.trace_add("write", lambda *_: self.refresh_account_info())
        self.archive_root.trace_add("write", lambda *_: self.refresh_account_info())
        self.refresh_account_info()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.pump)

    # ------------------------------------------------------------ 画面（共通）

    def build_hero(self):
        """上部の帯（sidenote / pdf-tools のヒーローと同じ構成）。アプリ名・一言・「機能」ボタン。
        「機能」ボタンは、タブ帯を隠したNotebookを切り替える（build_tabsで作る）。"""
        hero = ttk.Frame(self.root, style="Hero.TFrame", padding=(20, 14, 20, 12))
        hero.pack(fill="x")
        ttk.Label(hero, text="Xスクショ管理", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(hero, text="Xの投稿を、証拠として提出できる形で。", style="HeroDesc.TLabel").pack(
            anchor="w", pady=(2, 0))
        ttk.Label(hero, text="処理はすべてこのパソコンの中だけで行われ、外部には送信されません。",
                  style="HeroNote.TLabel").pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(hero, style="Hero.TFrame")
        row.pack(anchor="w")
        ttk.Label(row, text="機能", style="HeroHead.TLabel").pack(side="left", padx=(0, 12))
        self.swatch_row = row
        tk.Frame(self.root, height=1, background=COLOR_BORDER).pack(fill="x")

    def build_header(self):
        outer = ttk.Frame(self.root, style="Page.TFrame", padding=(16, 12, 16, 0))
        outer.pack(fill="x")
        card = tk.Frame(outer, background="white", highlightthickness=1, highlightbackground=COLOR_BORDER)
        card.pack(fill="x")
        frame = ttk.Frame(card, padding=(14, 10, 14, 10))
        frame.pack(fill="x")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="アカウント").grid(row=0, column=0, sticky="w", pady=4)
        entry = ttk.Combobox(frame, textvariable=self.account, values=self.known_accounts())
        entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        self.account_box = entry

        ttk.Label(frame, text="保存先").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(frame, textvariable=self.save_dir_label, foreground="#555").grid(
            row=1, column=1, sticky="w", pady=4
        )
        ttk.Button(frame, text="変更…", command=self.choose_root).grid(row=1, column=2, sticky="e")

    def build_tabs(self):
        holder = ttk.Frame(self.root, style="Page.TFrame", padding=(16, 12, 16, 0))
        holder.pack(fill="x")
        book = ttk.Notebook(holder, padding=(12, 8))
        book.pack(fill="x")
        self.build_get_tab(book)
        self.build_triage_tab(book)
        self.build_export_tab(book)
        book.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.book = book

        # 「機能」ボタン：押すとそのタブへ切り替わり、選ばれているものが黒く反転する
        self.swatches = []
        for i in range(len(book.tabs())):
            button = ttk.Button(self.swatch_row, text=book.tab(i, "text"), style="Swatch.TButton",
                                width=12, command=lambda n=i: book.select(n))
            button.pack(side="left", padx=(0, 8))
            self.swatches.append(button)
        self.sync_swatches()

    def sync_swatches(self):
        current = self.book.index(self.book.select())
        for i, button in enumerate(self.swatches):
            button.configure(style="SwatchSel.TButton" if i == current else "Swatch.TButton")

    def on_tab_changed(self, _event=None):
        """仕分けタブに切り替わったときだけ読み込む（取得の直後でも最新になる）"""
        self.sync_swatches()
        triage = self.book.tab(self.book.select(), "text") == "仕分け"
        if not triage:
            self.commit_memo()   # 書きかけのメモを捨てずに保存してから離れる
        self.show_log(not triage)
        if triage:
            self.triage_fitted = False   # ログ欄の有無で使える高さが変わるので測り直す
            self.load_triage()

    def build_get_tab(self, book):
        tab = ttk.Frame(book, padding=12)
        book.add(tab, text="取得")
        tab.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            tab, text="日付で指定", value="range", variable=self.period_mode
        ).grid(row=0, column=0, sticky="w", pady=3)
        row = ttk.Frame(tab)
        row.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Entry(row, textvariable=self.get_from, width=12).pack(side="left")
        ttk.Label(row, text=" 〜 ").pack(side="left")
        ttk.Entry(row, textvariable=self.get_to, width=12).pack(side="left")
        ttk.Label(row, text="  （2026-08-01 の形式）", foreground="#777").pack(side="left")

        ttk.Radiobutton(
            tab, text="遡って指定", value="back", variable=self.period_mode
        ).grid(row=1, column=0, sticky="w", pady=3)
        row2 = ttk.Frame(tab)
        row2.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(row2, text="過去 ").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=120, width=5, textvariable=self.back_count).pack(side="left")
        ttk.Radiobutton(row2, text="か月", value="月", variable=self.back_unit).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(row2, text="日", value="日", variable=self.back_unit).pack(side="left", padx=(6, 0))

        ttk.Label(tab, text="撮る範囲").grid(row=2, column=0, sticky="w", pady=3)
        row3 = ttk.Frame(tab)
        row3.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(row3, text="返信もすべて撮る（下までスクロールして繋げます）",
                        variable=self.full_page).pack(anchor="w")
        ttk.Label(row3, text="外すと1画面に収まる範囲だけになり、返信は途中までしか写りません。",
                  foreground="#777").pack(anchor="w")

        ttk.Label(tab, text="ウィンドウ幅").grid(row=3, column=0, sticky="w", pady=3)
        row4 = ttk.Frame(tab)
        row4.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Spinbox(row4, from_=400, to=1600, increment=20, width=6, textvariable=self.width).pack(side="left")
        ttk.Label(row4, text=" px（700で「アドレスバー + 投稿の列」だけが写ります）", foreground="#777").pack(side="left")

        bar = ttk.Frame(tab)
        bar.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.start_button = ttk.Button(bar, text="取得を開始", command=self.start_capture, style="Primary.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="中止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))

        ttk.Label(tab, text="これまでの取得").grid(row=5, column=0, sticky="nw", pady=(12, 0))
        self.history_label = ttk.Label(tab, text="（まだありません）", foreground="#555", justify="left")
        self.history_label.grid(row=5, column=1, sticky="w", pady=(12, 0))

    def build_triage_tab(self, book):
        """1枚ずつ見て重要度とメモを付ける画面。キーの1/2/3で採点して自動で次へ進む。"""
        tab = ttk.Frame(book, padding=12)
        book.add(tab, text="仕分け")

        header = ttk.Frame(tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Button(header, text="← 前へ", command=lambda: self.triage_move(-1)).pack(side="left")
        self.triage_pos = ttk.Label(header, text="0 / 0 枚", font=("", 11, "bold"))
        self.triage_pos.pack(side="left", padx=16)
        ttk.Button(header, text="次へ →", command=lambda: self.triage_move(1)).pack(side="left")
        self.triage_date = ttk.Label(header, text="", foreground="#555")
        self.triage_date.pack(side="left", padx=(20, 0))
        ttk.Button(header, text="未採点へ", command=self.triage_next_unranked).pack(side="right")
        # 進み具合は右の列に置くとウィンドウが画面より高くなるので、ここに1行で出す
        self.triage_progress = ttk.Label(header, text="", foreground="#555")
        self.triage_progress.pack(side="right", padx=(0, 16))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        # 返信まで撮ったスクショは縦に長いので、枠に収めて縦スクロールで見る
        view = ttk.Frame(body)
        view.pack(side="left", anchor="n")
        self.triage_canvas = tk.Canvas(
            view, width=TRIAGE_IMAGE_WIDTH, height=TRIAGE_VIEW_MAX_HEIGHT,
            highlightthickness=1, highlightbackground=COLOR_BORDER, background="white", cursor="hand2",
        )
        scroll = ttk.Scrollbar(view, orient="vertical", command=self.triage_canvas.yview)
        self.triage_canvas.configure(yscrollcommand=scroll.set)
        self.triage_canvas.pack(side="left")
        scroll.pack(side="left", fill="y")
        self.triage_canvas.bind("<Button-1>", lambda _e: self.open_current_image())
        self.triage_canvas.bind(
            "<MouseWheel>", lambda e: self.triage_canvas.yview_scroll(-e.delta // 120, "units")
        )

        side = ttk.Frame(body, padding=(14, 0, 0, 0))
        side.pack(side="left", fill="y")
        self.triage_side = side

        ttk.Label(side, text="重要度", font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(side, text="キーの 1 / 2 / 3 でも選べます\n選ぶと自動で次の1枚へ進みます",
                  foreground="#666", justify="left").pack(anchor="w", pady=(2, 8))
        self.triage_rank = tk.StringVar(value="")
        for value, label in (("3", "3  重要"), ("2", "2  普通"), ("1", "1  対象外")):
            ttk.Radiobutton(side, text=label, value=value, variable=self.triage_rank,
                            command=lambda: self.set_rank(int(self.triage_rank.get()))).pack(anchor="w", pady=2)
        ttk.Button(side, text="未採点に戻す", command=lambda: self.set_rank(0, advance=False)).pack(
            anchor="w", pady=(8, 0)
        )

        ttk.Label(side, text="メモ", font=("", 11, "bold")).pack(anchor="w", pady=(18, 2))
        ttk.Label(side, text="書き出すとサイドノートに\n引き継がれます",
                  foreground="#666", justify="left").pack(anchor="w", pady=(0, 4))
        memo_box = ttk.Frame(side)
        memo_box.pack(anchor="w", fill="x")
        self.triage_memo = tk.Text(memo_box, width=26, height=10, wrap="word", relief="flat",
                                   highlightthickness=1, highlightbackground=COLOR_BORDER,
                                   highlightcolor=COLOR_TEXT, padx=8, pady=6)
        memo_scroll = ttk.Scrollbar(memo_box, command=self.triage_memo.yview)
        self.triage_memo.configure(yscrollcommand=memo_scroll.set)
        self.triage_memo.pack(side="left")
        memo_scroll.pack(side="left", fill="y")

        ttk.Label(side, text="投稿の下が切れているときは、\n画像をクリックすると原寸で開きます",
                  foreground="#888", justify="left").pack(anchor="w", pady=(10, 0))

        # 1/2/3 で採点、← → で移動。メモ欄に入力中は横取りしない。
        for key in ("1", "2", "3"):
            self.root.bind(f"<KeyPress-{key}>", self.on_rank_key)
        self.root.bind("<Left>", lambda e: self.triage_key_move(e, -1))
        self.root.bind("<Right>", lambda e: self.triage_key_move(e, 1))

    def build_export_tab(self, book):
        tab = ttk.Frame(book, padding=12)
        book.add(tab, text="書き出し")
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="期間").grid(row=0, column=0, sticky="w", pady=3)
        row = ttk.Frame(tab)
        row.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Entry(row, textvariable=self.exp_from, width=12).pack(side="left")
        ttk.Label(row, text=" 〜 ").pack(side="left")
        ttk.Entry(row, textvariable=self.exp_to, width=12).pack(side="left")

        ttk.Label(tab, text="重要度").grid(row=1, column=0, sticky="w", pady=3)
        row1 = ttk.Frame(tab)
        row1.grid(row=1, column=1, sticky="w", pady=3)
        for value, label in (("0", "すべて"), ("2", "普通以上"), ("3", "重要のみ")):
            ttk.Radiobutton(row1, text=label, value=value, variable=self.min_rank).pack(side="left", padx=(0, 12))

        ttk.Label(tab, text="作るもの").grid(row=2, column=0, sticky="nw", pady=3)
        row2 = ttk.Frame(tab)
        row2.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(row2, text="PDF（出典とメモだけ。サイドノートアプリを経由しません）",
                        variable=self.want_pdf).pack(anchor="w")
        ttk.Checkbutton(row2, text="sidenote用JSON（注釈を書き足したいとき）",
                        variable=self.want_json).pack(anchor="w")
        ttk.Checkbutton(row2, text="提出用フォルダ（001…の連番コピー ＋ 一覧.csv）",
                        variable=self.want_images).pack(anchor="w")

        ttk.Label(tab, text="サイドノート\nの内容", justify="left").grid(row=3, column=0, sticky="nw", pady=3)
        row3 = ttk.Frame(tab)
        row3.grid(row=3, column=1, sticky="w", pady=3)
        for key in export.NOTE_ITEMS:
            ttk.Checkbutton(row3, text=export.NOTE_ITEM_LABELS[key],
                            variable=self.note_items[key]).pack(side="left", padx=(0, 10))
        ttk.Label(tab, text="PDF・sidenote用JSON の両方に効きます。すべて外すと画像だけになります。",
                  foreground="#777").grid(row=4, column=1, sticky="w")

        bar = ttk.Frame(tab)
        bar.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.export_button = ttk.Button(bar, text="書き出す", command=self.start_export, style="Primary.TButton")
        self.export_button.pack(side="left")
        ttk.Button(bar, text="書き出し先を開く", command=self.open_export_dir).pack(side="left", padx=(8, 0))

        ttk.Label(
            tab,
            text="原本には手を触れません。連番はここで初めて振るので、\n"
                 "何度書き出しても、既に引用した番号がずれることはありません。",
            foreground="#777", justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def build_log(self):
        frame = ttk.Frame(self.root, style="Page.TFrame", padding=(16, 12, 16, 4))
        frame.pack(fill="both", expand=True)
        self.log_frame = frame
        self.log_box = tk.Text(frame, height=12, wrap="word", state="disabled", background="white",
                               relief="flat", highlightthickness=1, highlightbackground=COLOR_BORDER,
                               padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def build_statusbar(self):
        bar = ttk.Frame(self.root, style="Page.TFrame", padding=(16, 4, 16, 14))
        bar.pack(fill="x")
        self.status_bar = bar
        ttk.Label(bar, textvariable=self.status, style="Page.TLabel").pack(side="left")
        ttk.Button(bar, text="保存先を開く", command=self.open_archive_dir).pack(side="right")

    def show_log(self, visible: bool):
        """仕分け中はログ欄を隠す。ログは取得・書き出しの進み具合を出すものなので
        仕分けでは使わず、その高さぶんスクショを大きく表示できる。"""
        if visible:
            if not self.log_frame.winfo_ismapped():
                self.log_frame.pack(fill="both", expand=True, before=self.status_bar)
        else:
            self.log_frame.pack_forget()

    # ------------------------------------------------------ アカウントまわり

    def known_accounts(self):
        root = Path(self.archive_root.get())
        if not root.exists():
            return []
        return sorted(
            d.name for d in root.iterdir() if d.is_dir() and (d / "manifest.json").exists()
        )

    def current_archive(self):
        account = self.account.get().strip()
        if not account:
            return None
        _, handle = normalize_account(account)
        return detail.Archive(Path(self.archive_root.get()), handle)

    def refresh_account_info(self):
        self.account_box.configure(values=self.known_accounts())
        archive = self.current_archive()
        if archive is None:
            self.save_dir_label.set("（アカウントを入力すると決まります）")
            self.history_label.configure(text="（まだありません）")
            return
        self.save_dir_label.set(str(archive.dir))
        lines = archive.history_lines()
        count = len(archive.data["posts"])
        if lines:
            self.history_label.configure(text="\n".join(lines) + f"\n  （保存済み合計 {count}件）")
        else:
            self.history_label.configure(text="（まだありません）")

    # --------------------------------------------------------------- 仕分け

    def load_triage(self, keep_position=False):
        archive = self.current_archive()
        index = self.triage_index if keep_position else 0
        self.triage_archive = archive
        self.triage_posts = archive.sorted_posts() if archive else []
        self.triage_index = min(index, max(0, len(self.triage_posts) - 1))
        self.show_triage()

    def current_triage_post(self):
        if not self.triage_posts:
            return None
        return self.triage_posts[self.triage_index]

    def show_triage(self):
        post = self.current_triage_post()
        self.triage_canvas.delete("all")
        if post is None:
            self.triage_canvas.create_text(
                20, 20, anchor="nw", text="（このアカウントのスクショがまだありません）", fill="#666"
            )
            self.triage_pos.configure(text="0 / 0 枚")
            self.triage_date.configure(text="")
            self.triage_progress.configure(text="")
            self.triage_rank.set("")
            return

        # 幅を合わせて表示する。画面の拡大率の関係で、論理pxで縮めても実際にはほぼ原寸になる。
        # with で開くのは、表示中もPNGを掴んだままにしないため
        # （掴んでいると、そのファイルを別のソフトから移動・削除できなくなる）。
        with Image.open(self.triage_archive.posts_dir / post["filename"]) as image:
            ratio = TRIAGE_IMAGE_WIDTH / image.width
            shown = image.resize(
                (TRIAGE_IMAGE_WIDTH, round(image.height * ratio)), Image.LANCZOS
            )
        self.triage_photo = ImageTk.PhotoImage(shown)   # 参照を持っていないと消える
        self.triage_canvas.create_image(0, 0, anchor="nw", image=self.triage_photo)
        self.triage_canvas.configure(scrollregion=(0, 0, shown.width, shown.height))
        self.triage_canvas.yview_moveto(0)
        self.fit_triage_image()

        self.triage_pos.configure(text=f"{self.triage_index + 1} / {len(self.triage_posts)} 枚")
        posted = post.get("posted_at")
        self.triage_date.configure(
            text=datetime.fromisoformat(posted).strftime("%Y年%m月%d日 %H:%M") if posted else ""
        )
        self.triage_rank.set(str(post.get("rank") or ""))
        self.triage_memo.delete("1.0", "end")
        self.triage_memo.insert("1.0", post.get("memo", "") or "")

        counts = self.triage_archive.rank_counts()
        self.triage_progress.configure(
            text=f"重要 {counts[3]} ／ 普通 {counts[2]} ／ 対象外 {counts[1]} ／ 未採点 {counts[None]}"
        )

    def fit_triage_image(self):
        """ウィンドウが画面（タスクバーを除いた作業領域）に収まるよう、
        表示枠以外が使っている高さを実測して、枠の高さを決める。
        最初に1枚表示した時だけ計算する。"""
        if self.triage_fitted:
            return
        self.triage_fitted = True
        self.root.update_idletasks()
        # ウィンドウの高さは「表示枠」と「右の操作列」の高い方で決まるので、
        # 両方を除いた残り(overhead)を出してから、使える高さを求める。
        # 右の列の方が高いときは、そこまでは枠を大きくしてもウィンドウは高くならない。
        side_height = self.triage_side.winfo_reqheight()
        tallest = max(self.triage_canvas.winfo_reqheight(), side_height)
        overhead = self.root.winfo_reqheight() - tallest
        # reqheight はウィンドウの中身の高さなので、タイトルバーのぶんも引く
        # （実測すると31px程度。ここを忘れるとウィンドウが画面からはみ出す）
        title_bar = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        available = self.work_height - overhead - title_bar - 12
        fitted = max(TRIAGE_VIEW_MIN_HEIGHT,
                     min(TRIAGE_VIEW_MAX_HEIGHT, max(available, side_height)))
        self.triage_canvas.configure(height=fitted)

    def triage_locked(self):
        """取得や書き出しの実行中は仕分けの書き込みを止める。

        実行中の取得は自前のArchive（manifest.jsonの写し）を持って保存を繰り返している。
        同時に仕分け側のArchiveから保存すると、古い写しで上書きしてしまい、
        撮影済みの記録か、入力した採点のどちらかが消える。"""
        if self.busy():
            messagebox.showinfo(
                "仕分け",
                "取得または書き出しの実行中は採点できません。\n終わってからお願いします。",
            )
            return True
        return False

    def save_triage(self):
        """仕分けの結果を書き込む。失敗したら黙らずに知らせる
        （index.csv を Excel で開いたままだと書き込めない）。"""
        try:
            self.triage_archive.save()
            return True
        except Exception as e:
            messagebox.showerror(
                "保存できません",
                f"仕分けの結果を保存できませんでした。\n\n{e}\n\n"
                "index.csv を Excel などで開いている場合は、閉じてからやり直してください。",
            )
            return False

    def commit_memo(self):
        """今表示している1枚のメモを保存する（画面が切り替わる前に必ず通す）"""
        post = self.current_triage_post()
        if post is None or self.busy():
            return
        memo = self.triage_memo.get("1.0", "end").strip()
        if memo != (post.get("memo", "") or ""):
            self.triage_archive.set_judgement(post["id"], memo=memo)
            self.save_triage()

    def set_rank(self, rank: int, advance=True):
        post = self.current_triage_post()
        if post is None or self.triage_locked():
            return
        self.commit_memo()
        self.triage_archive.set_judgement(post["id"], rank=rank)
        if not self.save_triage():
            return
        if advance and self.triage_index < len(self.triage_posts) - 1:
            self.triage_index += 1
        self.show_triage()

    def triage_move(self, step: int):
        if not self.triage_posts:
            return
        self.commit_memo()
        self.triage_index = max(0, min(len(self.triage_posts) - 1, self.triage_index + step))
        self.show_triage()

    def triage_next_unranked(self):
        """未採点の次の1枚へ飛ぶ（途中まで進めて中断したときの続き）"""
        if not self.triage_posts:
            return
        self.commit_memo()
        order = list(range(self.triage_index + 1, len(self.triage_posts))) + list(range(self.triage_index + 1))
        for i in order:
            if not self.triage_posts[i].get("rank"):
                self.triage_index = i
                self.show_triage()
                return
        messagebox.showinfo("仕分け", "未採点のスクショはありません。")

    def triage_keys_active(self):
        """仕分けのキー操作(1/2/3・←→)を受け付けてよい状態かどうか。

        キーはウィンドウ全体に結び付けているので、この確認をしないと
        別のタブの入力欄で日付「2026-09-01」を打っただけで 2 と 1 が採点として
        発火し、表示していない投稿の採点が黙って書き換わってしまう。"""
        if not self.triage_posts:
            return False
        if self.book.tab(self.book.select(), "text") != "仕分け":
            return False
        # 入力欄（メモ・アカウント・日付・数値）にカーソルがある間は文字入力を優先する。
        # ttkのEntry/Spinbox/Comboboxはいずれも tk.Entry を継承している。
        return not isinstance(self.root.focus_get(), (tk.Entry, tk.Text))

    def on_rank_key(self, event):
        if not self.triage_keys_active():
            return
        self.set_rank(int(event.keysym))

    def triage_key_move(self, event, step):
        if not self.triage_keys_active():
            return
        self.triage_move(step)

    def open_current_image(self):
        post = self.current_triage_post()
        if post is not None:
            subprocess.Popen(
                ["explorer", str(self.triage_archive.posts_dir / post["filename"])]
            )

    # ------------------------------------------------------- 設定の保存/復元

    def load_settings(self):
        if SETTINGS_PATH.exists():
            try:
                return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_settings(self):
        data = {
            "account": self.account.get(),
            "archive_root": self.archive_root.get(),
            "width": self.width.get(),
            "period_mode": self.period_mode.get(),
            "get_from": self.get_from.get(),
            "get_to": self.get_to.get(),
            "back_count": self.back_count.get(),
            "back_unit": self.back_unit.get(),
            "full_page": self.full_page.get(),
            "exp_from": self.exp_from.get(),
            "exp_to": self.exp_to.get(),
            "want_images": self.want_images.get(),
            "want_json": self.want_json.get(),
            "want_pdf": self.want_pdf.get(),
            "min_rank": self.min_rank.get(),
            "note_items": {key: var.get() for key, var in self.note_items.items()},
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass  # 設定が保存できなくても動作には支障がないので黙って続ける

    # --------------------------------------------------------------- 操作

    def choose_root(self):
        chosen = filedialog.askdirectory(
            initialdir=self.archive_root.get() or ".", title="アーカイブの置き場所を選ぶ"
        )
        if chosen:
            self.archive_root.set(chosen.replace("/", "\\"))
            self.account_box.configure(values=self.known_accounts())

    def open_archive_dir(self):
        archive = self.current_archive()
        self.open_folder(archive.dir if archive else Path(self.archive_root.get()))

    def open_export_dir(self):
        self.open_folder(Path(self.archive_root.get()) / "書き出し")

    def open_folder(self, path: Path):
        if not path.exists():
            messagebox.showinfo("フォルダ", "まだ作られていません。")
            return
        subprocess.Popen(["explorer", str(path)])

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def begin_work(self, target, args, label, allow_stop=False):
        self.save_settings()
        self.stop_flag.clear()
        self.start_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.stop_button.configure(state="normal" if allow_stop else "disabled")
        self.status.set("実行中…")
        self.append_log(label)
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    # ---- 取得 ----

    def start_capture(self):
        if self.busy():
            return
        account = self.account.get().strip()
        if not account:
            messagebox.showwarning("入力", "アカウントを入力してください。")
            return
        try:
            width = int(self.width.get())
            if width < 200:
                raise ValueError
        except ValueError:
            messagebox.showwarning("入力", "ウィンドウ幅は数字で入力してください。")
            return
        try:
            if self.period_mode.get() == "range":
                # 空のまま押すと、黙って「過去6か月」に化けてしまうので先に弾く
                if not self.get_from.get().strip():
                    messagebox.showwarning("期間", "開始日を入力してください。（例: 2026-08-01）")
                    return
                start, end = detail.resolve_period(self.get_from.get(), self.get_to.get())
            else:
                count = int(self.back_count.get())
                start, end = detail.resolve_period(
                    days=count if self.back_unit.get() == "日" else None,
                    months=count,
                )
        except ValueError as e:
            messagebox.showwarning("期間", str(e))
            return

        account_url, handle = normalize_account(account)
        archive = detail.Archive(Path(self.archive_root.get()), handle)
        full_page = self.full_page.get()
        self.begin_work(
            self.work_capture,
            (account_url, handle, start, end, archive, width, full_page),
            f"=== 取得: {handle} / {start:%Y-%m-%d}〜{end:%Y-%m-%d} / 幅{width}px"
            f" / {'返信も撮る' if full_page else '1画面のみ'} ===",
            allow_stop=True,
        )

    def work_capture(self, account_url, handle, start, end, archive, width, full_page):
        try:
            detail.run(
                account_url, handle, start, end, archive,
                width=width,
                full_page=full_page,
                log=lambda text: self.messages.put(("log", text)),
                should_stop=self.stop_flag.is_set,
                on_login_required=self.wait_for_login,
                on_progress=lambda done, total: self.messages.put(("progress", (done, total))),
            )
        except Exception as e:
            self.messages.put(("log", f"エラーで停止しました: {e}"))
        finally:
            self.messages.put(("done", None))

    # ---- 書き出し ----

    def start_export(self):
        if self.busy():
            return
        archive = self.current_archive()
        if archive is None:
            messagebox.showwarning("入力", "アカウントを入力してください。")
            return
        if not (self.want_images.get() or self.want_json.get() or self.want_pdf.get()):
            messagebox.showwarning("入力", "作るものを1つ以上選んでください。")
            return
        try:
            start = detail.parse_date(self.exp_from.get())
            end = detail.parse_date(self.exp_to.get()) if self.exp_to.get().strip() else date.today()
        except ValueError as e:
            messagebox.showwarning("期間", str(e))
            return
        if start > end:
            messagebox.showwarning("期間", "開始日が終了日より後になっています。")
            return

        min_rank = int(self.min_rank.get()) or None
        rank_label = f" / 重要度 {detail.RANK_LABELS[min_rank]}以上" if min_rank else ""
        # Tkinterの値は作業スレッドから読めないので、ここ(main側)で取り出して渡す
        self.begin_work(
            self.work_export,
            (archive.handle, start, end, Path(self.archive_root.get()),
             self.want_images.get(), self.want_json.get(), self.want_pdf.get(), min_rank,
             {key: var.get() for key, var in self.note_items.items()}),
            f"=== 書き出し: {archive.handle} / {start:%Y-%m-%d}〜{end:%Y-%m-%d}{rank_label} ===",
        )

    def work_export(self, handle, start, end, root, want_images, want_json, want_pdf,
                    min_rank, note_items):
        try:
            export.export(
                handle, start, end, root,
                want_images=want_images,
                want_json=want_json,
                want_pdf=want_pdf,
                min_rank=min_rank,
                note_items=note_items,
                log=lambda text: self.messages.put(("log", text)),
            )
            self.messages.put(("log", "書き出しが終わりました。"))
        except Exception as e:
            self.messages.put(("log", f"書き出しに失敗しました: {e}"))
        finally:
            self.messages.put(("done", None))

    # ---- 共通 ----

    def stop(self):
        self.stop_flag.set()
        self.status.set("中止しています…")
        self.append_log("中止を受け付けました。今開いている投稿の処理が終わり次第止まります。")

    def on_close(self):
        if self.busy():
            if not messagebox.askokcancel("終了", "実行中です。中止して終了しますか。"):
                return
            self.stop_flag.set()
        else:
            self.commit_memo()   # 書きかけのメモを捨てずに保存してから閉じる
        self.save_settings()
        self.root.destroy()

    def wait_for_login(self):
        """未ログインのとき、画面側にログインを促して完了を待つ(作業スレッドから呼ばれる)"""
        self.login_done.clear()
        self.messages.put(("login", None))
        while not self.login_done.wait(timeout=0.2):
            if self.stop_flag.is_set():
                raise RuntimeError("ログイン待ちの間に中止されました")

    # ------------------------------------------- 作業スレッドからの連絡を反映

    def pump(self):
        # ここで例外が抜けると after() が再登録されず、以後ログも進捗も一切
        # 届かなくなる（ボタンが disabled のまま固まる）ので、必ず握りつぶして続ける。
        try:
            self.drain_messages()
        except Exception as e:
            self.append_log(f"画面の更新でエラーが起きました: {e}")
        finally:
            self.root.after(100, self.pump)

    def drain_messages(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "progress":
                    done, total = payload
                    self.status.set(f"{done} / {total} 件" if total else "対象なし")
                elif kind == "login":
                    messagebox.showinfo(
                        "Xへのログイン",
                        "Xにログインしていません。\n"
                        "開いたブラウザ画面でログインし、タイムラインが表示されたら\n"
                        "この「OK」を押してください。",
                    )
                    self.login_done.set()
                elif kind == "done":
                    self.start_button.configure(state="normal")
                    self.export_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status.set("待機中")
                    self.account_box.configure(values=self.known_accounts())
                    self.refresh_account_info()
                    # 取得で増えた分を仕分けに反映する（実行中は読み込みを止めていたため）
                    if self.book.tab(self.book.select(), "text") == "仕分け":
                        self.load_triage(keep_position=True)
        except queue.Empty:
            pass

    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

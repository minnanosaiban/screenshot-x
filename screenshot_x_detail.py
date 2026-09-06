#!/usr/bin/env python3
"""
X (Twitter) の公開アカウントの投稿を1件ずつ「個別ページ」として開き、
ブラウザウィンドウ全体(アドレスバーに投稿URLが写った状態)をスクリーンショットして保存する。

画面から操作したい場合は screenshot_x_app.py (同じ処理のGUI版) を使う。

screenshot_x.py(タイムライン一覧のカードを撮る版)との違い:
    - 投稿を実際に1件ずつ開く(URLがアドレスバーに残る)
    - ページ内容だけでなく、ブラウザウィンドウそのものをキャプチャする
      (Playwrightのpage.screenshot()はアドレスバーまでは撮れないため)

使い方:
    python screenshot_x_detail.py <アカウントURL または @handle> [期間の指定] [--root フォルダ] [--width 700]

    期間の指定(いずれか1つ。省略時は過去6か月)
        --from 2026-08-01 --to 2026-08-31   日付の範囲で指定する
        --from 2026-08-01                   その日から今日まで
        --days 7 / --months 3               今日から遡る日数・月数で指定する

例:
    python screenshot_x_detail.py exampleuser --from 2026-08-01 --to 2026-08-31
    python screenshot_x_detail.py exampleuser --days 7

ブラウザウィンドウは幅700px・画面の高さいっぱいに固定して撮る。
Xは幅が狭いと右サイドバー(「関連性の高いアカウント」等)を出さないので、
スクショには「アドレスバー + 投稿の列」だけが写る。幅は --width で変えられる。

【保存先の構造】アカウントごとに1つのフォルダを作り、そこへ貯め続ける。
    <root>\<アカウント名>\
        posts\20260801_0912_2077xxxxxxxxxxxxxxxxx.png   ← 投稿日_時刻_投稿ID
        index.csv        投稿日時・投稿URL・ファイル名・取得日時・SHA-256 の一覧
        manifest.json    取得済みの記録と、いつどの期間を取得したかの履歴

ファイル名に通し番号を使わないのは、あとから別の期間(古い分)を足したときに
番号と日付の順序が食い違うのを避けるため。投稿日から始まるので名前順＝日付順になり、
投稿IDからURL(https://x.com/<アカウント>/status/<ID>)を復元できる。
提出用の「001…」の連番は、書き出し(screenshot_x_export.py)の時に振る。

ログインセッションは screenshot_x.py と同じ専用プロファイル(chrome_profile)を共有する。
未ログインなら、開いたブラウザで手動ログインが必要(screenshot_x.pyと同じ流れ)。

【重要な制約】
- headless(画面非表示)実行はX側のbot対策でブロックされるため、画面表示ありで実行する必要がある。
- キャプチャはブラウザのウィンドウ本人に描かせる方式(PrintWindow)なので、実行中に他のウィンドウを
  使っても写り込まない。ただしウィンドウを最小化すると撮れないので、最小化はしないこと。
- リポスト(コメント無し単純RT)は対象アカウント自身の投稿として検出できず対象から漏れる。
- 投稿数が多いアカウントだと1件ずつ開く分、全体では時間がかかる(体感1件あたり4〜5秒程度)。
"""
import argparse
import csv
import ctypes
import hashlib
import json
import os
import random
import re
from ctypes import wintypes
from datetime import date, datetime, time as time_of_day, timedelta
from pathlib import Path

from PIL import Image, ImageGrab

from screenshot_x import (
    PROFILE_DIR,
    STOP_AFTER_OLD,
    STOP_AFTER_STALL,
    get_tweet_info,
    normalize_account,
    safe_goto,
)
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

MAX_CONSECUTIVE_LOAD_FAILURES = 3  # これだけ連続で読み込み失敗したら、粘らずに終了する

# ブラウザウィンドウの横幅(拡大率を掛ける前のpx)。
# Xは幅が狭いと右サイドバー(「関連性の高いアカウント」等)を出さず、左メニューもアイコンだけに畳むので、
# この幅にすると「アドレスバー + 投稿の列」だけが写る。
DEFAULT_WINDOW_WIDTH = 700

# アーカイブの置き場所。この下にアカウント名のフォルダを作って貯めていく。
DEFAULT_ARCHIVE_ROOT = Path.home() / "Screenshots" / "x_archive"

# 仕分けの段階。数字が大きいほど重要。未設定は None。
RANK_LABELS = {3: "重要", 2: "普通", 1: "対象外"}

# ブラウザ起動時のオプション。
# --test-type は「サポートされていないコマンドライン フラグ」の黄色い警告バーを出さないための指定。
# 後半3つは、他のウィンドウに隠れたときChromeが描画を止めてしまい、キャプチャが古い内容や
# 真っ白になるのを防ぐための指定。
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--test-type",
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

# --------------------------------------------------------------------------
# Windowsのウィンドウを直接キャプチャするための準備 (PrintWindow方式)
#
# 画面をそのまま撮る方式(ImageGrab)だと、他のウィンドウが重なった瞬間に
# そちらが写り込んでしまう。PrintWindowはウィンドウ本人に描画させる仕組みなので、
# 隠れていても・裏に回っていてもブラウザの中身だけを撮れる。
# --------------------------------------------------------------------------
PW_RENDERFULLCONTENT = 0x00000002
CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# 64bit環境ではハンドル(ポインタ)が既定のint扱いだと壊れるため、型を明示する
_user32.GetWindowDC.restype = wintypes.HDC
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Xの投稿を1件ずつ開いて、アドレスバー込みでウィンドウ全体をスクショ保存する"
    )
    p.add_argument("account", help="アカウントURL (https://x.com/xxx) または @handle / handle")
    p.add_argument("--from", dest="date_from", default=None, help="取得する期間の開始日 (2026-08-01)")
    p.add_argument("--to", dest="date_to", default=None, help="取得する期間の終了日 (2026-08-31)。省略時は今日")
    p.add_argument("--months", type=int, default=6, help="遡る期間(月数)。--fromが無い場合に使う。デフォルト6")
    p.add_argument("--days", type=int, default=None, help="遡る期間(日数)。--monthsより優先")
    p.add_argument(
        "--root",
        default=str(DEFAULT_ARCHIVE_ROOT),
        help="アーカイブの置き場所。この下にアカウント名のフォルダを作る",
    )
    p.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WINDOW_WIDTH,
        help=f"ブラウザウィンドウの横幅。デフォルト{DEFAULT_WINDOW_WIDTH}(投稿の列だけが写る幅)",
    )
    p.add_argument(
        "--no-replies",
        action="store_true",
        help="返信を撮らず、1画面に収まる範囲だけにする（既定は返信も撮る）",
    )
    return p.parse_args()


def parse_exact_datetime(aria_label: str):
    """個別ページの日時リンクの aria-label (例: '午後11:56 · 2026年9月2日') を厳密にパースする"""
    m = re.search(
        r"(午前|午後)\s*(\d{1,2}):(\d{2})\s*[·・]\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        aria_label or "",
    )
    if not m:
        return None
    ampm, hh, mm, y, mo, d = m.groups()
    hh = int(hh)
    if ampm == "午後" and hh != 12:
        hh += 12
    if ampm == "午前" and hh == 12:
        hh = 0
    try:
        return datetime(int(y), int(mo), int(d), hh, int(mm))
    except ValueError:
        return None


def get_exact_posted_at(page, handle_lower: str, tweet_id: str):
    """個別ページ内で、この投稿自身への日時リンクを探して正確な日時を取り出す"""
    target_href = f"/{handle_lower}/status/{tweet_id}"
    for link in page.query_selector_all('a[href*="/status/"]'):
        href = (link.get_attribute("href") or "").lower()
        if href == target_href:
            dt = parse_exact_datetime(link.get_attribute("aria-label"))
            if dt:
                return dt
    return None


def wait_for_articles(page, timeout_ms=20000, log=print):
    """投稿(article要素)が実際に表示されるまで待つ。
    Xはページ遷移直後、大きな『X』ロゴのスプラッシュだけで投稿がまだ無い状態になることがあり、
    固定時間の待機だと(混雑時など)投稿が出る前に処理してしまい0件判定になる。
    timeout_ms待っても出なければ1回だけ読み込み直して再試行する。
    それでも出なければFalseを返す(呼び出し側は0件として扱われる)。"""
    try:
        page.wait_for_selector("article", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        log("  読み込みに時間がかかっています。再読み込みします…")
        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("article", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            log("  投稿を読み込めませんでした(表示できないアカウント、またはX側の一時的な問題の可能性)。")
            return False


def wait_until_settled(page, timeout_ms=8000):
    """読み込み中の「ぐるぐる」が消えるまで待つ。
    article が出た時点で撮ると、返信欄などがまだ読み込み中のまま写ってしまうため。
    時間内に消えない場合(返信が多い投稿など)は諦めてそのまま撮る。"""
    try:
        page.wait_for_function(
            "() => !document.querySelector('[role=\"progressbar\"]')",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(400)  # 画像などの描き込みが終わるまでの余裕


def get_screen_metrics():
    """画面キャプチャに必要なWindows側の寸法を取得する。

    Chromeが返すウィンドウ座標は「画面の拡大率(125%等)を掛ける前」の値なのに対し、
    ImageGrabは実ピクセルで切り抜くため、そのまま渡すと拡大率の分だけ範囲が足りず
    ウィンドウの右下が欠ける。その換算に使う scale をここで求める。

    border は、Windowsのウィンドウが持つ「見えない縁」(マウスでリサイズするための
    透明な余白)の幅。ウィンドウ座標にはこれが含まれているので、切り抜く時に差し引かないと
    左右と下に背景が写り込む。"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    try:
        screen_w = gdi32.GetDeviceCaps(hdc, 118)   # DESKTOPHORZRES: 実ピクセルの画面幅
        screen_h = gdi32.GetDeviceCaps(hdc, 117)   # DESKTOPVERTRES: 実ピクセルの画面高さ
        logical_w = gdi32.GetDeviceCaps(hdc, 8)    # HORZRES: 拡大率適用後(Chromeと同じ単位)
    finally:
        user32.ReleaseDC(0, hdc)

    work = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
    border = user32.GetSystemMetrics(32) + user32.GetSystemMetrics(92)  # SM_CXSIZEFRAME + SM_CXPADDEDBORDER

    return {
        "scale": (screen_w / logical_w) if logical_w else 1.0,
        "screen_w": screen_w,
        "screen_h": screen_h,
        "work_top": work.top,
        "work_height": work.bottom - work.top,  # タスクバーを除いた高さ
        "border": border,
    }


def find_window_handle(bounds):
    """CDPが返したウィンドウ位置とぴったり一致するChromeのウィンドウを探す。
    見つからなければNone(その場合は画面キャプチャ方式にフォールバックする)。"""
    target = (
        bounds["left"],
        bounds["top"],
        bounds["left"] + bounds["width"],
        bounds["top"] + bounds["height"],
    )
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, name, 64)
        if name.value != CHROME_WINDOW_CLASS:
            return True
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if (rect.left, rect.top, rect.right, rect.bottom) == target:
            found.append(hwnd)
            return False  # 見つかったので列挙を打ち切る
        return True

    _user32.EnumWindows(visit, 0)
    return found[0] if found else None


def setup_window(context, page, width: int, metrics):
    """ウィンドウを指定の幅・画面の高さいっぱいに固定して左上に置き、
    そのウィンドウのハンドル(キャプチャ対象)を一緒に返す。

    最大化しないのは、幅が広いとXが右サイドバー(「関連性の高いアカウント」等)を表示してしまい、
    スクショに投稿と関係のない領域が入るため。"""
    cdp = context.new_cdp_session(page)
    window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
    # 最大化されたままだと座標を指定できないので、まず通常状態に戻す
    cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
    cdp.send(
        "Browser.setWindowBounds",
        {
            "windowId": window_id,
            "bounds": {
                "left": 0,
                "top": metrics["work_top"],
                # 見えない縁の分だけ広げて、実際に見えている幅が width になるようにする
                "width": width + metrics["border"] * 2,
                "height": metrics["work_height"],
            },
        },
    )
    page.wait_for_timeout(500)
    bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
    return cdp, find_window_handle(bounds)


def grab_window_image(hwnd, width: int, height: int):
    """PrintWindowでウィンドウの中身を描かせて画像として取り出す。
    width/heightは実ピクセル(画面の拡大率を掛けたあとの大きさ)。"""
    hdc_src = _user32.GetWindowDC(hwnd)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_src)
    bitmap = _gdi32.CreateCompatibleBitmap(hdc_src, width, height)
    previous = _gdi32.SelectObject(hdc_mem, bitmap)
    try:
        if not _user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # マイナス = 上から下へ並べる
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(hdc_mem, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            return None
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    finally:
        _gdi32.SelectObject(hdc_mem, previous)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(hwnd, hdc_src)


def grab_window_cropped(cdp, hwnd, metrics):
    """ウィンドウを撮って、見えない縁を落とした画像を返す（撮れなければNone）"""
    if not hwnd:
        return None
    bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
    scale = metrics["scale"]
    image = grab_window_image(hwnd, round(bounds["width"] * scale), round(bounds["height"] * scale))
    if image is None:
        return None
    inset = round(metrics["border"] * scale)
    width, height = image.size
    return image.crop((inset, 0, width - inset, height - inset))


def find_discover_offset(page):
    """Xが返信の下に出す「もっと見つける」（無関係な他人の投稿の推薦）が始まる位置を、
    ページ先頭からの距離(CSS px)で返す。見つからなければ None。

    ここから下は対象アカウントの投稿でも返信でもないので、証拠としては不要。"""
    return page.evaluate(
        """() => {
            const labels = ['もっと見つける', 'Discover more'];
            for (const el of document.querySelectorAll('h2, div[role="heading"], span')) {
                if (!labels.includes((el.textContent || '').trim())) continue;
                // 見出しと同じ位置から始まる一番外側の要素まで遡って、区切り線ごと落とす
                let box = el;
                while (box.parentElement &&
                       box.parentElement.getBoundingClientRect().top === box.getBoundingClientRect().top) {
                    box = box.parentElement;
                }
                return Math.round(box.getBoundingClientRect().top + window.scrollY);
            }
            return null;
        }"""
    )


def blank_row_above(image, row: int, left: int = 0, right: int = None, limit: int = 80):
    """row の少し上で、文字も罫線も無い行を探す。
    見出しの位置で切ると文字の上端が残るため、その手前の余白で切るのに使う。

    left/right で見る範囲を本文の列に絞れる。画面全体で見ると、左メニューの
    ボタンが同じ高さにあるだけで「無地の行が無い」と判定されてしまうため。"""
    right = right or image.width
    lowest = max(1, row - limit)
    for y in range(row, lowest, -1):
        colors = image.crop((left, y - 1, right, y)).getcolors(right - left)
        if colors and len(colors) == 1:
            return y
    return row


def trim_bottom_blank(image, keep=24):
    """下端に続く無地の余白を削る。Xはページの末尾に大きな空白を持つことがあり、
    そのまま残すと縦に間延びした画像になるため。"""
    width, height = image.size
    sample = image.crop((0, height - 1, width, height)).getcolors(width)
    if not sample or len(sample) > 1:
        return image
    blank_color = sample[0][1]
    row = height
    while row > 1:
        colors = image.crop((0, row - 1, width, row)).getcolors(width)
        if not colors or len(colors) > 1 or colors[0][1] != blank_color:
            break
        row -= 1
    return image.crop((0, 0, width, min(height, row + keep)))


def capture_window_full(cdp, hwnd, page, filepath: Path, metrics, max_screens=12, log=print):
    """返信も含めてページ全体を撮る。

    スクロールしながら1画面ずつ撮り、新しく現れた分だけを縦に継ぎ足す。
    Chromeにページ全体を一度に描かせる方法(captureBeyondViewport)も試したが、
    Xは画面外の要素をDOMから外すため真っ白な画像になり使えなかった。

    左のメニューは画面に固定されているので、そのまま継ぎ足すと同じものが何度も写る。
    右端のスクロールバーも同様に、継ぎ足すたびに断片が並んでしまう。
    どちらも2枚目以降は白で塗って消す。"""
    # 返信への投稿では、Xが元の投稿を上に表示して自動で下へスクロールした状態で開く。
    # そのまま撮ると上の部分が抜け落ち、「もっと見つける」の位置計算もずれるので、
    # 必ず先頭へ戻してから撮り始める。
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)

    first = grab_window_cropped(cdp, hwnd, metrics)
    if first is None:
        return False

    dpr = page.evaluate("window.devicePixelRatio") or metrics["scale"]
    view_css = page.evaluate("window.innerHeight")
    width, window_height = first.size
    # 本文の列が始まる位置＝左メニューの右端。ここより左は2枚目以降で塗りつぶす。
    nav_right = page.evaluate(
        "() => { const a = document.querySelector('article');"
        " return a ? Math.round(a.getBoundingClientRect().left) : 0; }"
    )
    nav_right_px = round(nav_right * dpr)
    # スクロールバーの幅。innerWidthは含み、clientWidthは含まないので、その差で求まる。
    scrollbar_px = round(
        max(0, page.evaluate("window.innerWidth - document.documentElement.clientWidth")) * dpr
    )

    pieces = [first]
    previous_y = page.evaluate("window.scrollY")
    for _ in range(max_screens - 1):
        page.evaluate(f"window.scrollBy(0, {int(view_css * 0.9)})")
        page.wait_for_timeout(700)      # 返信の読み込み待ち
        current_y = page.evaluate("window.scrollY")
        grown = round((current_y - previous_y) * dpr)
        if grown <= 2:
            break
        shot = grab_window_cropped(cdp, hwnd, metrics)
        if shot is None or shot.size != first.size:
            break
        if grown > window_height:
            # 返信が上に差し込まれるとブラウザが位置を補正し、1画面ぶんを超えて
            # 進むことがある。そのまま切ると範囲外を黒で埋めた帯が入るので、
            # 撮れている分だけにとどめる（その差分は写らないが、黒帯よりは害が小さい）。
            log(f"  ※ スクロールが1画面を超えました（{grown}px）。その間の一部が写りません。")
            grown = window_height
        strip = shot.crop((0, window_height - grown, width, window_height))
        if nav_right_px > 0:
            strip.paste((255, 255, 255), (0, 0, nav_right_px, strip.height))
        if scrollbar_px > 0:
            strip.paste((255, 255, 255), (width - scrollbar_px, 0, width, strip.height))
        pieces.append(strip)
        previous_y = current_y
        if page.evaluate(
            "Math.ceil(window.scrollY + window.innerHeight) >= document.body.scrollHeight - 2"
        ):
            break
    else:
        log(f"  ※ {max_screens}画面ぶんで打ち切りました（返信が非常に多い投稿です）")

    total = sum(piece.height for piece in pieces)
    canvas = Image.new("RGB", (width, total), "white")
    y = 0
    for piece in pieces:
        canvas.paste(piece, (0, y))
        y += piece.height

    # 「もっと見つける」以下は無関係な推薦投稿なので切り落とす。
    # 継ぎ足した画像は「ブラウザ枠 + ページ先頭からの内容」が縦に並んでいるので、
    # ページ内の位置に枠の高さを足せば画像上の位置になる。
    chrome_height = window_height - round(view_css * dpr)
    discover = find_discover_offset(page)
    if discover is not None:
        # 継ぎ足しの丸め誤差で数px下にずれ、見出しの文字が残ることがあるので、
        # 少し上の余白（無地の行）を探してそこで切る
        cut = blank_row_above(
            canvas,
            chrome_height + round(discover * dpr),
            left=nav_right_px,
            right=width - scrollbar_px,
        )
        # 投稿本体まで消してしまわないよう、極端に短くなる場合は切らない
        if 400 <= cut < canvas.height:
            canvas = canvas.crop((0, 0, width, cut))
            log("  （「もっと見つける」以下は無関係な推薦投稿のため除きました）")

    trim_bottom_blank(canvas).save(str(filepath))
    return True


def capture_window(cdp, hwnd, filepath: Path, metrics):
    """ブラウザウィンドウをアドレスバーごと画像に保存する
    (Playwrightのscreenshot()はページ内しか撮れないため、この方式を使う)。

    通常はPrintWindowでウィンドウ本人に描かせるので、他のウィンドウが重なっていても
    そちらは写り込まない。ハンドルが取れなかった場合だけ、従来どおり画面から切り抜く。"""
    bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
    scale = metrics["scale"]
    border = metrics["border"]

    if hwnd:
        image = grab_window_image(hwnd, round(bounds["width"] * scale), round(bounds["height"] * scale))
        if image is not None:
            # 見えない縁(左・右・下に付く。上には付かない)を落とす
            inset = round(border * scale)
            width, height = image.size
            image.crop((inset, 0, width - inset, height - inset)).save(str(filepath))
            return

    # フォールバック: 画面そのものから切り抜く(この間は他のウィンドウを前に出さないこと)
    left = (bounds["left"] + border) * scale
    top = bounds["top"] * scale
    right = (bounds["left"] + bounds["width"] - border) * scale
    bottom = (bounds["top"] + bounds["height"] - border) * scale
    box = (
        max(0, round(left)),
        max(0, round(top)),
        min(metrics["screen_w"], round(right)),
        min(metrics["screen_h"], round(bottom)),
    )
    ImageGrab.grab(bbox=box).save(str(filepath))


class Archive:
    """アカウント1つ分の保存先。<root>\\<アカウント名>\\ の中身を受け持つ。

    manifest.json が「何を取得済みか」の唯一の台帳で、index.csv はそこから毎回作り直す
    (人が見るための一覧なので、手で直す前提にしない)。"""

    def __init__(self, root: Path, handle: str):
        self.handle = handle
        self.dir = Path(root) / handle
        self.posts_dir = self.dir / "posts"
        self.manifest_path = self.dir / "manifest.json"
        self.index_path = self.dir / "index.csv"
        self.data = self._load()

    def _load(self):
        if not self.manifest_path.exists():
            return {"account": self.handle, "posts": [], "runs": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            # ここで空のアーカイブを返してしまうと、次の保存でそれを書き戻し、
            # 採点・メモ・SHA-256の記録が完全に消える。読めないなら止める。
            raise RuntimeError(
                f"{self.manifest_path} を読めません（{e}）。\n"
                "壊れている可能性があります。手を加える前にファイルを退避してください。"
            ) from e
        data.setdefault("posts", [])
        data.setdefault("runs", [])
        return data

    def prepare(self):
        self.posts_dir.mkdir(parents=True, exist_ok=True)

    @property
    def known_ids(self):
        return {p["id"] for p in self.data["posts"]}

    def post_url(self, tweet_id: str):
        return f"https://x.com/{self.handle}/status/{tweet_id}"

    def filename_for(self, tweet_id: str, posted_at):
        stamp = posted_at.strftime("%Y%m%d_%H%M") if posted_at else "unknown"
        return f"{stamp}_{tweet_id}.png"

    def add(self, tweet_id: str, posted_at, filename: str, captured_at: datetime):
        digest = hashlib.sha256((self.posts_dir / filename).read_bytes()).hexdigest()
        self.data["posts"].append(
            {
                "id": tweet_id,
                "posted_at": posted_at.isoformat() if posted_at else None,
                "filename": filename,
                "captured_at": captured_at.isoformat(timespec="seconds"),
                "sha256": digest,
                # 仕分け(人が目で見て判断する)で後から入る。撮った時点では未設定。
                "rank": None,
                "memo": "",
            }
        )

    def set_judgement(self, tweet_id: str, rank=None, memo=None):
        """仕分けの結果を書き込む。rank / memo は渡した方だけ更新する。"""
        for post in self.data["posts"]:
            if post["id"] == tweet_id:
                if rank is not None:
                    post["rank"] = rank or None   # 0 や "" は「未設定」に戻す指示として扱う
                if memo is not None:
                    post["memo"] = memo
                return True
        return False

    def rank_counts(self):
        """仕分けの進み具合。キーは 3/2/1 と、未設定を表す None。"""
        counts = {3: 0, 2: 0, 1: 0, None: 0}
        for post in self.data["posts"]:
            counts[post.get("rank") or None] += 1
        return counts

    def record_run(self, start, end, saved: int, started_at: datetime):
        """いつ・どの期間を取得したかの履歴。期間の抜けに気づけるように残す。"""
        self.data["runs"].append(
            {
                "started_at": started_at.isoformat(timespec="seconds"),
                "from": f"{start:%Y-%m-%d}",
                "to": f"{end:%Y-%m-%d}",
                "saved": saved,
            }
        )

    def sorted_posts(self):
        """投稿日時順。日時が取れなかったものは末尾にまとめる。"""
        return sorted(
            self.data["posts"], key=lambda p: (p["posted_at"] is None, p["posted_at"] or "")
        )

    def save(self):
        """台帳を書き出す。1件撮るごとに呼ばれるので、150件なら150回上書きすることになる。
        直接上書きすると、その途中で電源が落ちたりOneDriveの同期とぶつかったりした時に
        台帳が壊れる。別名で書いてから置き換えることで、壊れかけの状態を残さない。"""
        self.data["posts"] = self.sorted_posts()
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.manifest_path)
        self._write_index()

    def _write_index(self):
        """画像だけを見ても元の投稿に戻れるように、一覧表をCSVで書き出す。
        Excelでそのまま開けるよう BOM 付きUTF-8。"""
        with self.index_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["投稿日時", "重要度", "メモ", "投稿URL", "ファイル名", "取得日時", "SHA-256"]
            )
            for p in self.data["posts"]:
                writer.writerow(
                    [
                        (p["posted_at"] or "").replace("T", " "),
                        RANK_LABELS.get(p.get("rank"), ""),
                        p.get("memo", ""),
                        self.post_url(p["id"]),
                        p["filename"],
                        (p.get("captured_at") or "").replace("T", " "),
                        p.get("sha256", ""),
                    ]
                )

    def history_lines(self):
        return [
            f"  {r['from']} 〜 {r['to']}   （{r['started_at'][:10]} 取得・{r['saved']}件）"
            for r in self.data["runs"]
        ]


def cli_login_prompt():
    """コマンドラインから実行したときのログイン待ち"""
    print("=" * 60)
    print("Xにログインしていません。開いたブラウザ画面で手動でログインしてください。")
    print("ログインしてタイムライン(ホーム)が表示されたら、このターミナルに戻って")
    print("Enterキーを押してください。")
    print("=" * 60)
    input("ログイン完了後、Enterキーを押してください... ")


def ensure_logged_in(page, on_login_required):
    """ログイン状態を確認し、未ログインなら on_login_required() で完了を待つ"""
    safe_goto(page, "https://x.com/home")
    page.wait_for_timeout(2000)
    if not page.url.rstrip("/").endswith("/home"):
        on_login_required()


def discover_ids(page, handle_lower: str, start: datetime, end: datetime, log=print, should_stop=None):
    """タイムラインをスクロールし、期間内の投稿IDを古い順のリストで返す。

    Xは新しい投稿から順にしか辿れないので、endより新しい投稿は「飛ばしながら通過」し、
    startより古い投稿が続いたところで打ち切る。ここで使う日時はタイムライン上の表示から
    読んだ概算なので、境界の投稿は個別ページを開いた時に正確な日時で確認し直す。"""
    should_stop = should_stop or (lambda: False)
    now = datetime.now()

    discovered = []  # 発見順(新しい→古い)
    seen_this_scan = set()
    consecutive_old = 0
    no_new_scrolls = 0
    last_height = 0

    while True:
        if should_stop():
            log("中止しました(投稿一覧の収集中)。")
            break

        articles = page.query_selector_all("article")
        new_this_round = 0

        for article in articles:
            try:
                info = get_tweet_info(article, handle_lower, now)
            except Exception:
                continue
            if not info or info["id"] in seen_this_scan:
                continue
            seen_this_scan.add(info["id"])
            new_this_round += 1

            if info["pinned"] or info["posted_at"] is None:
                # 固定ポストと日時が読めなかったものは、期間の判定を個別ページ側に任せる
                consecutive_old = 0
                discovered.append(info["id"])
            elif info["posted_at"] < start:
                consecutive_old += 1
                if consecutive_old >= STOP_AFTER_OLD:
                    break
                continue
            elif info["posted_at"] > end:
                # 期間より新しい投稿。まだ古い方に目的の投稿があるので、数えずに通過する
                consecutive_old = 0
                continue
            else:
                consecutive_old = 0
                discovered.append(info["id"])

        if consecutive_old >= STOP_AFTER_OLD:
            log(f"{start:%Y-%m-%d} より古いポストが続いたため収集を終了します。")
            break

        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1800)

        new_height = page.evaluate("document.body.scrollHeight")
        if new_this_round == 0 and new_height == last_height:
            no_new_scrolls += 1
            if no_new_scrolls >= STOP_AFTER_STALL:
                log("これ以上新しいポストが読み込まれないため収集を終了します。")
                break
        else:
            no_new_scrolls = 0
        last_height = new_height

    return list(reversed(discovered))  # 古い順にして返す


def run(
    account_url: str,
    handle: str,
    start: datetime,
    end: datetime,
    archive: "Archive",
    width: int = DEFAULT_WINDOW_WIDTH,
    full_page: bool = True,
    log=print,
    should_stop=None,
    on_login_required=cli_login_prompt,
    on_progress=None,
):
    """期間内の投稿を1件ずつ開いて保存する本体。

    log / should_stop / on_login_required / on_progress を差し替えることで、
    コマンドラインからでも画面(screenshot_x_app.py)からでも同じ処理を使える。"""
    should_stop = should_stop or (lambda: False)
    on_progress = on_progress or (lambda done, total: None)

    archive.prepare()
    done_ids = archive.known_ids
    handle_lower = handle.lower()
    started_at = datetime.now()
    saved = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            channel="chrome",
            # no_viewport=True でないとページは実ウィンドウの幅ではなく既定の1280pxで組まれてしまい、
            # ウィンドウを狭くしても右サイドバーが消えない(viewport=Noneは「指定なし」の意味で効かない)
            no_viewport=True,
            locale="ja-JP",
            # chromium_sandbox=True にしないとPlaywrightが --no-sandbox を付け、
            # 「サポートされていないコマンドライン フラグ」の黄色い警告バーがスクショに写り込む
            chromium_sandbox=True,
            args=BROWSER_ARGS,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in(page, on_login_required)
            metrics = get_screen_metrics()
            cdp, hwnd = setup_window(context, page, width, metrics)
            # 起動直後はアドレスバーに入力フォーカスが残り、URLが青く選択された状態で写ってしまう。
            # ページ側にフォーカスを移すと以降は選択されない(最初の1回だけでよい)。
            page.bring_to_front()
            if hwnd is None:
                log(
                    "※ ブラウザウィンドウを直接キャプチャできないため、画面から切り抜きます。"
                    "実行中はブラウザを最前面のままにしてください。"
                )

            log(f"投稿一覧を収集します: {account_url}")
            log(f"対象期間: {start:%Y-%m-%d} 〜 {end:%Y-%m-%d}")
            safe_goto(page, account_url)
            wait_for_articles(page, log=log)
            chronological_ids = discover_ids(
                page, handle_lower, start, end, log=log, should_stop=should_stop
            )
            todo = [tid for tid in chronological_ids if tid not in done_ids]
            log(
                f"期間内 {len(chronological_ids)} 件中、取得済み {len(done_ids & set(chronological_ids))} 件、"
                f"未取得 {len(todo)} 件を個別に開いて保存します。"
            )
            on_progress(0, len(todo))

            consecutive_load_failures = 0
            for index, tid in enumerate(todo):
                if should_stop():
                    log("中止しました。ここまでの保存分は残ります(次回は続きから)。")
                    break

                url = f"https://x.com/{handle}/status/{tid}"
                try:
                    safe_goto(page, url)
                    if not wait_for_articles(page, log=log):
                        consecutive_load_failures += 1
                        log(f"  スキップ (status/{tid}): 投稿を読み込めなかったため次回実行時に再試行されます")
                        if consecutive_load_failures >= MAX_CONSECUTIVE_LOAD_FAILURES:
                            log(
                                f"  読み込み失敗が{MAX_CONSECUTIVE_LOAD_FAILURES}件連続したため、"
                                "X側で一時的にブロックされている可能性があります。"
                                "ここで終了します。しばらく時間を置いてから再実行してください。"
                            )
                            break
                        page.wait_for_timeout(random.randint(800, 1500))
                        continue

                    consecutive_load_failures = 0
                    wait_until_settled(page)
                    posted_at = get_exact_posted_at(page, handle_lower, tid)

                    # タイムライン上の概算日時では拾ってしまう境界の投稿や固定ポストを、
                    # 個別ページで読んだ正確な日時でふるい落とす。
                    if posted_at is not None and not (start <= posted_at <= end):
                        log(f"  対象外 ({posted_at:%Y-%m-%d %H:%M}): 期間の外なので保存しません")
                        on_progress(index + 1, len(todo))
                        page.wait_for_timeout(random.randint(800, 1500))
                        continue

                    filename = archive.filename_for(tid, posted_at)
                    target = archive.posts_dir / filename
                    if not (full_page and capture_window_full(cdp, hwnd, page, target, metrics, log=log)):
                        capture_window(cdp, hwnd, target, metrics)
                    archive.add(tid, posted_at, filename, datetime.now())
                    archive.save()
                    saved += 1
                    log(f"[{saved}] 保存: {filename}")
                except Exception as e:
                    log(f"  失敗 (status/{tid}): {e} → 次回実行時に再試行されます")
                on_progress(index + 1, len(todo))
                page.wait_for_timeout(random.randint(800, 1500))
        finally:
            context.close()

    archive.record_run(start, end, saved, started_at)
    archive.save()
    log(f"完了。今回 {saved} 件を保存しました。保存先: {archive.dir}")
    return saved


def parse_date(text: str) -> date:
    """2026-08-01 / 2026/08/01 / 20260801 のいずれでも受け付ける"""
    cleaned = (text or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"日付として読めません: {text}（例: 2026-08-01）")


def resolve_period(date_from=None, date_to=None, days=None, months=6):
    """指定のしかたが3通りあるので、ここで (開始日時, 終了日時) に揃える。
    開始はその日の0時、終了はその日の23:59:59（終了日当日の投稿も含める）。"""
    today = date.today()
    if date_from:
        start_date = date_from if isinstance(date_from, date) else parse_date(date_from)
        end_date = (date_to if isinstance(date_to, date) else parse_date(date_to)) if date_to else today
    else:
        span = days if days is not None else months * 30
        end_date = today
        start_date = today - timedelta(days=span)
    if start_date > end_date:
        raise ValueError("開始日が終了日より後になっています。")
    return (
        datetime.combine(start_date, time_of_day.min),
        datetime.combine(end_date, time_of_day.max),
    )


def main():
    args = parse_args()
    account_url, handle = normalize_account(args.account)
    start, end = resolve_period(args.date_from, args.date_to, args.days, args.months)
    archive = Archive(Path(args.root), handle)
    run(account_url, handle, start, end, archive, args.width, full_page=not args.no_replies)


if __name__ == "__main__":
    main()

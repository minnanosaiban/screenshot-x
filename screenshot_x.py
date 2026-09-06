#!/usr/bin/env python3
"""
X (Twitter) の公開アカウントのポストを1件ずつスクリーンショットして保存するスクリプト。

使い方:
    python screenshot_x.py <アカウントURL または @handle> [--months 6 | --days 7] [--out "保存先フォルダ"]

例:
    python screenshot_x.py exampleuser
    python screenshot_x.py https://x.com/exampleuser --months 6
    python screenshot_x.py exampleuser --days 7 --out "保存先フォルダ"

初回実行時はブラウザ画面が開くので、そこで手動でXにログインしてください。
ログイン状態はこのスクリプト専用のプロファイル(chrome_profileフォルダ)に保存されるので、
次回以降は自動でログイン状態になります(実際に使っているChromeのプロファイルとは別物です)。

同じ保存先フォルダに対して繰り返し実行すると、前回までに保存済みのポストは
seen_ids.json で記憶していて重複保存しないので、差分だけ追記する使い方もできます。

【重要な制約】
- X側のbot対策(Cloudflare)により、ブラウザを画面非表示(headless)で動かすと
  ブロックされる(2026-09時点で確認済み)。そのため画面を表示したまま実行する必要がある。
- Xのフロントエンドは data-testid 属性を使わない構成に変わっており、投稿日時も
  ISO日時ではなく「40m」「2h」「1月11日」のような表示テキストしか取得できない。
  このスクリプトはそれを簡易パースして6か月フィルタに使っている(誤差が出うる)。
- リポスト(コメント無し引用ではない単純RT)は、投稿リンクの飛び先が元投稿者のものに
  なるため対象アカウント自身の投稿として検出できず、スクショ対象から漏れる可能性がある。
"""
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / "chrome_profile"  # ログインセッション保存用の専用プロファイル

STOP_AFTER_OLD = 8     # 期間外(古い)ポストがこれだけ連続したら終了
STOP_AFTER_STALL = 4   # スクロールしても新しいポストが増えない回数がこれだけ続いたら終了


def parse_args():
    p = argparse.ArgumentParser(description="Xアカウントのポストを1件ずつスクショ保存する")
    p.add_argument("account", help="アカウントURL (https://x.com/xxx) または @handle / handle")
    p.add_argument("--months", type=int, default=6, help="遡る期間(月数)。デフォルト6")
    p.add_argument("--days", type=int, default=None, help="遡る期間(日数)。指定時は--monthsより優先")
    p.add_argument(
        "--out",
        default=str(Path.home() / "Screenshots" / "x_archive"),
        help="保存先フォルダ",
    )
    return p.parse_args()


def normalize_account(account: str):
    """入力から (アカウントURL, ハンドル) を作る"""
    account = account.strip()
    if account.startswith("http"):
        url = account.rstrip("/")
        handle = url.rsplit("/", 1)[-1]
    else:
        handle = account.lstrip("@")
        url = f"https://x.com/{handle}"
    return url, handle


def load_seen_ids(state_path: Path) -> set:
    if state_path.exists():
        try:
            return set(json.loads(state_path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen_ids(state_path: Path, seen_ids: set):
    state_path.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")


def safe_goto(page, url, retries=3):
    """ログイン直後などにX側の自動リダイレクトと競合して
    'interrupted by another navigation' になることがあるので、少し待って再試行する"""
    last_err = None
    for attempt in range(retries):
        try:
            return page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            raise
        except Exception as e:
            if "interrupted by another navigation" not in str(e):
                raise
            last_err = e
            page.wait_for_timeout(1500)
    raise last_err


def ensure_logged_in(page):
    """ログイン状態を確認し、未ログインなら手動ログインを待つ"""
    safe_goto(page, "https://x.com/home")
    page.wait_for_timeout(2000)

    if not page.url.rstrip("/").endswith("/home"):
        print("=" * 60)
        print("Xにログインしていません。開いたブラウザ画面で手動でログインしてください。")
        print("ログインしてタイムライン(ホーム)が表示されたら、このターミナルに戻って")
        print("Enterキーを押してください。")
        print("=" * 60)
        input("ログイン完了後、Enterキーを押してください... ")


def extract_tweet_id(href):
    m = re.search(r"/status/(\d+)", href or "")
    return m.group(1) if m else None


def parse_relative_date(text: str, now: datetime):
    """Xの表示上の日時文字列(「40m」「2h」「1月11日」「2024年5月3日」等)を概算のdatetimeに変換する。
    解釈できない場合はNoneを返す。"""
    text = (text or "").strip()

    m = re.fullmatch(r"(\d+)s", text)
    if m:
        return now - timedelta(seconds=int(m.group(1)))
    m = re.fullmatch(r"(\d+)m", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.fullmatch(r"(\d+)h", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.fullmatch(r"(\d+)d", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(\d+)年(\d+)月(\d+)日", text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d+)月(\d+)日", text)
    if m:
        mo, d = map(int, m.groups())
        try:
            candidate = datetime(now.year, mo, d)
        except ValueError:
            return None
        if candidate > now:
            candidate = candidate.replace(year=now.year - 1)
        return candidate
    return None


def get_tweet_info(article, handle_lower: str, now: datetime):
    """article要素から、対象アカウント自身の投稿かどうか判定し、ID・概算日時を返す。
    対象アカウント自身の投稿でなければNone(他ユーザーの返信プレビュー等)。"""
    link_el = article.query_selector('a[href*="/status/"]')
    if not link_el:
        return None
    href = link_el.get_attribute("href") or ""
    if not href.lower().startswith(f"/{handle_lower}/status/"):
        return None  # 他ユーザーの投稿(返信プレビュー等) or 単純リポスト

    tweet_id = extract_tweet_id(href)
    if not tweet_id:
        return None

    date_text = link_el.inner_text()
    posted_at = parse_relative_date(date_text, now)

    pinned = article.query_selector('[data-icon="icon-pin-fill"]') is not None
    return {"id": tweet_id, "posted_at": posted_at, "pinned": pinned}


def run(account_url: str, handle: str, days: int, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "seen_ids.json"
    seen_ids = load_seen_ids(state_path)
    handle_lower = handle.lower()

    saved = 0
    with sync_playwright() as p:
        # headless(画面非表示)だとX側のbot対策でブロックされるため、画面表示ありで実行する。
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            channel="chrome",
            viewport=None,  # 固定サイズにすると画面より大きくなりスクロールできなくなるため、実ウィンドウのサイズをそのまま使う
            locale="ja-JP",
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        now = datetime.now()
        cutoff = now - timedelta(days=days)

        print(f"タイムラインを開きます: {account_url}")
        safe_goto(page, account_url)
        page.wait_for_timeout(2500)

        # seen_ids(ファイルに保存する方)には「実際にスクショを保存できたID」だけを入れる。
        # session_ids はこの実行中だけの重複処理防止用(失敗した投稿も含めて記録し、
        # 同じ実行内で何度もリトライし続けないようにする)。
        session_ids = set(seen_ids)
        consecutive_old = 0
        failed = 0
        no_new_scrolls = 0
        last_height = 0

        while True:
            articles = page.query_selector_all("article")
            new_this_round = 0

            for article in articles:
                try:
                    info = get_tweet_info(article, handle_lower, now)
                except Exception:
                    continue
                if not info or info["id"] in session_ids:
                    continue

                session_ids.add(info["id"])
                new_this_round += 1

                if info["pinned"] or info["posted_at"] is None:
                    # 固定ポスト、または日付を解釈できなかったものは期間フィルタの対象外
                    consecutive_old = 0
                elif info["posted_at"] < cutoff:
                    consecutive_old += 1
                    if consecutive_old >= STOP_AFTER_OLD:
                        break
                    continue
                else:
                    consecutive_old = 0

                date_label = (
                    info["posted_at"].strftime("%Y%m%d") if info["posted_at"] else "unknown"
                )
                filename = f"{date_label}_{info['id']}.png"
                filepath = out_dir / filename
                if filepath.exists():
                    seen_ids.add(info["id"])
                    continue
                try:
                    article.scroll_into_view_if_needed()
                    page.wait_for_timeout(400)
                    article.screenshot(path=str(filepath))
                    saved += 1
                    seen_ids.add(info["id"])  # 保存できたものだけ「完了」として記録する
                    print(f"[{saved}] 保存: {filename}")
                except Exception as e:
                    # ここで seen_ids に入れない = 次回実行時にもう一度トライ対象になる
                    failed += 1
                    print(f"  スクショ失敗 ({info['id']}): {e} → 次回実行時に再試行されます")

            if consecutive_old >= STOP_AFTER_OLD:
                print(f"{days}日より古いポストが{STOP_AFTER_OLD}件連続したため終了します。")
                break

            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1800)

            new_height = page.evaluate("document.body.scrollHeight")
            if new_this_round == 0 and new_height == last_height:
                no_new_scrolls += 1
                if no_new_scrolls >= STOP_AFTER_STALL:
                    print("これ以上新しいポストが読み込まれないため終了します。")
                    break
            else:
                no_new_scrolls = 0
            last_height = new_height

        save_seen_ids(state_path, seen_ids)
        context.close()

    msg = f"完了。今回 {saved} 件のポストを保存しました。保存先: {out_dir}"
    if failed:
        msg += f"\n({failed} 件は一時的な失敗のため未保存 → 次回実行時に自動で再試行されます)"
    print(msg)


def main():
    args = parse_args()
    account_url, handle = normalize_account(args.account)
    out_dir = Path(args.out)
    days = args.days if args.days is not None else args.months * 30
    run(account_url, handle, days, out_dir)


if __name__ == "__main__":
    main()

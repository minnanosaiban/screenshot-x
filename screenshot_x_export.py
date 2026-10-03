#!/usr/bin/env python3
"""
アーカイブ(screenshot_x_detail.pyが貯めたスクショ)から、期間を指定して書き出す。

書き出せるものは2つ。
    1. 提出用フォルダ … 期間内のPNGを 001_ から連番を振ってコピーし、一覧.csv を添える
    2. sidenote用JSON … サイドノートアプリ(https://sidenote-pdf.pages.dev/)で開くファイル。
       1枚ごとに「投稿日時 / 投稿URL / 取得日」のサイドノートを入れた状態で作る

原本(アーカイブ)には一切手を触れない。連番は書き出しの時にだけ振るので、
何度書き出しても、別の期間を書き出しても、既に引用した番号がずれることはない。

使い方:
    python screenshot_x_export.py <アカウント> --from 2026-08-01 --to 2026-08-31 [--out 出力先]
    python screenshot_x_export.py exampleuser --from 2026-08-01 --to 2026-08-31 --json-only

【sidenote用JSONについて】
画像はJSONの中にbase64で埋め込まれる(アプリがそういう作りで、1ファイルを相手に渡せば
そのまま見られるようにするため)。1枚あたり約190KB、150枚で約31MBになる。
150枚でも読み込みは1秒程度・PDF化も8秒程度で問題なく動くことは実測済みだが、
アプリ側のブラウザ内自動保存(上限5MB程度)は効かないので、作業中はこまめに「保存」を押すこと。
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import shutil
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

from PIL import Image

from screenshot_x_detail import (
    DEFAULT_ARCHIVE_ROOT,
    RANK_LABELS,
    Archive,
    blank_row_above,
    parse_date,
)

# スクショの左端にある固定メニューが占める幅の割合（幅875pxのうち約110px）。
# 切れ目を探すときにこの範囲を見ないようにするために使う。
NAV_COLUMN_RATIO = 0.14
from screenshot_x import launch_chromium, normalize_account

# サイドノートアプリの画像ブロックが持っている削除ボタンのアイコン。
# アプリが作るHTMLと同じ形にしておかないと、読み込んだ後の見た目が変わってしまう。
ICON_X = (
    '<svg class="icon" xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 16 16"'
    ' aria-hidden="true"><path d="M2.146 2.854a.5.5 0 1 1 .708-.708L8 7.293l5.146-5.147a.5.5 0 0 1'
    ' .708.708L8.707 8l5.147 5.146a.5.5 0 0 1-.708.708L8 8.707l-5.146 5.147a.5.5 0 0'
    ' 1-.708-.708L7.293 8z"></path></svg>'
)


def parse_args():
    p = argparse.ArgumentParser(description="アーカイブから期間を指定して書き出す")
    p.add_argument("account", help="アカウント (@handle / handle / URL)")
    p.add_argument("--from", dest="date_from", required=True, help="開始日 (2026-08-01)")
    p.add_argument("--to", dest="date_to", default=None, help="終了日 (2026-08-31)。省略時は今日")
    p.add_argument("--root", default=str(DEFAULT_ARCHIVE_ROOT), help="アーカイブの置き場所")
    p.add_argument("--out", default=None, help="書き出し先フォルダ。省略時はアーカイブの隣に作る")
    p.add_argument("--rank", type=int, choices=[1, 2, 3], default=None,
                   help="この重要度以上だけ書き出す（3=重要 2=普通 1=対象外）")
    p.add_argument("--images", action="store_true", help="提出用フォルダを作る")
    p.add_argument("--json", action="store_true", help="sidenote用JSONを作る")
    p.add_argument("--pdf", action="store_true", help="PDFを作る（サイドノートアプリを経由しない）")
    p.add_argument(
        "--note",
        default=",".join(NOTE_ITEMS),
        help="サイドノートに入れる項目をカンマ区切りで指定"
             f"（{'/'.join(NOTE_ITEMS)}、既定は全部。none で注釈なし）",
    )
    return p.parse_args()


def parse_note_items(text: str):
    chosen = {part.strip() for part in (text or "").split(",") if part.strip()}
    if chosen == {"none"}:
        return {key: False for key in NOTE_ITEMS}
    unknown = chosen - set(NOTE_ITEMS)
    if unknown:
        raise ValueError(f"--note に使えない項目です: {', '.join(sorted(unknown))}")
    return {key: key in chosen for key in NOTE_ITEMS}


def select_posts(archive: Archive, start: date, end: date, min_rank=None, log=print):
    """期間内の投稿を、投稿日時の古い順で返す。日時が無いものは対象外。
    min_rank を指定すると、仕分けでそれ以上の重要度が付いたものだけに絞る
    （重要度が未設定のものは、絞り込みを掛けた時点で外れる）。"""
    selected = []
    undated = []
    for post in archive.sorted_posts():
        if not post.get("posted_at"):
            # 投稿日時を読めなかったもの。期間で選べないので必ず対象外になるが、
            # 黙って落とすと証拠が1件欠けたことに気づけないので知らせる。
            undated.append(post["filename"])
            continue
        posted_at = datetime.fromisoformat(post["posted_at"])
        if not (start <= posted_at.date() <= end):
            continue
        if min_rank and (post.get("rank") or 0) < min_rank:
            continue
        selected.append((posted_at, post))

    if undated:
        log(f"  ※ 投稿日時が読めなかった {len(undated)}件は書き出せません: "
            + "、".join(undated[:5]) + ("…" if len(undated) > 5 else ""))
    return selected


def verify_hashes(archive: Archive, selected, log=print):
    """書き出す画像が、撮影時に台帳へ記録したSHA-256と一致するか確かめる。
    不一致やファイル欠落は、改変・破損・移動の可能性があるので、書き出す前に知らせる。
    戻り値は問題のあった件数。"""
    problems = 0
    for _, post in selected:
        path = archive.posts_dir / post["filename"]
        expected = post.get("sha256")
        if not path.exists():
            log(f"  ※ ファイルがありません: {post['filename']}")
            problems += 1
        elif expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            log(f"  ※ 撮影時から内容が変わっています（SHA-256が一致しません）: {post['filename']}")
            problems += 1
    return problems


# サイドノートに入れる項目。書き出しのときに個別に外せる。
NOTE_ITEMS = ("time", "url", "captured", "rank", "memo")
NOTE_ITEM_LABELS = {
    "time": "投稿日時",
    "url": "投稿URL",
    "captured": "取得日",
    "rank": "重要度",
    "memo": "メモ",
}


def all_note_items():
    return {key: True for key in NOTE_ITEMS}


def source_note_text(archive: Archive, posted_at: datetime, post, items=None) -> str:
    """1枚ごとにサイドノートへ入れる出典。「いつの投稿か・どこにあるか・いつ撮ったか」と重要度。
    items で項目を外せる。全部外した場合は空文字を返す（呼び出し側でノート自体を作らない）。"""
    items = items or all_note_items()
    lines = []
    if items.get("time"):
        lines.append(
            f"{posted_at.year}年{posted_at.month}月{posted_at.day}日 "
            f"{posted_at.hour}:{posted_at.minute:02d}"
        )
    if items.get("url"):
        lines.append(archive.post_url(post["id"]))

    tail = []
    captured = post.get("captured_at")
    if items.get("captured") and captured:
        c = datetime.fromisoformat(captured)
        tail.append(f"（{c.year}年{c.month}月{c.day}日 取得）")
    if items.get("rank") and post.get("rank"):
        tail.append(f"重要度：{RANK_LABELS[post['rank']]}")
    if tail:
        lines.append("　".join(tail))
    return "\n".join(lines)


def memo_text(post, items=None) -> str:
    items = items or all_note_items()
    return post.get("memo", "").strip() if items.get("memo") else ""


def image_block_html(para_id: str, data_url: str, has_note=True) -> str:
    """サイドノートアプリが画像を挿入したときと同じHTMLを組み立てる。
    アプリは読み込み時にこのHTMLをそのまま本文に入れてからボタンを繋ぎ直すので、
    ボタン類も含めて同じ形にしておく必要がある。

    has_note=False のときは通し番号のバッジを置かない（コメントが1件も無い画像は、
    アプリ側でも番号が付かず「＋ ノート」から書き始める状態になる）。"""
    badge = (
        f'<span class="note-anchor img-note-anchor" data-anchor-id="{para_id}">'
        '<sup class="note-num"></sup></span>'
        if has_note else ""
    )
    return (
        f'<div class="para para-image" contenteditable="false" data-para-id="{para_id}">'
        f'<div class="para-image-notes">{badge}</div>'
        '<div class="para-image-inner"><div class="para-image-meta">'
        '<button type="button" class="para-image-note-btn"'
        ' title="この画像にコメントを追加（出典はサイドノートに記載してください）">＋ ノート</button>'
        f'<button type="button" class="para-image-del-btn" title="この画像を削除">{ICON_X}</button>'
        '</div>'
        f'<img class="para-image-img" src="{data_url}" alt="スクショ"></div></div>'
    )


def build_sidenote_project(archive: Archive, selected, title: str, items=None):
    """サイドノートアプリのプロジェクト(version 3・本文モード)を組み立てる。

    1つの画像に付くコメントは「出典」→「仕分けのメモ」の順に積む。アプリ側では
    同じ通し番号のスレッドとして並び、続きは「＋ 返信」から書き足せる。"""
    items = items or all_note_items()
    blocks = []
    notes_by_anchor = []
    reply_seq = 1
    image_no = 0
    for posted_at, post in selected:
        # 返信まで撮った縦長のスクショは、サイドノートアプリ側のPDF化でも1ページに
        # 収まらないので、ここで切り分けて複数の画像として並べる。
        with Image.open(archive.posts_dir / post["filename"]) as image:
            pieces = slice_for_pages(image.convert("RGB"))

        notes = []
        source = source_note_text(archive, posted_at, post, items)
        if source:
            notes.append({"id": f"r{reply_seq}", "text": source, "color": "black"})
            reply_seq += 1
        memo = memo_text(post, items)
        if memo:
            notes.append({"id": f"r{reply_seq}", "text": memo, "color": "black"})
            reply_seq += 1

        for position, piece in enumerate(pieces):
            image_no += 1
            para_id = f"img{image_no}"
            buffer = io.BytesIO()
            piece.save(buffer, format="PNG")
            data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            # 注釈は1枚目にだけ付ける（2枚目以降は同じ投稿の続き）
            first_piece = position == 0
            blocks.append(image_block_html(para_id, data_url, has_note=first_piece and bool(notes)))
            if first_piece and notes:
                notes_by_anchor.append([para_id, notes])

    return {
        "app": "sidenote-pdf",
        "version": 3,
        "title": title,
        "savedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "mode": "text",
        "notesByAnchor": notes_by_anchor,
        "anchorIdSeq": 1,
        # メモの分だけコメントが増えるので、件数ではなく実際に使った番号の続きを渡す
        # （ここがずれると、アプリ側で足した返信のIDが既存とぶつかる）
        "replyIdSeq": reply_seq,
        "colorNames": {"black": "自分", "blue": "共有相手"},
        # 画像の後ろに空の段落を1つ置く。これが無いと最後の画像より下に文字を打てなくなる。
        "docHTML": "".join(blocks) + '<div class="para"><br></div>',
        # 縦長のスクショは切り分けるので、投稿の件数ではなく実際に置いた画像の枚数で数える
        "imageIdSeq": image_no + 1,
    }


# PDFの版面。サイドノートアプリの印刷用CSSと同じ寸法にしてあるので、
# アプリ経由で作ったPDFと見た目が揃う（本文115mm・注釈48mm・A4）。
PDF_CSS = """
/* 余白10mm。スクショ1枚は115mm幅なら132.7mm高になるので、A4(297mm)に2枚
   （265.4mm + 間隔）を載せるには、この余白でないと1枚しか入らない。 */
@page { size: A4; margin: 10mm; }
body { font-family: "Yu Gothic", "Meiryo", sans-serif; color: #111; margin: 0; }
h1 { font-size: 12pt; margin: 0 0 4mm; }
/* 画像と注釈は「入れ子にせず、兄弟として並べる」。div で包むと1ページに1枚しか
   載らなくなる（包んだ箱の中が全てfloatで高さが0になり、改ページが噛み合わないため）。
   サイドノートアプリの印刷版面も同じ理由で兄弟に並べている。 */
.shot { width: 115mm; float: left; clear: both; margin: 0 0 10pt; display: block;
        border: 1px solid #ddd; }
.note { width: 48mm; float: right; clear: right; margin: 0 0 8pt;
        font-size: 8.5pt; line-height: 1.6; color: #333;
        /* 出典のURLは区切りが無いので、指定しないと枠からはみ出して切れる */
        overflow-wrap: anywhere; }
.note .num { color: #666; font-size: .75em; vertical-align: super; margin-right: 2px; }
.note .cont { color: #888; }
.note .memo { display: block; margin-top: 4pt; }
"""


# 版面の寸法。スクショは115mm幅で置くので、A4(297mm)・余白10mmだと
# 1ページに入る高さは277mm。ただし1ページ目にはタイトルが乗り、画像には枠線と
# 下余白も付くため、そのぶんを見て255mmを上限にする。
# ここを大きくしすぎると「1ページ目がタイトルだけで空になる」状態が起きる。
PAGE_IMAGE_WIDTH_MM = 115
PAGE_IMAGE_MAX_HEIGHT_MM = 255


def find_blank_row(image, target: int, search: int = 160):
    """target の少し手前で、文字も罫線も無い行を探す。
    返信を含む縦長のスクショをページに切り分けるとき、文字の途中で切らないため。

    左メニューの領域は見ない。そこにボタンが並んでいるだけで「無地の行が無い」と
    判定されてしまうため（撮影側の blank_row_above と同じ理由・同じ実装を使う）。"""
    left = round(image.width * NAV_COLUMN_RATIO)
    return blank_row_above(image, target, left=left, right=image.width, limit=search)


def slice_for_pages(image):
    """A4の1ページに収まらない縦長画像を、ページに収まる高さで切り分ける。
    収まるものはそのまま1枚で返す。

    上限いっぱいで順に切ると、末尾に数ミリだけの切れ端が残って
    「◯の続き」だけが載ったページができてしまう。残りの高さから毎回
    必要な枚数を計算し直すことで、均等に分けつつ、切れ目を探して短くなった
    ぶんが最後の1枚に溜まって上限を超えるのも防ぐ。"""
    limit = round(image.width * PAGE_IMAGE_MAX_HEIGHT_MM / PAGE_IMAGE_WIDTH_MM)
    if image.height <= limit:
        return [image]

    pieces = []
    top = 0
    while top < image.height:
        remaining = image.height - top
        if remaining <= limit:
            pieces.append(image.crop((0, top, image.width, image.height)))
            break
        # 残りを何枚に分けるかを毎回決め直す（切れ目探しでずれた分をここで吸収する）
        step = -(-remaining // (-(-remaining // limit)))
        bottom = find_blank_row(image, min(top + step, image.height))
        if bottom <= top + step // 2:          # 適当な切れ目が無ければそのままの位置で切る
            bottom = min(top + step, image.height)
        pieces.append(image.crop((0, top, image.width, bottom)))
        top = bottom
    return pieces


def build_pdf_html(archive: Archive, selected, title: str, items=None, slice_dir: Path = None) -> str:
    """PDFのもとになるHTML。画像はファイルを直接参照する（base64にしないので軽い）。"""
    items = items or all_note_items()
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{escape(title)}</title><style>{PDF_CSS}</style>",
        f"<h1>{escape(title)}</h1>",
    ]
    number = 0
    for index, (posted_at, post) in enumerate(selected, start=1):
        original = archive.posts_dir / post["filename"]
        with Image.open(original) as image:
            pieces = slice_for_pages(image.convert("RGB"))

        sources = []
        if len(pieces) == 1:
            sources.append(original.resolve().as_uri())
        else:
            # 返信まで撮った縦長のスクショは1ページに収まらないので、切り分けて並べる
            for part_no, piece in enumerate(pieces, start=1):
                piece_path = slice_dir / f"{index:04d}_{part_no}.png"
                piece.save(piece_path)
                sources.append(piece_path.resolve().as_uri())

        source = source_note_text(archive, posted_at, post, items)
        memo = memo_text(post, items)
        has_note = bool(source or memo)
        if has_note:
            number += 1
        for position, src in enumerate(sources):
            parts.append(f"<img class='shot' src='{src}'>")
            # 注釈は1枚目にだけ付ける（2枚目以降は同じ投稿の続き）。
            # 注釈が無い設定のときは「続き」も出さない（番号が無いので
            # 「0 の続き」や前の投稿の番号を指す表示になってしまう）。
            if not has_note:
                continue
            if position == 0:
                note = escape(source).replace("\n", "<br>")
                memo_html = f"<span class='memo'>{escape(memo)}</span>" if memo else ""
                parts.append(
                    f"<aside class='note'><span class='num'>{number}</span>{note}{memo_html}</aside>"
                )
            else:
                parts.append(
                    f"<aside class='note'><span class='cont'>{number} の続き</span></aside>"
                )
    return "".join(parts)


def export_pdf(archive: Archive, selected, path: Path, title: str, log=print, items=None):
    """ブラウザの印刷機能でPDFにする。サイドノートアプリを経由しないので、
    出典とメモだけのPDFならこれ1手で作れる。"""
    from playwright.sync_api import sync_playwright

    path.parent.mkdir(parents=True, exist_ok=True)
    html_path = path.with_suffix(".html")
    slice_dir = path.parent / f"_{path.stem}_tmp"
    slice_dir.mkdir(exist_ok=True)
    try:
        html_path.write_text(
            build_pdf_html(archive, selected, title, items, slice_dir), encoding="utf-8"
        )
        with sync_playwright() as p:
            # 取得側と同じく、入っているGoogle Chrome(無ければEdge)を使う。
            # 既定のchromiumを使うと `playwright install chromium` が別途必要になり、
            # 取得は動くのに書き出しだけ失敗する、という分かりにくい状態になる。
            browser = launch_chromium(p.chromium, headless=True)
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(500)   # 画像の描画待ち
            page.pdf(
                path=str(path),
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
            )
            browser.close()
    finally:
        # 中間ファイル（HTMLと、切り分けた画像）は残さない
        html_path.unlink(missing_ok=True)
        shutil.rmtree(slice_dir, ignore_errors=True)

    log(f"PDF: {path}（{len(selected)}枚・{path.stat().st_size / 1024 / 1024:.1f} MB）")
    return path


def export_images(archive: Archive, selected, out_dir: Path, log=print):
    """提出用フォルダ。ここで初めて 001 からの連番を振る(原本の名前は変えない)。"""
    if out_dir.exists():
        # 前回より件数が減ると、古い連番のPNGが残って一覧.csv に載らない
        # 「どこにも記載の無い証拠」になってしまうので、作り直す
        stale = [p for p in out_dir.iterdir() if p.is_file()]
        if stale:
            log(f"  前回の書き出し {len(stale)}件を消してから作り直します: {out_dir.name}")
        for path in stale:
            path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for seq, (posted_at, post) in enumerate(selected, start=1):
        name = f"{seq:03d}_{posted_at:%Y%m%d_%H%M}.png"
        shutil.copy2(archive.posts_dir / post["filename"], out_dir / name)
        rows.append(
            [
                seq,
                posted_at.strftime("%Y-%m-%d %H:%M"),
                archive.post_url(post["id"]),
                name,
                (post.get("captured_at") or "").replace("T", " "),
                post.get("sha256", ""),
            ]
        )

    with (out_dir / "一覧.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["番号", "投稿日時", "投稿URL", "ファイル名", "取得日時", "SHA-256"])
        writer.writerows(rows)

    log(f"提出用フォルダ: {out_dir}（{len(rows)}件）")
    return out_dir


def export_sidenote_json(archive: Archive, selected, path: Path, title: str, log=print, items=None):
    project = build_sidenote_project(archive, selected, title, items)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    size_mb = path.stat().st_size / 1024 / 1024
    log(f"sidenote用JSON: {path}（{len(selected)}枚・{size_mb:.1f} MB）")
    if size_mb > 40:
        log("  ※ 大きいので、期間を分けて書き出すことをお勧めします。")
    return path


def export(
    handle: str,
    start: date,
    end: date,
    root: Path,
    out_root: Path = None,
    want_images=True,
    want_json=True,
    want_pdf=False,
    min_rank=None,
    note_items=None,
    log=print,
):
    archive = Archive(Path(root), handle)
    if not archive.manifest_path.exists():
        raise FileNotFoundError(f"アーカイブが見つかりません: {archive.dir}")

    selected = select_posts(archive, start, end, min_rank, log=log)
    if not selected:
        condition = f"{start:%Y-%m-%d}〜{end:%Y-%m-%d}"
        if min_rank:
            condition += f"・重要度「{RANK_LABELS[min_rank]}」以上"
        log(f"{condition} に該当する投稿がアーカイブにありません。")
        return []

    problems = verify_hashes(archive, selected, log=log)
    if problems:
        raise RuntimeError(
            f"{problems}件のスクショに問題があるため書き出しを中止しました。上のログを確認してください。"
        )

    suffix = f"_{RANK_LABELS[min_rank]}以上" if min_rank else ""
    label = f"{start:%Y-%m-%d}_{end:%Y-%m-%d}{suffix}"
    if min_rank:
        log(f"重要度「{RANK_LABELS[min_rank]}」以上に絞り込みます。")
    out_root = Path(out_root) if out_root else Path(root) / "書き出し"
    title = f"{handle}_{start:%Y%m%d}-{end:%Y%m%d}{suffix}"
    note_items = note_items or all_note_items()
    log(f"{start:%Y-%m-%d}〜{end:%Y-%m-%d}: {len(selected)}件を書き出します。")

    if want_json or want_pdf:
        on = [NOTE_ITEM_LABELS[k] for k in NOTE_ITEMS if note_items.get(k)]
        log("サイドノートに入れる項目: " + ("・".join(on) if on else "なし（画像だけ）"))

    made = []
    if want_images:
        made.append(export_images(archive, selected, out_root / f"提出用_{handle}_{label}", log=log))
    if want_json:
        made.append(
            export_sidenote_json(
                archive, selected, out_root / f"sidenote-{title}.json", title,
                log=log, items=note_items,
            )
        )
    if want_pdf:
        made.append(
            export_pdf(archive, selected, out_root / f"{title}.pdf", title,
                       log=log, items=note_items)
        )
    return made


def main():
    args = parse_args()
    _, handle = normalize_account(args.account)
    start = parse_date(args.date_from)
    end = parse_date(args.date_to) if args.date_to else date.today()
    # 何も指定が無ければ、提出用フォルダとJSONの両方を作る（従来どおり）
    chosen = args.images or args.json or args.pdf
    export(
        handle,
        start,
        end,
        Path(args.root),
        Path(args.out) if args.out else None,
        want_images=args.images or not chosen,
        want_json=args.json or not chosen,
        want_pdf=args.pdf,
        min_rank=args.rank,
        note_items=parse_note_items(args.note),
    )


if __name__ == "__main__":
    main()

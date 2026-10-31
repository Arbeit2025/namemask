"""仮名化マスカー namemask

AI にテキストを貼る前に、メールアドレス・電話番号・郵便番号・URL などを
仮名（【メール1】など）に置換し、AI の回答を元の表記へ復元できるツール。
単一ファイル Flask + SQLite（対応表の保存用）。
"""
import json
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, redirect, render_template_string, request, url_for

app = Flask(__name__)
JST = ZoneInfo("Asia/Tokyo")
DB_PATH = os.environ.get("NAMEMASK_DB", os.path.join(os.path.dirname(__file__), "namemask.db"))

# 適用順に注意：電話番号の前半（090-1234 等）が郵便番号パターンに
# 誤マッチしないよう、電話を郵便番号より先に処理する。
PATTERNS = [
    ("メール", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("URL", re.compile(r"https?://[^\s<>\"'）」]+")),
    ("電話", re.compile(r"0\d{1,4}-\d{1,4}-\d{4}|0\d{9,10}")),
    ("郵便番号", re.compile(r"(?<![\d-])〒?\d{3}-\d{4}(?![\d-])")),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS maps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entries TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


def mask_text(text, dictionary):
    """dictionary: [(原文, 仮名), ...] を先に適用し、その後パターン置換する。
    戻り値: (置換後テキスト, [(原文, 仮名), ...])"""
    entries = []
    result = text
    for original, alias in sorted(dictionary, key=lambda p: -len(p[0])):
        if original and alias and original in result:
            result = result.replace(original, alias)
            entries.append((original, alias))
    counters = {}
    seen = {}

    def repl(kind):
        def _r(m):
            original = m.group(0)
            if original in seen:
                return seen[original]
            counters[kind] = counters.get(kind, 0) + 1
            alias = "【" + kind + str(counters[kind]) + "】"
            seen[original] = alias
            entries.append((original, alias))
            return alias

        return _r

    for kind, pattern in PATTERNS:
        result = pattern.sub(repl(kind), result)
    return result, entries


def restore_text(text, entries):
    """entries: [(原文, 仮名), ...] 仮名の長いものから先に戻す。"""
    result = text
    for original, alias in sorted(entries, key=lambda p: -len(p[1])):
        result = result.replace(alias, original)
    return result


INDEX = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>仮名化マスカー</title>
<style>body{font-family:sans-serif;max-width:720px;margin:2em auto;padding:0 1em}
textarea{width:100%}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em}</style>
</head><body>
<h1>仮名化マスカー</h1>
<p>AI に貼る前に、メール・電話番号・郵便番号・URL を仮名に置き換えます。</p>
<form method="post" action="{{ url_for('do_mask') }}">
  <textarea name="text" rows="8" placeholder="元のテキストを貼り付け">{{ text or '' }}</textarea>
  <h3>ユーザー辞書（任意）</h3>
  <p>原文 → 仮名 のペア（先に適用されます）</p>
  {% for i in range(3) %}
  <input type="text" name="orig" placeholder="原文（例：株式会社ほげ）">
  <input type="text" name="alias" placeholder="仮名（例：【会社A】）"><br>
  {% endfor %}
  <h3>対応表の保存（任意）</h3>
  <label>名前 <input type="text" name="map_name" placeholder="保存名を入れると対応表を保存します"></label><br>
  <button type="submit">仮名化する</button>
</form>
{% if masked is not none %}
<h2>仮名化したテキスト</h2>
<textarea rows="8" readonly>{{ masked }}</textarea>
<h2>対応表</h2>
<table><tr><th>原文</th><th>仮名</th></tr>
{% for o, a in entries %}<tr><td>{{ o }}</td><td>{{ a }}</td></tr>{% endfor %}
</table>
{% endif %}
<h2>保存済みの対応表</h2>
<ul>
{% for m in maps %}
<li>{{ m.created_at }} {{ m.name }}（<a href="{{ url_for('restore_page', map_id=m.id) }}">復元に使う</a>） <form method="post" action="{{ url_for('delete_map', map_id=m.id) }}" style="display:inline" onsubmit="return confirm('この対応表を削除しますか？')"><button type="submit">削除</button></form></li>
{% else %}<li>なし</li>{% endfor %}
</ul>
</body></html>"""

RESTORE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>復元 - 仮名化マスカー</title>
<style>body{font-family:sans-serif;max-width:720px;margin:2em auto;padding:0 1em}textarea{width:100%}</style>
</head><body>
<h1>復元</h1>
<p>対応表「{{ map_row.name }}」を使って、仮名を元の表記に戻します。</p>
<form method="post">
  <textarea name="text" rows="8" placeholder="AI の回答などを貼り付け">{{ text or '' }}</textarea>
  <button type="submit">復元する</button>
</form>
{% if restored is not none %}
<h2>復元結果</h2>
<textarea rows="8" readonly>{{ restored }}</textarea>
{% endif %}
<p><a href="{{ url_for('index') }}">戻る</a></p>
</body></html>"""


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    db = get_db()
    maps = db.execute("SELECT * FROM maps ORDER BY id DESC").fetchall()
    return render_template_string(INDEX, text=None, masked=None, entries=[], maps=maps)


@app.route("/mask", methods=["POST"])
def do_mask():
    text = request.form.get("text", "")
    originals = request.form.getlist("orig")
    aliases = request.form.getlist("alias")
    dictionary = [(o.strip(), a.strip()) for o, a in zip(originals, aliases) if o.strip() and a.strip()]
    masked, entries = mask_text(text, dictionary)
    map_name = (request.form.get("map_name") or "").strip()
    if map_name and entries:
        db = get_db()
        db.execute(
            "INSERT INTO maps(name, entries, created_at) VALUES(?, ?, ?)",
            (map_name, json.dumps(entries, ensure_ascii=False), datetime.now(JST).strftime("%Y-%m-%d %H:%M")),
        )
        db.commit()
    db = get_db()
    maps = db.execute("SELECT * FROM maps ORDER BY id DESC").fetchall()
    return render_template_string(INDEX, text=text, masked=masked, entries=entries, maps=maps)


@app.route("/delete/<int:map_id>", methods=["POST"])
def delete_map(map_id):
    db = get_db()
    db.execute("DELETE FROM maps WHERE id = ?", (map_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/restore/<int:map_id>", methods=["GET", "POST"])
def restore_page(map_id):
    db = get_db()
    row = db.execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        return redirect(url_for("index"))
    restored = None
    text = request.form.get("text", "") if request.method == "POST" else ""
    if request.method == "POST":
        entries = [tuple(e) for e in json.loads(row["entries"])]
        restored = restore_text(text, entries)
    return render_template_string(RESTORE, map_row=row, text=text, restored=restored)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))

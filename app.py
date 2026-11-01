"""Local, learner-facing course quality feedback web app.

Uses only Python's standard library. The prototype stores anonymous responses
in a local SQLite database and shows course-level summaries after five
responses have been collected.
"""

from __future__ import annotations

import csv
import html
import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "templates" / "index.html"
CSS_PATH = ROOT / "static" / "style.css"
DATA_PATH = Path(os.environ.get("FEEDBACK_DB", ROOT / "data" / "feedback.db"))
MIN_GROUP_SIZE = 5
MAX_BODY_BYTES = 64_000
MAX_COMMENT_LENGTH = 1_000

DIMENSIONS = [
    ("clarity", "理解清楚", "本單元的學習目標與內容清楚易懂"),
    ("pace", "進度步調", "課程進度適合我的學習步調"),
    ("interaction", "參與互動", "課堂活動讓我有機會參與與表達"),
    ("materials", "教材資源", "教材與例子幫助我理解重點"),
    ("feedback", "學習回饋", "我能從教師回饋或評量中知道如何進步"),
]

TOPICS = {
    "進度步調": ("進度", "步調", "太快", "太慢", "節奏", "速度"),
    "教材資源": ("教材", "講義", "投影片", "範例", "資源"),
    "參與互動": ("互動", "討論", "提問", "分組", "參與", "活動"),
    "學習回饋": ("回饋", "批改", "評語", "說明", "評量"),
    "理解清楚": ("理解", "清楚", "不懂", "迷惘", "學會"),
}

IDENTIFIER_PATTERNS = [
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:(?:\+?886[-\s]?)0?9\d{2}|09\d{2})[-\s]?\d{3}[-\s]?\d{3}(?!\d)"),
    re.compile(r"(?:學號|身分證|證號|電話|手機)\s*[:：]?\s*[A-Z0-9-]{6,}", re.IGNORECASE),
]


def db_connect() -> sqlite3.Connection:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with db_connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course TEXT NOT NULL,
                clarity INTEGER NOT NULL CHECK (clarity BETWEEN 1 AND 5),
                pace INTEGER NOT NULL CHECK (pace BETWEEN 1 AND 5),
                interaction INTEGER NOT NULL CHECK (interaction BETWEEN 1 AND 5),
                materials INTEGER NOT NULL CHECK (materials BETWEEN 1 AND 5),
                feedback INTEGER NOT NULL CHECK (feedback BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL
            )
            """
        )


def fetch_responses(course: str | None = None) -> list[dict]:
    connection = db_connect()
    try:
        if course:
            rows = connection.execute(
                "SELECT * FROM responses WHERE course = ? ORDER BY submitted_at DESC",
                (course,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM responses ORDER BY submitted_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def score_means(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {}
    return {
        key: round(sum(int(row[key]) for row in rows) / len(rows), 1)
        for key, _label, _prompt in DIMENSIONS
    }


def topic_counts(rows: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in TOPICS}
    for row in rows:
        comment = str(row.get("comment", "")).strip()
        if not comment:
            continue
        matched = False
        for name, keywords in TOPICS.items():
            if any(word in comment for word in keywords):
                counts[name] += 1
                matched = True
        if not matched:
            counts["其他"] = counts.get("其他", 0) + 1
    return {name: count for name, count in counts.items() if count > 0}


def course_aggregates() -> list[dict]:
    rows = fetch_responses()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["course"], []).append(row)
    return [
        {"course": course, "count": len(items), **score_means(items)}
        for course, items in sorted(grouped.items(), key=lambda entry: entry[0].casefold())
        if len(items) >= MIN_GROUP_SIZE
    ]


def csv_safe(value: str) -> str:
    """Keep spreadsheet applications from executing course names as formulas."""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_page(title: str, main: str, active: str = "survey") -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("@@PAGE_TITLE@@", esc(title))
        .replace("@@SURVEY_ACTIVE@@", "active" if active == "survey" else "")
        .replace("@@DASHBOARD_ACTIVE@@", "active" if active == "dashboard" else "")
        .replace("@@MAIN@@", main)
    )


def render_form(error_message: str = "", course: str = "", ratings: dict | None = None,
                comment: str = "", submitted: bool = False) -> str:
    ratings = ratings or {}
    notice = (
        '<div class="notice success" role="status">謝謝你的回饋。課程團隊會參考整體意見調整教學。</div>'
        if submitted
        else ""
    )
    error = (
        f'<div class="notice error" role="alert">{esc(error_message)}</div>'
        if error_message
        else ""
    )
    fields = []
    for key, label, prompt in DIMENSIONS:
        options = []
        for value, scale_label in enumerate(("很不同意", "不同意", "普通", "同意", "很同意"), start=1):
            checked = " checked" if str(ratings.get(key, "")) == str(value) else ""
            options.append(
                f'<label class="score-option"><input type="radio" name="{key}" '
                f'value="{value}" required{checked}><span class="score-number">{value}</span>'
                f'<span class="score-word">{scale_label}</span></label>'
            )
        fields.append(
            f'<fieldset class="question"><legend><strong>{esc(label)}</strong>'
            f'<span>{esc(prompt)}</span></legend><div class="score-options">{"".join(options)}</div></fieldset>'
        )

    main = f"""
    <section class="hero">
      <p class="eyebrow">學習者回饋站</p>
      <h1>讓你的學習經驗<br>成為下一次改進的依據</h1>
      <p>用五項簡短評分和一則選填意見，告訴課程團隊哪些做法有效、哪裡還能更清楚。</p>
      <a class="hero-link" href="/dashboard">查看課程整體回饋 <span aria-hidden="true">→</span></a>
    </section>
    <section class="form-shell" aria-labelledby="form-title">
      <div class="section-heading">
        <div><p class="eyebrow dark">匿名填答</p><h2 id="form-title">本次課程回饋</h2></div>
        <p class="scale-note">1 分最低 · 5 分最高</p>
      </div>
      {notice}{error}
      <form method="post" action="/submit" class="feedback-form">
        <label class="course-label" for="course">課程或單元名稱 <span aria-hidden="true">*</span></label>
        <input id="course" name="course" type="text" maxlength="80" required
          placeholder="例如：單元一｜資料整理" value="{esc(course)}">
        <div class="questions">{"".join(fields)}</div>
        <label class="course-label" for="comment">想補充的建議 <span class="optional">選填</span></label>
        <p class="helper">可以分享最有幫助的環節，或你希望調整的地方。</p>
        <textarea id="comment" name="comment" maxlength="{MAX_COMMENT_LENGTH}"
          placeholder="請描述課程經驗或具體建議，不必留下個人資料。">{esc(comment)}</textarea>
        <p class="privacy-note"><strong>匿名與隱私：</strong>本表不收集姓名或學號。請勿填入姓名、學號、電話或電子郵件；回饋會以課程整體方式呈現。</p>
        <button class="primary-button" type="submit">送出匿名回饋</button>
      </form>
    </section>
    """
    return render_page("填寫課程回饋", main, "survey")


def render_rating_rows(means: dict[str, float]) -> str:
    rows = []
    for key, label, _prompt in DIMENSIONS:
        score = means[key]
        width = max(0, min(100, score * 20))
        rows.append(
            f'<div class="rating-row"><span>{esc(label)}</span>'
            f'<progress class="rating-track" max="100" value="{width:.0f}" '
            f'aria-label="{esc(label)} {score} 分，共 5 分"></progress>'
            f'<strong>{score:.1f}<small> / 5</small></strong></div>'
        )
    return "".join(rows)


def render_dashboard(course_filter: str = "") -> str:
    all_rows = fetch_responses()
    courses = sorted({row["course"] for row in all_rows}, key=str.casefold)
    if course_filter:
        rows = fetch_responses(course_filter)
    else:
        eligible_courses = {item["course"] for item in course_aggregates()}
        rows = [row for row in all_rows if row["course"] in eligible_courses]
    options = ['<option value="">所有課程</option>']
    for course in courses:
        selected = " selected" if course_filter == course else ""
        options.append(f'<option value="{esc(course)}"{selected}>{esc(course)}</option>')

    if not all_rows:
        insight = '<div class="empty-state"><h3>目前還沒有回饋資料</h3><p>可以先填寫一份匿名回饋，系統會在累積到足夠份數後顯示整體趨勢。</p><a href="/">前往填寫</a></div>'
    elif len(rows) < MIN_GROUP_SIZE:
        insight = (
            '<div class="privacy-state"><h3>整體分析尚未開放</h3>'
            f'<p>目前符合條件的回饋未達 {MIN_GROUP_SIZE} 份。為降低個別學習者被辨識的機會，'
            '系統會在累積足夠匿名回饋後，才顯示分數平均與文字主題。</p></div>'
        )
    else:
        means = score_means(rows)
        lowest_key = min(means, key=means.get)
        lowest_label = next(label for key, label, _prompt in DIMENSIONS if key == lowest_key)
        recommendations = {
            "clarity": "可在課前提供單元目標與範例，並於課中確認關鍵概念。",
            "pace": "可在重點段落增加停頓與練習時間，並讓學習者回報理解狀況。",
            "interaction": "可增加小組討論、提問或不同形式的參與機會。",
            "materials": "可整理教材重點，補上示例或課後複習資源。",
            "feedback": "可提供更明確的評量說明與下一步練習建議。",
        }
        topics = topic_counts(rows)
        topic_html = (
            "".join(f'<li><span>{esc(name)}</span><strong>{count} 則提及</strong></li>' for name, count in sorted(topics.items(), key=lambda item: (-item[1], item[0])))
            if topics
            else '<li class="muted">目前沒有可彙整的文字意見</li>'
        )
        insight = f"""
        <div class="summary-intro"><div><p class="eyebrow dark">課程整體結果</p><h3>{esc(course_filter or "所有達門檻課程")}</h3></div>
          <p class="response-count"><strong>{len(rows)}</strong><span>份匿名回饋</span></p></div>
        <section class="analysis-section"><h3>五項課程品質指標</h3>
          <div class="rating-list">{render_rating_rows(means)}</div>
          <p class="helper">分數為 1 至 5 分平均；此頁只顯示符合最小樣本數的整體結果。</p>
        </section>
        <div class="analysis-columns">
          <section class="analysis-section"><h3>文字意見主題</h3>
            <ul class="topic-list">{topic_html}</ul>
            <p class="helper">以關鍵詞規則分類；一則意見可能提及多個主題，不顯示原始留言。</p>
          </section>
          <section class="action-note"><p class="eyebrow dark">可討論的下一步</p>
            <h3>先檢視「{esc(lowest_label)}」</h3><p>{recommendations[lowest_key]}</p>
            <p class="helper">分析結果是討論線索，請由課程團隊結合教學情境確認。</p>
          </section>
        </div>
        """

    aggregate_rows = course_aggregates()
    aggregate_table = ""
    if aggregate_rows:
        tr = []
        for item in aggregate_rows:
            tr.append(
                "<tr>" + "".join(
                    f'<td>{esc(value)}</td>' for value in [
                        item["course"], item["count"], item["clarity"], item["pace"],
                        item["interaction"], item["materials"], item["feedback"],
                    ]
                ) + "</tr>"
            )
        aggregate_table = f"""
        <section class="analysis-section course-table-section"><div class="table-heading"><div><p class="eyebrow dark">已達彙整門檻</p><h3>課程概覽</h3></div>
        <a class="text-link" href="/export.csv">下載彙整 CSV</a></div>
        <div class="table-scroll"><table><thead><tr><th>課程</th><th>份數</th><th>理解</th><th>進度</th><th>互動</th><th>教材</th><th>回饋</th></tr></thead>
        <tbody>{"".join(tr)}</tbody></table></div>
        <p class="helper">僅匯出達到 {MIN_GROUP_SIZE} 份回饋的課程平均，不含原始留言或個人資料。</p></section>
        """

    main = f"""
    <section class="hero compact">
      <p class="eyebrow">學習者回饋概覽</p><h1>看見共同經驗，找到課程可以改進的地方</h1>
      <p>分數呈現整體趨勢，文字主題協助理解原因；所有結果都應回到教學情境中解讀。</p>
    </section>
    <section class="dashboard-shell">
      <div class="filter-bar"><div><p class="eyebrow dark">課程篩選</p><h2>整體回饋</h2></div>
        <form method="get" action="/dashboard"><label for="course-filter">選擇課程</label>
          <select id="course-filter" name="course">{"".join(options)}</select><button type="submit">查看</button></form>
      </div>
      {insight}
      {aggregate_table}
      <div class="dashboard-footnote"><strong>文字整理方式</strong><span>原型版採本機關鍵詞規則彙整，不會把留言送往外部 AI 服務。正式使用前，請依學校規範設定資料存取與保存期限。</span></div>
    </section>
    """
    return render_page("課程整體回饋", main, "dashboard")


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "CourseFeedbackPrototype/1.0"

    def send_bytes(self, body: bytes, content_type: str, status: int = 200,
                   extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, document: str, status: int = 200) -> None:
        self.send_bytes(document.encode("utf-8"), "text/html; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self.send_bytes(b"ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/static/style.css":
            self.send_bytes(CSS_PATH.read_bytes(), "text/css; charset=utf-8")
            return
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            self.send_html(render_form(submitted=query.get("submitted", [""])[0] == "1"))
            return
        if parsed.path == "/dashboard":
            query = parse_qs(parsed.query)
            self.send_html(render_dashboard(query.get("course", [""])[0].strip()))
            return
        if parsed.path == "/export.csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["課程", "匿名回饋份數", "理解清楚", "進度步調", "參與互動", "教材資源", "學習回饋"])
            for item in course_aggregates():
                writer.writerow([
                    csv_safe(item["course"]), item["count"], item["clarity"], item["pace"],
                    item["interaction"], item["materials"], item["feedback"],
                ])
            body = output.getvalue().encode("utf-8-sig")
            self.send_bytes(body, "text/csv; charset=utf-8", extra_headers={"Content-Disposition": "attachment; filename=course-feedback-summary.csv"})
            return
        if parsed.path == "/favicon.ico":
            self.send_bytes(b"", "image/x-icon", 204)
            return
        self.send_html(render_page("找不到頁面", '<div class="empty-state"><h1>找不到這個頁面</h1><a href="/">回到回饋首頁</a></div>'), 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        if urlsplit(self.path).path != "/submit":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid request length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_error(413, "Request body is too large")
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
            form = parse_qs(raw, keep_blank_values=True, strict_parsing=False)
        except (UnicodeDecodeError, ValueError):
            self.send_error(400, "Invalid form data")
            return

        values = {key: items[0].strip() if items else "" for key, items in form.items()}
        course = values.get("course", "")
        comment = values.get("comment", "")
        ratings = {key: values.get(key, "") for key, _label, _prompt in DIMENSIONS}
        error = ""
        if not course or len(course) > 80:
            error = "請填寫課程名稱（最多 80 個字）。"
        elif len(comment) > MAX_COMMENT_LENGTH:
            error = f"補充意見請控制在 {MAX_COMMENT_LENGTH} 字以內。"
        elif any(pattern.search(comment) for pattern in IDENTIFIER_PATTERNS):
            error = "補充意見中似乎包含電話、電子郵件或識別資訊，請移除後再送出。"
        else:
            for key, _label, _prompt in DIMENSIONS:
                if ratings[key] not in {"1", "2", "3", "4", "5"}:
                    error = "請為每一項課程品質指標選擇 1 至 5 分。"
                    break

        if error:
            self.send_html(render_form(error, course, ratings, comment), 400)
            return

        payload = {key: int(ratings[key]) for key in ratings}
        connection = db_connect()
        try:
            connection.execute(
                """INSERT INTO responses
                (course, clarity, pace, interaction, materials, feedback, comment, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    course, payload["clarity"], payload["pace"], payload["interaction"],
                    payload["materials"], payload["feedback"], comment,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.send_response(303)
        self.send_header("Location", "/?submitted=1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format_string: str, *args) -> None:
        # Keep routine server logs concise and avoid printing submitted content.
        super().log_message(format_string, *args)


def main() -> None:
    init_database()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), FeedbackHandler)
    print(f"課程品質回饋工具已啟動：http://{host}:{port}")
    print(f"本機資料庫：{DATA_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止伺服器。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


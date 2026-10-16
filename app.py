import csv
import io
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import (
    Flask, redirect, abort, request, jsonify,
    render_template_string, Response, send_file
)
import segno

load_dotenv()

app = Flask(__name__)
DB_PATH = os.environ.get("REDIRECT_DB", "redirects.db")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "super-secret-key-change-me")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
PAUSE_FALLBACK_URL = os.environ.get("PAUSE_FALLBACK_URL",
                                    "[https://yourdomain.com/paused](https://yourdomain.com/paused)")

# Target mapping: slug -> config dict
TARGETS = {
    "collx": {
        "url": "https://share.collx.app/Luap",
        "name": "Luap's CollX Profile",
        "active": os.environ.get("COLLX_ACTIVE", "true").lower() == "true"
    },
    "ebay": {
        "url": "https://www.ebay.com/str/luapslowendlounge",
        "name": "Luap's Low-End Lounge",
        "active": os.environ.get("EBAY_ACTIVE", "true").lower() == "true"
    }
}

BOT_REGEX = re.compile(
    r"(bot|crawl|spider|slurp|facebookexternalhit|whatsapp|telegrambot|"
    r"twitterbot|applebot|discordbot|slackbot|linkedinbot|pinterest|"
    r"bytespider|googlebot|bingbot|duckduckbot|yandex|preview)",
    re.IGNORECASE
)


def classify_device(ua: str) -> str:
    if not ua:
        return "Unknown"
    ua_lower = ua.lower()
    if "ipad" in ua_lower or "iphone" in ua_lower:
        return "iOS"
    if "android" in ua_lower:
        return "Android"
    if "macintosh" in ua_lower or "mac os" in ua_lower:
        return "macOS"
    if "windows" in ua_lower:
        return "Windows"
    if "linux" in ua_lower:
        return "Linux"
    return "Other"


def is_bot(user_agent: str) -> bool:
    if not user_agent or not user_agent.strip():
        return True
    return bool(BOT_REGEX.search(user_agent))


def send_click_webhook(slug: str, subtag: str, device: str, referrer: str):
    if not WEBHOOK_URL:
        return
    try:
        content = f"🎯 **Link Clicked!** `/{slug}`"
        if subtag:
            content += f" (Placement: `{subtag}`)"
        content += f"\n• **Device:** {device}\n• **Referrer:** {referrer or 'Direct'}"

        payload = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "RedirectNotifier/1.0"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        # Non-blocking telemetry failure
        pass


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS click_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                subtag TEXT,
                referrer TEXT,
                device_type TEXT,
                user_agent TEXT,
                ip_hash TEXT,
                clicked_at TEXT NOT NULL
            )
        """)
        # Schema migration check for subtag & device_type if DB already existed
        cursor = conn.execute("PRAGMA table_info(click_events)")
        existing_cols = [row[1] for row in cursor.fetchall()]
        if "subtag" not in existing_cols:
            conn.execute("ALTER TABLE click_events ADD COLUMN subtag TEXT")
        if "device_type" not in existing_cols:
            conn.execute(
                "ALTER TABLE click_events ADD COLUMN device_type TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_slug ON click_events(slug)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subtag ON click_events(subtag)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clicked_at ON click_events(clicked_at)")
        conn.commit()


init_db()


# --- Dynamic QR Code Generator Route ---
@app.route("/<slug>/qr")
@app.route("/<slug>/<subtag>/qr")
def generate_qr(slug, subtag=None):
    slug_clean = slug.lower()
    if slug_clean not in TARGETS:
        abort(404)

    # Reconstruct the tracking redirect URL to encode into the QR code
    base_url = request.host_url.rstrip("/")
    target_redirect = f"{base_url}/{slug_clean}"
    if subtag:
        target_redirect += f"/{subtag.lower()}"

    # Generate high-resolution SVG or PNG in memory
    qr = segno.make(target_redirect, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=8, border=2)
    buffer.seek(0)

    return send_file(buffer, mimetype="image/png")


# --- Core Redirect Route ---
@app.route("/<slug>")
@app.route("/<slug>/<subtag>")
def handle_redirect(slug, subtag=None):
    slug_clean = slug.lower()
    subtag_clean = subtag.lower() if subtag else None
    config = TARGETS.get(slug_clean)

    if not config:
        abort(404)

    # Check vacation/pause state
    destination_url = config["url"] if config["active"] else PAUSE_FALLBACK_URL

    raw_user_agent = request.headers.get("User-Agent", "")

    # Bot/Preview filter: redirect immediately without writing to DB
    if is_bot(raw_user_agent):
        return redirect(destination_url, code=302)

    # Parse visitor telemetry
    device = classify_device(raw_user_agent)
    referrer = request.headers.get("Referer", "")
    raw_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    ip_hash = str(hash(raw_ip.split(",")[0].strip())) if raw_ip else None
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO click_events 
               (slug, subtag, referrer, device_type, user_agent, ip_hash, clicked_at) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (slug_clean, subtag_clean, referrer, device, raw_user_agent[:250],
             ip_hash, now)
        )
        conn.commit()

    # Dispatch outbound webhook notice if enabled
    send_click_webhook(slug_clean, subtag_clean, device, referrer)

    return redirect(destination_url, code=302)


# --- CSV Export Route ---
@app.route("/stats/export.csv")
def export_csv():
    if request.args.get("key") != ADMIN_SECRET:
        abort(403)

    days = request.args.get("days", type=int)
    query = "SELECT id, slug, subtag, referrer, device_type, user_agent, ip_hash, clicked_at FROM click_events"
    params = ()

    if days:
        query += " WHERE clicked_at >= datetime('now', ?)"
        params = (f"-{days} days",)

    query += " ORDER BY id DESC"

    with get_db() as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "slug", "subtag", "referrer", "device_type", "user_agent",
         "ip_hash", "clicked_at_utc"])

    for row in rows:
        writer.writerow([
            row["id"], row["slug"], row["subtag"] or "", row["referrer"] or "",
            row["device_type"], row["user_agent"], row["ip_hash"],
            row["clicked_at"]
        ])

    csv_data = output.getvalue()
    output.close()

    filename = f"click_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# --- Analytics Dashboard ---
@app.route("/stats")
def stats_dashboard():
    if request.args.get("key") != ADMIN_SECRET:
        abort(403)

    with get_db() as conn:
        # Lifetime totals + unique visitors (distinct IP hashes)
        summary_cursor = conn.execute("""
            SELECT 
                slug, 
                COUNT(*) as total_clicks,
                COUNT(DISTINCT ip_hash) as unique_visitors,
                MAX(clicked_at) as last_click
            FROM click_events
            GROUP BY slug
        """)
        summary_rows = summary_cursor.fetchall()

        # Placement / sub-tag breakdown
        placement_cursor = conn.execute("""
            SELECT 
                slug, 
                COALESCE(subtag, 'direct / root') as placement,
                COUNT(*) as total_clicks,
                COUNT(DISTINCT ip_hash) as unique_visitors
            FROM click_events
            GROUP BY slug, subtag
            ORDER BY total_clicks DESC
        """)
        placement_rows = placement_cursor.fetchall()

        # Device distribution
        device_cursor = conn.execute("""
            SELECT device_type, COUNT(*) as count 
            FROM click_events 
            GROUP BY device_type 
            ORDER BY count DESC
        """)
        device_rows = device_cursor.fetchall()

        # Daily breakdown (past 30 days)
        daily_cursor = conn.execute("""
            SELECT 
                strftime('%Y-%m-%d', clicked_at) AS click_date,
                slug,
                COUNT(*) AS click_count
            FROM click_events
            WHERE clicked_at >= datetime('now', '-30 days')
            GROUP BY click_date, slug
            ORDER BY click_date DESC, slug ASC
        """)
        daily_rows = daily_cursor.fetchall()

        # Recent 25 clicks
        recent_cursor = conn.execute("""
            SELECT slug, subtag, device_type, referrer, clicked_at 
            FROM click_events 
            ORDER BY id DESC LIMIT 25
        """)
        recent_logs = recent_cursor.fetchall()

    if request.headers.get("Accept") == "application/json":
        return jsonify({
            "summary": [dict(r) for r in summary_rows],
            "placements": [dict(r) for r in placement_rows],
            "devices": [dict(r) for r in device_rows],
            "daily_last_30_days": [dict(r) for r in daily_rows],
            "recent": [dict(r) for r in recent_logs]
        })

    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Storefront Click Tracker</title>
      <style>
        body { font-family: system-ui, -apple-system, sans-serif; max-width: 1000px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
        .grid { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 15px; }
        .col { flex: 1; min-width: 300px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
        th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e1e4e8; }
        th { background: #f6f8fa; font-weight: 600; }
        code { background: #f1f3f5; padding: 2px 5px; border-radius: 4px; font-size: 12px; }
        .badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
        .badge-active { background: #e6fcf5; color: #0ca678; }
        .badge-paused { background: #fff5f5; color: #fa5252; }
        .btn { display: inline-block; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: 500; }
      </style>
    </head>
    <body>
      <h2>Storefront Click Analytics</h2>

      <div style="margin: 15px 0;">
        <a href="/stats/export.csv?key={{ request.args.get('key') }}" class="btn" style="background: #228be6; color: #fff;">Download All Clicks (.csv)</a>
        <a href="/stats/export.csv?key={{ request.args.get('key') }}&days=30" class="btn" style="background: #e9ecef; color: #495057; margin-left: 6px;">Last 30 Days (.csv)</a>
      </div>

      <h3>Destination Summary</h3>
      <table>
        <tr>
          <th>Destination</th>
          <th>Status</th>
          <th>Total Clicks</th>
          <th>Unique Visitors</th>
          <th>Unique Ratio</th>
          <th>QR Code</th>
          <th>Last Click (UTC)</th>
        </tr>
        {% for r in summary_rows %}
        <tr>
          <td><code>/{{ r['slug'] }}</code></td>
          <td>
            {% if targets[r['slug']]['active'] %}
              <span class="badge badge-active">Active</span>
            {% else %}
              <span class="badge badge-paused">Paused</span>
            {% endif %}
          </td>
          <td><strong>{{ r['total_clicks'] }}</strong></td>
          <td>{{ r['unique_visitors'] }}</td>
          <td>{{ ((r['unique_visitors'] / r['total_clicks']) * 100) | round(1) }}%</td>
          <td><a href="/{{ r['slug'] }}/qr" target="_blank">View QR</a></td>
          <td>{{ r['last_click'] or 'Never' }}</td>
        </tr>
        {% endfor %}
      </table>

      <div class="grid">
        <div class="col">
          <h3>Campaigns & Placements</h3>
          <table>
            <tr><th>Slug</th><th>Placement / Subtag</th><th>Clicks</th><th>Unique</th><th>QR</th></tr>
            {% for r in placement_rows %}
            <tr>
              <td><code>/{{ r['slug'] }}</code></td>
              <td>{{ r['placement'] }}</td>
              <td><strong>{{ r['total_clicks'] }}</strong></td>
              <td>{{ r['unique_visitors'] }}</td>
              <td>
                {% if r['placement'] != 'direct / root' %}
                  <a href="/{{ r['slug'] }}/{{ r['placement'] }}/qr" target="_blank">QR</a>
                {% else %}
                  <a href="/{{ r['slug'] }}/qr" target="_blank">QR</a>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </table>
        </div>

        <div class="col">
          <h3>Visitor Devices</h3>
          <table>
            <tr><th>Platform</th><th>Click Count</th></tr>
            {% for r in device_rows %}
            <tr><td>{{ r['device_type'] }}</td><td><strong>{{ r['count'] }}</strong></td></tr>
            {% endfor %}
          </table>

          <h3 style="margin-top: 25px;">Daily Activity (30 Days)</h3>
          <table>
            <tr><th>Date</th><th>Slug</th><th>Clicks</th></tr>
            {% for r in daily_rows %}
            <tr><td>{{ r['click_date'] }}</td><td><code>/{{ r['slug'] }}</code></td><td>{{ r['click_count'] }}</td></tr>
            {% else %}
            <tr><td colspan="3" style="color: #888;">No recent clicks.</td></tr>
            {% endfor %}
          </table>
        </div>
      </div>

      <h3 style="margin-top: 35px;">Recent Clicks (Last 25)</h3>
      <table>
        <tr><th>Target</th><th>Placement</th><th>Device</th><th>Referrer</th><th>Timestamp (UTC)</th></tr>
        {% for log in recent_logs %}
        <tr>
          <td><code>/{{ log['slug'] }}</code></td>
          <td>{{ log['subtag'] or 'root' }}</td>
          <td>{{ log['device_type'] }}</td>
          <td>{{ log['referrer'] or 'Direct' }}</td>
          <td>{{ log['clicked_at'] }}</td>
        </tr>
        {% endfor %}
      </table>
    </body>
    </html>
    """
    return render_template_string(
        html,
        summary_rows=summary_rows,
        placement_rows=placement_rows,
        device_rows=device_rows,
        daily_rows=daily_rows,
        recent_logs=recent_logs,
        targets=TARGETS
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
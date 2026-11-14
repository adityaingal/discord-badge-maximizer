"""
Lightweight HTTP Health Check and Status Web Server for Render Deployments.
Binds to 0.0.0.0:$PORT so Render detects an open port and marks the Web Service as Live.
Also includes an automated keep-alive self-pinger to prevent Render Free plan spin-downs.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

SERVER_START_TIME = time.time()
_STATUS_PROVIDER: Optional[Callable[[], Dict[str, Any]]] = None

# The public dashboard only shows counters. The account, guild and channel
# ids are personal data and are only rendered when the request carries the
# secret from the DASHBOARD_KEY environment variable.
DASHBOARD_KEY_ENV = "DASHBOARD_KEY"


def _is_authorized(query: str) -> bool:
    key = os.getenv(DASHBOARD_KEY_ENV, "").strip()
    if not key:
        return False
    params = urllib.parse.parse_qs(query)
    return key in params.get("key", [])


def _redact(data: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the identifying ids from the status data."""
    clean = dict(data)
    streams = []
    for stream in clean.get("active_streams", []) or []:
        streams.append({
            "elapsed_hours": stream.get("elapsed_hours"),
            "duration_hours": stream.get("duration_hours"),
        })
    clean["active_streams"] = streams
    return clean


class RenderHealthCheckHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BadgeMaximizerServer/1.0"

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if not path:
            path = "/"

        if path in ("/", "/health", "/healthz"):
            self._handle_dashboard()
        elif path in ("/status", "/stats", "/api/status", "/api/stats"):
            self._handle_status_json()
        elif path in ("/ping", "/pong"):
            self._handle_ping()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _handle_ping(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(b"pong")

    def _handle_status_json(self) -> None:
        data = _STATUS_PROVIDER() if _STATUS_PROVIDER else {}
        if not _is_authorized(self.path.partition("?")[2]):
            data = _redact(data)
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_dashboard(self) -> None:
        data = _STATUS_PROVIDER() if _STATUS_PROVIDER else {}
        authorized = _is_authorized(self.path.partition("?")[2])
        if not authorized:
            data = _redact(data)
        bot_user = data.get("bot_user", "Connecting...")
        bot_online = data.get("bot_online", False)
        accounts_count = data.get("accounts_count", 0)
        active_streams = data.get("active_streams", [])
        uptime_sec = int(time.time() - SERVER_START_TIME)
        uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"
        port = os.getenv("PORT", "10000")
        render_url = os.getenv("RENDER_EXTERNAL_URL", "")

        stream_rows = ""
        if active_streams:
            for s in active_streams:
                if authorized:
                    identity = (
                        f"<td><code>{str(s.get('snowflake', ''))[:8]}...</code></td>"
                        f"<td>{s.get('guild_id', '-')}</td>"
                        f"<td>{s.get('channel_id', '-')}</td>"
                    )
                else:
                    identity = "<td colspan=\"3\"><code>hidden</code></td>"
                stream_rows += f"""
                <tr class="stream-row">
                    {identity}
                    <td>{s.get('elapsed_hours', 0):.2f}h / {s.get('duration_hours', 0)}h</td>
                    <td><span class="badge badge-live">LIVE BROADCASTING</span></td>
                </tr>
                """
        else:
            stream_rows = "<tr><td colspan='5' class='empty-row'>No active streams. Use <code>/stream24</code> in Discord to start.</td></tr>"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Discord Badge Maximizer · Render Free Service</title>
    <style>
        :root {{
            --bg: #090d16;
            --card-bg: #111827;
            --border: #1f2937;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --accent: #5865F2;
            --accent-glow: rgba(88, 101, 242, 0.25);
            --success: #22c55e;
            --success-glow: rgba(34, 197, 94, 0.25);
            --warning: #f59e0b;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: var(--bg);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            padding: 32px 16px;
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 860px;
            width: 100%;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}
        .header {{
            background: linear-gradient(135deg, #1e1b4b 0%, #111827 100%);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4);
        }}
        .title-group h1 {{
            font-size: 24px;
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .title-group p {{
            color: var(--text-muted);
            font-size: 14px;
            margin-top: 4px;
        }}
        .status-pill {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(34, 197, 94, 0.12);
            color: var(--success);
            border: 1px solid rgba(34, 197, 94, 0.3);
            border-radius: 20px;
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 600;
        }}
        .status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--success);
            box-shadow: 0 0 10px var(--success);
            animation: pulse 2s infinite;
        }}
        @keyframes pulse {{
            0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 var(--success-glow); }}
            70% {{ transform: scale(1); box-shadow: 0 0 0 8px rgba(34, 197, 94, 0); }}
            100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); }}
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}
        .card-label {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
        }}
        .card-val {{
            font-size: 20px;
            font-weight: 700;
            color: #fff;
        }}
        .card-sub {{
            font-size: 12px;
            color: var(--text-muted);
        }}
        .table-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            overflow: hidden;
        }}
        .table-card h2 {{
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 14px;
            color: #fff;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th {{
            text-align: left;
            padding: 10px 12px;
            background: rgba(255,255,255,0.02);
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
            font-weight: 600;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid var(--border);
            color: var(--text-main);
        }}
        .empty-row {{
            text-align: center;
            color: var(--text-muted);
            padding: 24px !important;
        }}
        code {{
            background: rgba(255,255,255,0.06);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
        }}
        .badge {{
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 600;
            display: inline-block;
        }}
        .badge-live {{
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }}
        .info-box {{
            background: rgba(88, 101, 242, 0.08);
            border: 1px solid rgba(88, 101, 242, 0.25);
            border-radius: 12px;
            padding: 16px 20px;
            font-size: 13px;
            line-height: 1.6;
        }}
        .info-box strong {{ color: #a5b4fc; }}
        .info-box a {{ color: #818cf8; text-decoration: none; font-weight: 600; }}
        .info-box a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="title-group">
                <h1>Discord Badge Maximizer</h1>
                <p>Render Free Plan Web Service · Port {port} Bound &amp; Healthy</p>
            </div>
            <div class="status-pill">
                <div class="status-dot"></div>
                <span>SERVICE LIVE</span>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-label">Discord Bot</div>
                <div class="card-val">{"🟢 Online" if bot_online else "🟡 Connecting"}</div>
                <div class="card-sub">{bot_user}</div>
            </div>
            <div class="card">
                <div class="card-label">Connected Accounts</div>
                <div class="card-val">{accounts_count}</div>
                <div class="card-sub">Loaded in session</div>
            </div>
            <div class="card">
                <div class="card-label">Active Broadcasts</div>
                <div class="card-val">{len(active_streams)}</div>
                <div class="card-sub">Streams transmitting</div>
            </div>
            <div class="card">
                <div class="card-label">Instance Uptime</div>
                <div class="card-val">{uptime_str}</div>
                <div class="card-sub">Render service lifetime</div>
            </div>
        </div>

        <div class="table-card">
            <h2>Active Discord Streams</h2>
            <table>
                <thead>
                    <tr>
                        <th>Account</th>
                        <th>Guild ID</th>
                        <th>Channel ID</th>
                        <th>Duration</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {stream_rows}
                </tbody>
            </table>
        </div>

        <div class="info-box">
            <strong>💡 Render Free Plan 24/7 Keep-Alive:</strong><br>
            Render free web services sleep after 15 minutes of inactivity.<br>
            To keep your bot and video streams broadcasting 24/7 without interruption:
            <br>
            Set up a free monitor at <a href="https://uptimerobot.com" target="_blank" rel="noopener">UptimeRobot.com</a> or <a href="https://cron-job.org" target="_blank" rel="noopener">cron-job.org</a> to send an HTTP GET request to:
            <br>
            <code>{"https://" + render_url if render_url and not render_url.startswith("http") else (render_url or "https://<your-render-app>.onrender.com")}/ping</code> every <strong>5 minutes</strong>.
        </div>
    </div>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine health check log noise to keep bot logs readable
        pass


def start_web_server(status_provider: Optional[Callable[[], Dict[str, Any]]] = None) -> Optional[Any]:
    global _STATUS_PROVIDER
    _STATUS_PROVIDER = status_provider

    port_env = os.getenv("PORT", "10000")
    try:
        port = int(port_env)
    except ValueError:
        port = 10000

    class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        server = ThreadedServer(("0.0.0.0", port), RenderHealthCheckHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="RenderHealthWebServer")
        server_thread.start()
        print(f"[OK] Render health check web server listening on 0.0.0.0:{port} (PORT detected)")
        return server
    except Exception as exc:
        print(f"[!] Failed to bind web server to 0.0.0.0:{port}: {exc}")
        return None


def start_keep_alive_pinger() -> None:
    """Background thread that pings RENDER_EXTERNAL_URL every 10 min to prevent Free plan spin-downs."""
    raw_url = os.getenv("RENDER_EXTERNAL_URL", "").strip() or os.getenv("PING_URL", "").strip()
    if not raw_url:
        return

    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        target_url = f"https://{raw_url.rstrip('/')}/ping"
    else:
        target_url = f"{raw_url.rstrip('/')}/ping"

    def _loop() -> None:
        time.sleep(45)  # wait for startup
        print(f"[OK] Render internal keep-alive loop activated for: {target_url}")
        while True:
            try:
                req = urllib.request.Request(
                    target_url,
                    headers={"User-Agent": "Render-KeepAlive/1.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    pass
            except Exception:
                pass
            time.sleep(300)  # every 5 minutes, Render sleeps after 15

    thread = threading.Thread(target=_loop, daemon=True, name="RenderKeepAlive")
    thread.start()

"""
web_server.py  —  Flask веб-сервер для просмотра карты сети в браузере
GET /           -> netmap.html  (интерактивная карта)
GET /api/map    -> JSON: устройства + соединения + метки
GET /api/stats  -> JSON: счётчики online/offline/unknown
GET /api/history/<id> -> JSON: история SNMP метрик
POST /api/login -> авторизация (логин/пароль)
GET /api/logout -> выход
"""
from __future__ import annotations
import os
import threading
import time
import hashlib
import secrets
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import NetworkMapApp

try:
    from flask import Flask, jsonify, Response, request, session, redirect
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# Путь к HTML-файлу рядом с этим скриптом
_HERE      = os.path.dirname(os.path.abspath(__file__))
_HTML_FILE = os.path.join(_HERE, "netmap.html")
_LOGIN_FILE = os.path.join(_HERE, "login.html")

# ── Авторизация ───────────────────────────────────────────────────────────────
def _hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# Дефолтные учётные данные (можно сменить в диалоге Tkinter)
DEFAULT_CREDENTIALS = {
    "admin": _hash_pw("admin")
}

# Активные сессии: token -> {"user": str, "created": float}
_sessions: dict = {}
_SESSION_TTL = 8 * 3600  # 8 часов

# ── Singleton ────────────────────────────────────────────────────────────────
_server_instance: Optional["NetMapWebServer"] = None


def get_or_create(app, host="0.0.0.0", port=5050):
    global _server_instance
    if _server_instance and _server_instance.running:
        return _server_instance
    _server_instance = NetMapWebServer(app, host, port)
    return _server_instance


def stop_server():
    global _server_instance
    if _server_instance:
        _server_instance.stop()
        _server_instance = None


# ── Сервер ───────────────────────────────────────────────────────────────────
class NetMapWebServer:
    def __init__(self, app, host, port):
        self.app         = app
        self.host        = host
        self.port        = port
        self.running     = False
        self._thread     = None
        self._flask      = None
        # Пользователи с ролями: {username: {password: hash, role: "admin"|"viewer"}}
        self.users = {
            "admin": {"password": _hash_pw("admin"), "role": "admin"}
        }
        self.credentials  = {"admin": _hash_pw("admin")}
        self.auth_enabled = True
        self._sse_clients: set = set()   # активные SSE соединения

    def set_credentials(self, username: str, password: str, role: str = "admin"):
        self.users[username] = {"password": _hash_pw(password), "role": role}
        self.credentials = {u: v["password"] for u, v in self.users.items()}

    def _check_auth(self) -> bool:
        if not self.auth_enabled:
            return True
        token = request.cookies.get("nm_token", "")
        sess  = _sessions.get(token)
        if not sess:
            return False
        if time.time() - sess["created"] > _SESSION_TTL:
            _sessions.pop(token, None)
            return False
        return True

    def _is_admin(self) -> bool:
        token = request.cookies.get("nm_token", "")
        sess  = _sessions.get(token, {})
        user  = sess.get("user", "")
        return self.users.get(user, {}).get("role") == "admin"

    def _get_role(self) -> str:
        token = request.cookies.get("nm_token", "")
        sess  = _sessions.get(token, {})
        user  = sess.get("user", "")
        return self.users.get(user, {}).get("role", "viewer")

    def start(self):
        if not FLASK_OK:
            return False, "Flask не установлен: pip install flask"
        if self.running:
            return True, f"http://localhost:{self.port}"
        self._flask = self._build_app()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="FlaskWebServer")
        self.running = True
        self._thread.start()
        return True, f"http://localhost:{self.port}"

    def stop(self):
        self.running = False

    def _run(self):
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._flask.run(host=self.host, port=self.port,
                        debug=False, use_reloader=False, threaded=True)

    # ── Flask routes ──────────────────────────────────────────────────────────
    def _build_app(self):
        flask_app = Flask(__name__)
        flask_app.json.ensure_ascii = False
        flask_app.secret_key = secrets.token_hex(32)
        srv = self

        def _login_page():
            """Страница входа."""
            return Response(LOGIN_HTML, mimetype="text/html; charset=utf-8")

        def _need_auth():
            """Возвращает редирект на /login или 401 для API."""
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")

        @flask_app.route("/login", methods=["GET"])
        def login_page():
            return _login_page()

        @flask_app.route("/api/login", methods=["POST"])
        def api_login():
            data = request.get_json(silent=True) or {}
            username = data.get("username", "").strip()
            password = data.get("password", "")
            user_data = srv.users.get(username)
            if user_data and user_data["password"] == _hash_pw(password):
                token = secrets.token_hex(24)
                _sessions[token] = {
                    "user": username,
                    "role": user_data.get("role", "viewer"),
                    "created": time.time()
                }
                resp = jsonify({"ok": True, "role": user_data.get("role", "viewer")})
                resp.set_cookie("nm_token", token, httponly=True,
                                max_age=_SESSION_TTL, samesite="Lax")
                return resp
            return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401

        @flask_app.route("/api/logout")
        def api_logout():
            token = request.cookies.get("nm_token", "")
            _sessions.pop(token, None)
            resp = redirect("/login")
            resp.delete_cookie("nm_token")
            return resp

        @flask_app.route("/")
        def index():
            if not srv._check_auth():
                return _need_auth()
            try:
                with open(_HTML_FILE, "r", encoding="utf-8") as f:
                    html = f.read()
            except FileNotFoundError:
                return Response(
                    "<h2>Файл netmap.html не найден рядом с web_server.py</h2>",
                    status=500, mimetype="text/html"
                )
            return Response(html, mimetype="text/html; charset=utf-8")

        @flask_app.route("/api/tabs")
        def api_tabs():
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._tabs_data())

        @flask_app.route("/api/map")
        def api_map():
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._map_data(None))

        @flask_app.route("/api/map/<tab_name>")
        def api_map_tab(tab_name):
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._map_data(tab_name))

        @flask_app.route("/api/stats")
        def api_stats():
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._stats(None))

        @flask_app.route("/api/stats/<tab_name>")
        def api_stats_tab(tab_name):
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._stats(tab_name))

        @flask_app.route("/api/history/<dev_id>")
        def api_history(dev_id):
            if not srv._check_auth(): return _need_auth()
            return jsonify(srv._history(dev_id))

        @flask_app.route("/api/db/purge", methods=["POST"])
        def api_purge():
            if not srv._check_auth(): return _need_auth()
            try:
                import reporter
                data = request.get_json(silent=True) or {}
                days = int(data.get("days", 90))
                days = max(1, min(days, 365))
                deleted = reporter.history_db.purge_old_records(days)
                size_mb = reporter.history_db.get_db_size_mb()
                return jsonify({"ok": True, "deleted": deleted, "size_mb": round(size_mb, 2)})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)})

        @flask_app.route("/api/db/stats")
        def api_db_stats():
            if not srv._check_auth(): return _need_auth()
            try:
                import reporter
                size_mb = reporter.history_db.get_db_size_mb()
                return jsonify({"ok": True, "size_mb": round(size_mb, 2)})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)})

        # ── SSE: мгновенные обновления без polling ────────────────────────────
        @flask_app.route("/api/stream")
        def api_stream():
            if not srv._check_auth():
                return Response("data: {\"error\":\"unauthorized\"}\n\n",
                                status=401, mimetype="text/event-stream")

            def generate():
                last_sent = {}   # dev_id -> (status, latency_bucket)
                client_id = id(generate)
                srv._sse_clients.add(client_id)
                try:
                    # Первое сообщение — полный снимок карты
                    import json as _json
                    full  = srv._map_data(None)
                    stats = srv._stats(None)
                    yield f"event: init\ndata: {_json.dumps({'map': full, 'stats': stats}, ensure_ascii=False)}\n\n"

                    while srv.running:
                        time.sleep(1)
                        tab = srv.app.current_tab
                        if not tab:
                            yield "event: ping\ndata: {}\n\n"
                            continue

                        # Отправляем только изменения статусов
                        changes = []
                        for dev_id, dev in tab.devices.items():
                            lat_bucket = int((dev.latency or 0) / 10)
                            key = (dev.status.value, lat_bucket,
                                   str(getattr(dev, "snmp_last_info", None)))
                            if last_sent.get(dev_id) != key:
                                last_sent[dev_id] = key
                                snmp = getattr(dev, "snmp_last_info", None) or {}
                                changes.append({
                                    "id":           dev_id,
                                    "status":       dev.status.value,
                                    "latency":      dev.latency,
                                    "last_checked": dev.last_checked,
                                    "snmp_info":    snmp,
                                })

                        if changes:
                            payload = _json.dumps({"changes": changes}, ensure_ascii=False)
                            yield f"event: update\ndata: {payload}\n\n"
                        else:
                            # keepalive каждую секунду
                            yield "event: ping\ndata: {}\n\n"

                except GeneratorExit:
                    pass
                finally:
                    srv._sse_clients.discard(client_id)

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection":        "keep-alive",
                }
            )

        # ── Лог событий ───────────────────────────────────────────────────────
        @flask_app.route("/api/events")
        def api_events():
            if not srv._check_auth(): return _need_auth()
            try:
                import reporter
                limit  = int(request.args.get("limit", 100))
                etype  = request.args.get("type")
                events = reporter.history_db.get_events(limit=limit, event_type=etype or None)
                return jsonify({"ok": True, "events": events})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e), "events": []})

        # ── Backup / Restore ──────────────────────────────────────────────────
        @flask_app.route("/api/backup")
        def api_backup():
            if not srv._check_auth(): return _need_auth()
            try:
                import zipfile, io
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    # Все файлы карт
                    for name, tab in srv.app.tabs.items():
                        if tab.file_path and os.path.exists(tab.file_path):
                            zf.write(tab.file_path, os.path.basename(tab.file_path))
                    # session.json
                    sess_path = os.path.join(_HERE, "session.json")
                    if os.path.exists(sess_path):
                        zf.write(sess_path, "session.json")
                    # SQLite история
                    db_path = os.path.join(_HERE, "snmp_history.db")
                    if os.path.exists(db_path):
                        zf.write(db_path, "snmp_history.db")
                buf.seek(0)
                ts = time.strftime("%Y%m%d_%H%M%S")
                return Response(
                    buf.read(),
                    mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=netmap_backup_{ts}.zip"}
                )
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)})

        # ── Статистика для боковой панели ────────────────────────────────────
        @flask_app.route("/api/summary")
        def api_summary():
            if not srv._check_auth(): return _need_auth()
            try:
                tab = srv.app.current_tab
                if not tab:
                    return jsonify({"ok": True, "data": {}})
                from device import DeviceStatus
                devs = list(tab.devices.values())
                total_in_b  = 0.0
                total_out_b = 0.0
                latencies   = []
                for d in devs:
                    if d.latency is not None:
                        latencies.append(d.latency)
                    snmp = getattr(d, "snmp_last_info", None) or {}
                    for k, v in snmp.items():
                        lo = k.lower()
                        try:
                            n = float(str(v).split()[0])
                            if "inoctets" in lo:  total_in_b  += n
                            if "outoctets" in lo: total_out_b += n
                        except (ValueError, TypeError):
                            pass
                def fmt_bytes(b):
                    if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
                    if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
                    if b >= 1024:          return f"{b/1024:.1f} KB"
                    return f"{b:.0f} B"
                return jsonify({"ok": True, "data": {
                    "total_in":    fmt_bytes(total_in_b),
                    "total_out":   fmt_bytes(total_out_b),
                    "avg_latency": round(sum(latencies)/len(latencies), 1) if latencies else None,
                    "max_latency": round(max(latencies), 1) if latencies else None,
                    "devices_total":  len(devs),
                    "devices_online": sum(1 for d in devs if d.status == DeviceStatus.ONLINE),
                }})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)})

        # ── Пользователи (роли admin/viewer) ────────────────────────────────
        @flask_app.route("/api/users", methods=["GET"])
        def api_users():
            if not srv._check_auth(): return _need_auth()
            token = request.cookies.get("nm_token", "")
            sess  = _sessions.get(token, {})
            role  = srv.users.get(sess.get("user", ""), {}).get("role", "viewer")
            users = [{"username": u, "role": v["role"]}
                     for u, v in srv.users.items()]
            return jsonify({"ok": True, "users": users, "my_role": role})

        @flask_app.route("/api/users", methods=["POST"])
        def api_users_add():
            if not srv._check_auth(): return _need_auth()
            if not srv._is_admin(): return jsonify({"ok": False, "error": "Нет прав"}), 403
            data = request.get_json(silent=True) or {}
            uname = data.get("username", "").strip()
            pw    = data.get("password", "")
            role  = data.get("role", "viewer")
            if not uname or len(pw) < 4:
                return jsonify({"ok": False, "error": "Неверные данные"}), 400
            srv.users[uname] = {"password": _hash_pw(pw), "role": role}
            srv.credentials  = {u: v["password"] for u, v in srv.users.items()}
            return jsonify({"ok": True})

        @flask_app.route("/api/users/<uname>", methods=["DELETE"])
        def api_users_del(uname):
            if not srv._check_auth(): return _need_auth()
            if not srv._is_admin(): return jsonify({"ok": False, "error": "Нет прав"}), 403
            srv.users.pop(uname, None)
            srv.credentials = {u: v["password"] for u, v in srv.users.items()}
            return jsonify({"ok": True})

        return flask_app

    # ── Data helpers ──────────────────────────────────────────────────────────
    def _tabs_data(self):
        """Список всех карт с суммарной статистикой."""
        from device import DeviceStatus
        result = []
        for name, tab in self.app.tabs.items():
            devs = list(tab.devices.values())
            result.append({
                "name":    name,
                "active":  (name == self.app.current_tab_name),
                "total":   len(devs),
                "online":  sum(1 for d in devs if d.status == DeviceStatus.ONLINE),
                "offline": sum(1 for d in devs if d.status == DeviceStatus.OFFLINE),
            })
        return result

    def _get_tab(self, tab_name):
        """Возвращает нужную вкладку или текущую если tab_name=None."""
        if tab_name and tab_name in self.app.tabs:
            return self.app.tabs[tab_name]
        return self.app.current_tab

    def _map_data(self, tab_name):
        tab = self._get_tab(tab_name)
        if not tab:
            return {"devices": [], "connections": [], "labels": [], "tab": ""}

        devices = []
        for dev_id, dev in tab.devices.items():
            devices.append({
                "id":           dev_id,
                "name":         dev.name,
                "ip":           dev.ip,
                "dtype":        dev.dtype,
                "x":            float(dev.x),
                "y":            float(dev.y),
                "status":       dev.status.value,
                "latency":      dev.latency,
                "last_checked": dev.last_checked,
                "location":     getattr(dev, "location", ""),
                "snmp_info":    getattr(dev, "snmp_last_info", None) or {},
            })

        connections = [{"from": a, "to": b} for a, b in tab.connections]

        labels = []
        for (id1, id2), slots in self.app.conn_labels._labels.items():
            for s in slots:
                labels.append({
                    "from":  id1, "to": id2,
                    "label": s.get("oid_label", ""),
                    "value": s.get("_last_val"),
                    "unit":  s.get("unit", ""),
                })

        return {
            "tab":         tab.name,
            "devices":     devices,
            "connections": connections,
            "labels":      labels,
        }

    def _history(self, dev_id):
        """История SNMP метрик. Принимает ?period=5m|10m|1h|1d|1mo или ?start_ts=unix."""
        try:
            from flask import request
            import reporter

            # Определяем временной диапазон по периоду
            period = request.args.get('period', '1h')
            now    = time.time()
            period_map = {
                '5m':  5   * 60,
                '10m': 10  * 60,
                '1h':  3600,
                '1d':  86400,
                '1mo': 86400 * 30,
            }
            seconds   = period_map.get(period, 3600)
            start_ts  = float(request.args.get('start_ts', now - seconds))
            # Лимит точек зависит от периода — больше период, больше точек
            limit_map = {'5m': 100, '10m': 120, '1h': 200, '1d': 500, '1mo': 500}
            limit = int(request.args.get('limit', limit_map.get(period, 200)))
            limit = max(10, min(limit, 1000))

            names = reporter.history_db.get_all_metric_names(dev_id)
            result = {}
            for name in names:
                rows = reporter.history_db.get_metrics_for_device(
                    dev_id, metric_name=name, start_ts=start_ts, limit=limit
                )
                pts = []
                for r in rows:
                    ts, mn, raw, num, unit = r
                    if num is not None:
                        pts.append({"ts": ts, "v": num, "unit": unit or ""})
                if pts:
                    pts.sort(key=lambda x: x["ts"])
                    result[name] = pts
            return {"ok": True, "metrics": result, "period": period}
        except Exception as e:
            return {"ok": False, "error": str(e), "metrics": {}}

    def _stats(self, tab_name):
        tab = self._get_tab(tab_name)
        if not tab:
            return {"total": 0, "online": 0, "offline": 0,
                    "unknown": 0, "updated": "", "role": "viewer"}
        from device import DeviceStatus
        devs = list(tab.devices.values())
        return {
            "tab":     tab.name,
            "total":   len(devs),
            "online":  sum(1 for d in devs if d.status == DeviceStatus.ONLINE),
            "offline": sum(1 for d in devs if d.status == DeviceStatus.OFFLINE),
            "unknown": sum(1 for d in devs if d.status not in
                          (DeviceStatus.ONLINE, DeviceStatus.OFFLINE)),
            "updated": time.strftime("%H:%M:%S"),
            "role":    self._get_role(),
        }



# ════════════════════════════════════════════════════════════════════════════
#  Страница входа (встроена прямо в Python для простоты)
# ════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetMap — Вход</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#fff;font-family:Consolas,monospace;
  display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#23233a;border:1px solid #444466;border-radius:12px;
  padding:36px 40px;width:340px;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
h1{color:#ffb86c;font-size:20px;margin-bottom:6px;text-align:center}
.sub{color:#666;font-size:12px;text-align:center;margin-bottom:28px}
label{display:block;font-size:12px;color:#b0b0d0;margin-bottom:5px}
input{width:100%;background:#2d2d44;border:1px solid #444466;border-radius:6px;
  color:#fff;padding:9px 12px;font-family:inherit;font-size:13px;outline:none;
  transition:border 0.2s;margin-bottom:16px}
input:focus{border-color:#ffb86c}
button{width:100%;background:#ffb86c;border:none;border-radius:6px;
  color:#1a1a2e;padding:10px;font-size:14px;font-weight:bold;
  font-family:inherit;cursor:pointer;transition:background 0.2s}
button:hover{background:#ffd08c}
#err{color:#ff5555;font-size:12px;text-align:center;margin-top:12px;
  min-height:18px}
</style>
</head>
<body>
<div class="card">
  <h1>&#9783; NetMap</h1>
  <div class="sub">Network Monitor — Вход</div>
  <label>Логин</label>
  <input id="u" type="text" placeholder="admin" autocomplete="username">
  <label>Пароль</label>
  <input id="p" type="password" placeholder="••••••" autocomplete="current-password">
  <button id="btn">Войти</button>
  <div id="err"></div>
</div>
<script>
function doLogin() {
  var u = document.getElementById('u').value.trim();
  var p = document.getElementById('p').value;
  document.getElementById('err').textContent = '';
  fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username: u, password: p})
  }).then(function(r){ return r.json(); })
    .then(function(d){
      if (d.ok) { window.location.href = '/'; }
      else { document.getElementById('err').textContent = d.error || 'Ошибка'; }
    }).catch(function(){ document.getElementById('err').textContent = 'Ошибка соединения'; });
}
document.getElementById('btn').addEventListener('click', doLogin);
document.getElementById('p').addEventListener('keydown', function(e){
  if (e.key === 'Enter') doLogin();
});
document.getElementById('u').addEventListener('keydown', function(e){
  if (e.key === 'Enter') document.getElementById('p').focus();
});
// Автофокус
window.addEventListener('load', function(){
  document.getElementById('u').focus();
});
</script>
</body>
</html>
"""

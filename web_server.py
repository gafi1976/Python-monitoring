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
        # Учётные данные: {username: hashed_password}
        self.credentials = dict(DEFAULT_CREDENTIALS)
        # Можно задать через Tkinter-диалог
        self.auth_enabled = True

    def set_credentials(self, username: str, password: str):
        self.credentials = {username: _hash_pw(password)}

    def _check_auth(self) -> bool:
        """Проверяет токен сессии из cookie."""
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
            stored   = srv.credentials.get(username)
            if stored and stored == _hash_pw(password):
                token = secrets.token_hex(24)
                _sessions[token] = {"user": username, "created": time.time()}
                resp = jsonify({"ok": True})
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
                    "unknown": 0, "updated": ""}
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

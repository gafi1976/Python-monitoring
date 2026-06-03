"""
web_server.py  —  Flask веб-сервер для просмотра карты сети в браузере
GET /           -> netmap.html  (интерактивная карта)
GET /api/map    -> JSON: устройства + соединения + метки
GET /api/stats  -> JSON: счётчики online/offline/unknown
"""
from __future__ import annotations
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import NetworkMapApp

try:
    from flask import Flask, jsonify, Response
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# Путь к HTML-файлу рядом с этим скриптом
_HERE     = os.path.dirname(os.path.abspath(__file__))
_HTML_FILE = os.path.join(_HERE, "netmap.html")

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
        self.app     = app
        self.host    = host
        self.port    = port
        self.running = False
        self._thread = None
        self._flask  = None

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
        srv = self

        @flask_app.route("/")
        def index():
            # Читаем netmap.html с диска при каждом запросе
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
            return jsonify(srv._tabs_data())

        @flask_app.route("/api/map")
        def api_map():
            return jsonify(srv._map_data(None))

        @flask_app.route("/api/map/<tab_name>")
        def api_map_tab(tab_name):
            return jsonify(srv._map_data(tab_name))

        @flask_app.route("/api/stats")
        def api_stats():
            return jsonify(srv._stats(None))

        @flask_app.route("/api/stats/<tab_name>")
        def api_stats_tab(tab_name):
            return jsonify(srv._stats(tab_name))

        @flask_app.route("/api/history/<dev_id>")
        def api_history(dev_id):
            return jsonify(srv._history(dev_id))

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
        """История SNMP метрик для графика. Возвращает все числовые метрики."""
        try:
            import reporter
            # Получаем все имена метрик устройства
            names = reporter.history_db.get_all_metric_names(dev_id)
            result = {}
            for name in names:
                rows = reporter.history_db.get_metrics_for_device(
                    dev_id, metric_name=name, limit=60
                )
                # Берём только числовые значения, сортируем по времени
                pts = []
                for r in rows:
                    ts, mn, raw, num, unit = r
                    if num is not None:
                        pts.append({"ts": ts, "v": num, "unit": unit or ""})
                if pts:
                    pts.sort(key=lambda x: x["ts"])
                    result[name] = pts
            return {"ok": True, "metrics": result}
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

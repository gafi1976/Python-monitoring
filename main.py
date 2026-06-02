#!/usr/bin/env python3
"""
Network Map — Zabbix-style monitor with multiple map tabs,
SNMP triggers, factor (coefficient) for OID values,
beautiful Telegram messages (MarkdownV2), and history/Excel reports.
"""
import tkinter as tk
from tkinter import messagebox, simpledialog
import threading
import json
import os
import time
import subprocess
import platform
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Tuple
import queue
import concurrent.futures
import urllib.request as _urllib
import re
import copy
from collections import deque
from tkinter import filedialog
from snmp_lld import LLDDialog
from connection_label import ConnectionLabelManager, ConnectionLabelDialog
    

# ── pysnmp 7.x asyncio API ───────────────────────────────────────────────────
try:
    from pysnmp.hlapi.v3arch.asyncio import (
        get_cmd, SnmpEngine, CommunityData, UdpTransportTarget,
        ContextData, ObjectType, ObjectIdentity
    )
    SNMP_AVAILABLE = True
except Exception as _e:
    SNMP_AVAILABLE = False
    print(f"[SNMP] недоступен: {_e}")

from device_settings import DeviceSettingsDialog
from scan_dialog import ScanDialog
from device import Device, DeviceStatus

# Импорт модуля для отчётов и истории
import reporter

# ── Palette (улучшенная видимость) ──────────────────────────────────────────
COLORS = {
    "bg":          "#1a1a2e", "bg2":         "#23233a", "bg3":         "#2d2d44",
    "border":      "#444466", "text":        "#ffffff", "text_dim":    "#b0b0d0",
    "accent":      "#ffb86c", "accent2":     "#50fa7b", "danger":      "#ff5555",
    "warning":     "#f1fa8c", "online":      "#50fa7b", "offline":     "#ff5555",
    "unknown":     "#8be9fd", "checking":    "#f1fa8c", "canvas_bg":   "#1a1a2e",
    "grid":        "#2d2d44", "connection":  "#6272a4", "conn_active": "#50fa7b",
    "selection":   "#444466", "toolbar":     "#23233a", "status_bar":  "#1a1a2e",
    "tab_active":  "#2d2d44", "tab_inactive":"#23233a",
}

DEVICE_ICONS = {
    "router": "🌐", "switch": "🔀", "server": "🖥️", "pc": "💻",
    "printer": "🖨️", "camera": "📷", "phone": "📞", "ups": "🔋", "other": "📡",
}

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "network_data.json")


# ═════════════════════════════════════════════════════════════════════════════
#  SNMP helper
# ═════════════════════════════════════════════════════════════════════════════

def snmp_get_sync(ip: str, community: str, port: int, version: str,
                  oid_str: str, timeout: float = 3.0) -> tuple[Optional[str], Optional[str]]:
    if not SNMP_AVAILABLE:
        return None, "pysnmp не установлен"

    async def _get():
        engine = SnmpEngine()
        try:
            errInd, errSt, errIdx, varBinds = await get_cmd(
                engine,
                CommunityData(community, mpModel=0 if version == "1" else 1),
                await UdpTransportTarget.create((ip, port), timeout=timeout, retries=0),
                ContextData(),
                ObjectType(ObjectIdentity(oid_str)),
            )
            if errInd:
                return None, str(errInd)
            if errSt:
                return None, f"{errSt.prettyPrint()} at {errIdx and varBinds[int(errIdx)-1][0] or '?'}"
            for vb in varBinds:
                return str(vb[1]), None
            return None, "нет данных"
        finally:
            engine.close_dispatcher()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_get())
    except Exception as exc:
        return None, str(exc)
    finally:
        loop.close()


def format_uptime(val_str: str) -> str:
    digits = re.findall(r'\d+', val_str)
    if not digits:
        return val_str
    try:
        ticks = int(digits[0])
        s = ticks // 100
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        parts = []
        if d: parts.append(f"{d}д")
        if h: parts.append(f"{h}ч")
        if m: parts.append(f"{m}м")
        parts.append(f"{s}с")
        return " ".join(parts)
    except Exception:
        return val_str


# ═════════════════════════════════════════════════════════════════════════════
#  Telegram helpers (MarkdownV2 с экранированием)
# ═════════════════════════════════════════════════════════════════════════════

def escape_markdownv2(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!\\'
    escaped = []
    for ch in str(text):
        if ch in special_chars:
            escaped.append('\\' + ch)
        else:
            escaped.append(ch)
    return ''.join(escaped)

def send_telegram_markdown(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "token/chat_id не заданы"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True
    }
    try:
        req = _urllib.Request(url, data=json.dumps(payload).encode("utf-8"),
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                return True, ""
            return False, result.get("description", "unknown error")
    except Exception as e:
        return False, str(e)


# ═════════════════════════════════════════════════════════════════════════════
#  MapData — состояние одной карты
# ═════════════════════════════════════════════════════════════════════════════

class MapData:
    def __init__(self, name: str, file_path: Optional[str] = None):
        self.name = name
        self.file_path = file_path
        self.devices: Dict[str, Device] = {}
        self.connections: List[Tuple[str, str]] = []
        self.canvas_offset = [0, 0]
        self._prev_stable: Dict[str, Optional[DeviceStatus]] = {}
        self._last_checked_ts: Dict[str, float] = {}
        self._snmp_alert_ts: Dict[str, float] = {}
        self.undo_stack: deque = deque(maxlen=50)

    def snapshot(self) -> dict:
        return {
            "devices": copy.deepcopy({k: v.to_dict() for k, v in self.devices.items()}),
            "connections": copy.deepcopy(self.connections),
        }

    def restore_snapshot(self, state: dict):
        self.devices.clear()
        for k, v in state["devices"].items():
            self.devices[k] = Device.from_dict(v)
        self.connections = [tuple(c) for c in state["connections"]]
        self._prev_stable.clear()
        self._last_checked_ts.clear()
        self._snmp_alert_ts.clear()

    def to_dict(self) -> dict:
        return {
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
            "connections": self.connections,
            "canvas_offset": self.canvas_offset,
        }

    def from_dict(self, data: dict):
        self.devices.clear()
        for k, v in data.get("devices", {}).items():
            self.devices[k] = Device.from_dict(v)
        self.connections = [tuple(c) for c in data.get("connections", [])]
        self.canvas_offset = list(data.get("canvas_offset", [0, 0]))


# ═════════════════════════════════════════════════════════════════════════════
#  Main App with tabs
# ═════════════════════════════════════════════════════════════════════════════

class NetworkMapApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Network Map — Zabbix Style Monitor")
        self.root.geometry("1340x800")
        self.root.configure(bg=COLORS["bg"])
        self.root.minsize(900, 600)

        # Общие настройки Telegram
        self.tg_token = ""
        self.tg_chat_id = ""
        self.alert_on_offline = tk.BooleanVar(value=True)
        self.alert_on_online = tk.BooleanVar(value=True)

        # Управление вкладками
        self.tabs: Dict[str, MapData] = {}
        self.current_tab_name: Optional[str] = None

        # Мониторинг
        self.monitoring_active = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.result_queue = queue.Queue()
        self.conn_labels = ConnectionLabelManager()

        # UI элементы
        self._build_ui()
        self._start_queue_processor()

        # Инициализация БД истории (создаст таблицу)
        reporter.history_db

        # Стартовая карта
        self._init_first_tab()

        self._draw_all()
        self.root.bind('<Control-z>', self._undo)
        self.root.bind('<Control-Z>', self._undo)

    def _open_lld_for(self, dev_id: str):
        tab = self.current_tab
        if not tab or dev_id not in tab.devices:
            return
        dev = tab.devices[dev_id]
        LLDDialog(self.root, dev, COLORS)
        # После закрытия — OID уже в dev.snmp_oids
        self._draw_all()

    # ──────────────────────────────────────────────────────────────────────────
    #  Управление вкладками
    # ──────────────────────────────────────────────────────────────────────────

    def _init_first_tab(self):
        if os.path.exists(DATA_PATH):
            try:
                with open(DATA_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                map_data = MapData("Основная", DATA_PATH)
                map_data.from_dict(data)
                self.tg_token = data.get("telegram_token", "")
                self.tg_chat_id = data.get("telegram_chat_id", "")
                self.alert_on_offline.set(data.get("alert_on_offline", True))
                self.alert_on_online.set(data.get("alert_on_online", True))
                self.conn_labels.from_dict(data.get("conn_labels", {}))
                self._update_tg_dot()
                self.tabs["Основная"] = map_data
                self.current_tab_name = "Основная"
                self._refresh_tab_bar()
                self._update_title()
                return
            except Exception:
                pass
        self._new_map()

    def _new_map(self, name: Optional[str] = None):
        if name is None:
            base = "Новая карта"
            name = base
            i = 1
            while name in self.tabs:
                i += 1
                name = f"{base} {i}"
        map_data = MapData(name)
        self.tabs[name] = map_data
        self.current_tab_name = name
        self._refresh_tab_bar()
        self._select_tab(name)
        self._update_title()
        self._set_status(f"Создана карта '{name}'")

    def _open_map(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Открыть карту",
            filetypes=[("NetMap файлы", "*.json"), ("Все файлы", "*.*")],
            initialdir=os.path.dirname(os.path.abspath(__file__))
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            tab_name = os.path.splitext(os.path.basename(path))[0]
            original_name = tab_name
            i = 1
            while tab_name in self.tabs:
                tab_name = f"{original_name} ({i})"
                i += 1
            map_data = MapData(tab_name, path)
            map_data.from_dict(data)
            self.tabs[tab_name] = map_data
            self.current_tab_name = tab_name
            self._refresh_tab_bar()
            self._select_tab(tab_name)
            self._update_title()
            self._set_status(f"Открыта карта '{tab_name}' ({len(map_data.devices)} устройств)")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл:\n{e}", parent=self.root)

    def _save_current_map(self):
        if self.current_tab_name is None:
            return
        tab = self.tabs[self.current_tab_name]
        if tab.file_path:
            self._save_map_to_path(tab, tab.file_path)
        else:
            self._save_current_map_as()

    def _save_current_map_as(self):
        if self.current_tab_name is None:
            return
        tab = self.tabs[self.current_tab_name]
        def_path = tab.file_path if tab.file_path else "network_map.json"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить карту как",
            defaultextension=".json",
            filetypes=[("NetMap файлы", "*.json"), ("Все файлы", "*.*")],
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            initialfile=os.path.basename(def_path)
        )
        if not path:
            return
        tab.file_path = path
        self._save_map_to_path(tab, path)
        new_name = os.path.splitext(os.path.basename(path))[0]
        if new_name != tab.name and new_name not in self.tabs:
            old_name = tab.name
            self.tabs[new_name] = self.tabs.pop(old_name)
            self.tabs[new_name].name = new_name
            self.current_tab_name = new_name
            self._refresh_tab_bar()
        self._update_title()

    def _save_map_to_path(self, tab: MapData, path: str):
        data = tab.to_dict()
        data["telegram_token"] = self.tg_token
        data["telegram_chat_id"] = self.tg_chat_id
        data["alert_on_offline"] = self.alert_on_offline.get()
        data["alert_on_online"] = self.alert_on_online.get()
        data["conn_labels"] = self.conn_labels.to_dict()  # ← ДОБАВИТЬ
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._set_status(f"Сохранено: {os.path.basename(path)}")

    def _close_tab(self, tab_name: str):
        if len(self.tabs) == 1:
            if not messagebox.askyesno("Закрыть вкладку",
                                       "Это последняя карта. Закрыть её? Будет создана новая пустая карта.",
                                       parent=self.root):
                return
        del self.tabs[tab_name]
        if self.current_tab_name == tab_name:
            if self.tabs:
                self.current_tab_name = next(iter(self.tabs.keys()))
            else:
                self._new_map()
        self._refresh_tab_bar()
        self._select_tab(self.current_tab_name)
        self._update_title()

    def _rename_tab(self, tab_name: str):
        new_name = simpledialog.askstring("Переименовать", "Новое имя вкладки:",
                                          initialvalue=tab_name, parent=self.root)
        if not new_name or new_name == tab_name:
            return
        if new_name in self.tabs:
            messagebox.showerror("Ошибка", "Имя уже существует", parent=self.root)
            return
        tab = self.tabs.pop(tab_name)
        tab.name = new_name
        self.tabs[new_name] = tab
        if self.current_tab_name == tab_name:
            self.current_tab_name = new_name
        self._refresh_tab_bar()
        self._update_title()

    def _refresh_tab_bar(self):
        for widget in self.tab_bar.winfo_children():
            widget.destroy()
        for name, tab in self.tabs.items():
            btn_frame = tk.Frame(self.tab_bar, bg=COLORS["tab_active" if name == self.current_tab_name else "tab_inactive"])
            btn_frame.pack(side="left", padx=1, pady=2)
            label = tk.Label(btn_frame, text=name, bg=btn_frame["bg"], fg=COLORS["text"],
                             font=("Consolas", 9))
            label.pack(side="left", padx=(8, 4), pady=4)
            label.bind("<Button-1>", lambda e, n=name: self._select_tab(n))
            label.bind("<Double-Button-1>", lambda e, n=name: self._rename_tab(n))
            close_btn = tk.Label(btn_frame, text="✕", bg=btn_frame["bg"], fg=COLORS["text_dim"],
                                 font=("Consolas", 8), cursor="hand2")
            close_btn.pack(side="left", padx=(0, 6), pady=4)
            close_btn.bind("<Button-1>", lambda e, n=name: self._close_tab(n))
        plus_btn = tk.Label(self.tab_bar, text="+", bg=COLORS["bg3"], fg=COLORS["accent"],
                            font=("Consolas", 12, "bold"), cursor="hand2")
        plus_btn.pack(side="left", padx=8, pady=2)
        plus_btn.bind("<Button-1>", lambda e: self._new_map())

    def _select_tab(self, name: str):
        if name == self.current_tab_name:
            return
        was_monitoring = self.monitoring_active
        if was_monitoring:
            self._toggle_monitoring()
        self.current_tab_name = name
        self._refresh_tab_bar()
        self._draw_all()
        if was_monitoring:
            self._toggle_monitoring()
        self._update_title()
        self._set_status(f"Переключено на карту '{name}'")

    def _update_title(self):
        if self.current_tab_name:
            tab = self.tabs[self.current_tab_name]
            if tab.file_path:
                title = f"Network Map — {tab.name} ({os.path.basename(tab.file_path)})"
            else:
                title = f"Network Map — {tab.name} [не сохранено]"
        else:
            title = "Network Map"
        self.root.title(title)

    @property
    def current_tab(self) -> Optional[MapData]:
        if self.current_tab_name:
            return self.tabs.get(self.current_tab_name)
        return None

    @property
    def devices(self) -> Dict[str, Device]:
        tab = self.current_tab
        return tab.devices if tab else {}

    @property
    def connections(self) -> List[Tuple[str, str]]:
        tab = self.current_tab
        return tab.connections if tab else []

    @property
    def canvas_offset(self) -> List[int]:
        tab = self.current_tab
        return tab.canvas_offset if tab else [0, 0]

    @canvas_offset.setter
    def canvas_offset(self, val):
        if self.current_tab:
            self.current_tab.canvas_offset = val

    # ──────────────────────────────────────────────────────────────────────────
    #  UI построение
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        content = tk.Frame(self.root, bg=COLORS["bg"])
        content.pack(fill="both", expand=True)
        self._build_left_panel(content)
        self._build_canvas(content)
        self._build_status_bar()
        self._build_tab_bar()

    def _build_toolbar(self):
        C = COLORS
        tb = tk.Frame(self.root, bg=C["toolbar"], height=52)
        tb.pack(fill="x", side="top")
        tb.pack_propagate(False)

        tk.Label(tb, text="  🗺 NetMap", font=("Consolas", 14, "bold"),
                 bg=C["toolbar"], fg=C["accent"]).pack(side="left", padx=(12, 20), pady=8)
        tk.Frame(tb, bg=C["border"], width=1).pack(side="left", fill="y", pady=8)

        for text, cmd, color in [
            ("➕ Добавить",    self._add_device,          C["accent2"]),
            ("🔗 Соединить",   self._toggle_connect_mode, C["accent"]),
            ("✂️ Разъединить", self._disconnect_selected, C["warning"]),
            ("🗑 Удалить",     self._delete_selected,     C["danger"]),
        ]:
            tk.Button(tb, text=text, command=cmd,
                      bg=C["bg3"], fg=color,
                      activebackground=C["border"], activeforeground=color,
                      relief="flat", bd=0, font=("Consolas", 10),
                      padx=12, pady=6, cursor="hand2").pack(side="left", padx=4, pady=8)

        # Кнопка сканирования
        tk.Button(tb, text="🔍 Сканировать сеть",
                  command=self._open_scan_dialog,
                  bg=C["bg3"], fg="#c792ea",
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10, "bold"),
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4, pady=8)

        # Кнопка Telegram
        tk.Button(tb, text="🔔 Telegram",
                  command=self._open_telegram_settings,
                  bg=C["bg3"], fg="#ffb454",
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4, pady=8)

        # Кнопка отчёта
        tk.Button(tb, text="📊 Отчёт",
                  command=self._open_report_dialog,
                  bg=C["bg3"], fg="#8be9fd",
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4, pady=8)

        self.monitor_btn_var = tk.StringVar(value="▶ Мониторинг")
        self.monitor_btn = tk.Button(tb, textvariable=self.monitor_btn_var,
                                     command=self._toggle_monitoring,
                                     bg="#1a3a1a", fg=C["online"],
                                     activebackground=C["border"],
                                     relief="flat", bd=0,
                                     font=("Consolas", 10, "bold"),
                                     padx=14, pady=6, cursor="hand2")
        self.monitor_btn.pack(side="right", padx=12, pady=8)

        # Кнопки сохранения/открытия
        tk.Button(tb, text="💾 Сохранить",
                  command=self._save_current_map,
                  bg=C["bg3"], fg=C["text_dim"],
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=10, pady=6, cursor="hand2").pack(side="right", padx=2, pady=8)

        tk.Button(tb, text="💾 Сохранить как",
                  command=self._save_current_map_as,
                  bg=C["bg3"], fg=C["text_dim"],
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=10, pady=6, cursor="hand2").pack(side="right", padx=2, pady=8)

        tk.Button(tb, text="📂 Открыть",
                  command=self._open_map,
                  bg=C["bg3"], fg=C["accent"],
                  activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=10, pady=6, cursor="hand2").pack(side="right", padx=2, pady=8)

        self.tg_dot = tk.Label(tb, text="●", font=("Consolas", 12),
                               bg=C["toolbar"], fg=C["text_dim"])
        self.tg_dot.pack(side="right", padx=2, pady=8)
        tk.Label(tb, text="TG", font=("Consolas", 9),
                 bg=C["toolbar"], fg=C["text_dim"]).pack(side="right", pady=8)

    def _open_report_dialog(self):
        if not self.devices:
            messagebox.showinfo("Нет данных", "Нет устройств для построения отчёта", parent=self.root)
            return
        reporter.show_report_dialog(self.root, self.devices)

    def _build_left_panel(self, parent):
        C = COLORS
        panel = tk.Frame(parent, bg=C["bg2"], width=240)
        panel.pack(side="left", fill="y")
        panel.pack_propagate(False)

        tk.Label(panel, text="УСТРОЙСТВА", font=("Consolas", 9, "bold"),
                 bg=C["bg2"], fg=C["text_dim"], padx=12, pady=10).pack(fill="x")
        tk.Frame(panel, bg=C["border"], height=1).pack(fill="x")

        stats = tk.Frame(panel, bg=C["bg2"], pady=8)
        stats.pack(fill="x", padx=12)
        self.stat_total   = self._stat_label(stats, "Всего",   "0", C["text"])
        self.stat_online  = self._stat_label(stats, "Online",  "0", C["online"])
        self.stat_offline = self._stat_label(stats, "Offline", "0", C["offline"])

        tk.Frame(panel, bg=C["border"], height=1).pack(fill="x")
        tk.Label(panel, text="Список устройств", font=("Consolas", 9),
                 bg=C["bg2"], fg=C["text_dim"], padx=12, pady=6).pack(fill="x")

        lf = tk.Frame(panel, bg=C["bg2"])
        lf.pack(fill="both", expand=True, padx=6)
        sb = tk.Scrollbar(lf, bg=C["bg3"])
        sb.pack(side="right", fill="y")
        self.device_listbox = tk.Listbox(
            lf, bg=C["bg2"], fg=C["text"],
            selectbackground=C["selection"], selectforeground=C["accent"],
            relief="flat", bd=0, font=("Consolas", 10),
            yscrollcommand=sb.set, activestyle="none")
        self.device_listbox.pack(fill="both", expand=True)
        sb.config(command=self.device_listbox.yview)
        self.device_listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self.device_listbox.bind("<Double-Button-1>", self._on_list_double_click)

        tk.Frame(panel, bg=C["border"], height=1).pack(fill="x")
        leg = tk.Frame(panel, bg=C["bg2"], pady=8)
        leg.pack(fill="x", padx=12)
        for dot, color, label in [
            ("●", C["online"],  " Online"),
            ("●", C["offline"], " Offline"),
            ("●", C["unknown"], " Неизвестно"),
            ("●", C["checking"]," Проверка"),
        ]:
            r = tk.Frame(leg, bg=C["bg2"])
            r.pack(fill="x", pady=1)
            tk.Label(r, text=dot,   fg=color,      bg=C["bg2"], font=("Consolas", 10)).pack(side="left")
            tk.Label(r, text=label, fg=C["text_dim"],bg=C["bg2"],font=("Consolas", 9)).pack(side="left")

    def _stat_label(self, parent, title, value, color):
        f = tk.Frame(parent, bg=COLORS["bg2"])
        f.pack(fill="x", pady=2)
        tk.Label(f, text=title+":", font=("Consolas", 9),
                 bg=COLORS["bg2"], fg=COLORS["text_dim"]).pack(side="left")
        lbl = tk.Label(f, text=value, font=("Consolas", 9, "bold"),
                       bg=COLORS["bg2"], fg=color)
        lbl.pack(side="right")
        return lbl

    def _build_canvas(self, parent):
        C = COLORS
        cf = tk.Frame(parent, bg=C["canvas_bg"])
        cf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cf, bg=C["canvas_bg"], highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>",        self._on_canvas_click)
        self.canvas.bind("<Button-3>",        self._on_canvas_right_click)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)
        self.canvas.bind("<B1-Motion>",       self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Button-2>",        self._on_pan_start)
        self.canvas.bind("<B2-Motion>",       self._on_pan_drag)
        self.canvas.bind("<Configure>",       lambda e: self._draw_all())
        self.connect_label = tk.Label(cf,
            text="🔗 Режим соединения: кликните на два устройства",
            font=("Consolas", 10), bg="#1a2a3a", fg=C["accent"], padx=10, pady=4)

    def _build_status_bar(self):
        C = COLORS
        self.status_bar = tk.Frame(self.root, bg=C["status_bar"], height=26)
        self.status_bar.pack(fill="x", side="bottom")
        self.status_bar.pack_propagate(False)
        self.status_var = tk.StringVar(value="Готово")
        tk.Label(self.status_bar, textvariable=self.status_var, font=("Consolas", 9),
                 bg=C["status_bar"], fg=C["text_dim"], padx=12).pack(side="left")
        self.time_var = tk.StringVar()
        tk.Label(self.status_bar, textvariable=self.time_var, font=("Consolas", 9),
                 bg=C["status_bar"], fg=C["text_dim"], padx=12).pack(side="right")
        self._update_time()
        snmp_txt = "SNMP: ✓" if SNMP_AVAILABLE else "SNMP: ✗ (pip install pysnmp)"
        tk.Label(self.status_bar, text=snmp_txt, font=("Consolas", 9),
                 bg=C["status_bar"],
                 fg=C["online"] if SNMP_AVAILABLE else C["warning"],
                 padx=12).pack(side="right")

    def _build_tab_bar(self):
        C = COLORS
        self.tab_bar = tk.Frame(self.root, bg=C["bg2"], height=32)
        self.tab_bar.pack(fill="x", side="bottom")
        self.tab_bar.pack_propagate(False)

    # ──────────────────────────────────────────────────────────────────────────
    #  Рисование карты
    # ──────────────────────────────────────────────────────────────────────────

    def _draw_all(self):
        self.canvas.delete("all")
        self._draw_grid()
        self._draw_connections()
        self._draw_devices()

    def _draw_grid(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        step = 40
        ox, oy = self.canvas_offset
        x = (ox % step) - step
        while x < w:
            self.canvas.create_line(x, 0, x, h, fill=COLORS["grid"])
            x += step
        y = (oy % step) - step
        while y < h:
            self.canvas.create_line(0, y, w, y, fill=COLORS["grid"])
            y += step

    def _draw_connections(self):
        self.conn_labels.draw(
            self.canvas, self.connections,
            self.devices, self.canvas_offset, COLORS
        )

    # def _draw_connections(self):
    #     for (id1, id2) in self.connections:
    #         d1, d2 = self.devices.get(id1), self.devices.get(id2)
    #         if not d1 or not d2:
    #             continue
    #         x1, y1 = d1.x + self.canvas_offset[0], d1.y + self.canvas_offset[1]
    #         x2, y2 = d2.x + self.canvas_offset[0], d2.y + self.canvas_offset[1]
    #         both_on = (d1.status == DeviceStatus.ONLINE and d2.status == DeviceStatus.ONLINE)
    #         self.canvas.create_line(
    #             x1, y1, x2, y2,
    #             fill=COLORS["conn_active"] if both_on else COLORS["connection"],
    #             width=2 if both_on else 1,
    #             dash=() if both_on else (4, 4))

    def _draw_devices(self):
        C = COLORS
        now = time.time()
        for dev_id, dev in self.devices.items():
            x, y = dev.x + self.canvas_offset[0], dev.y + self.canvas_offset[1]
            sz = 40

            sc = {
                DeviceStatus.ONLINE:   C["online"],
                DeviceStatus.OFFLINE:  C["offline"],
                DeviceStatus.UNKNOWN:  C["unknown"],
                DeviceStatus.CHECKING: C["checking"],
            }.get(dev.status, C["unknown"])

            is_sel = (dev_id == getattr(self, 'selected_device', None))
            if is_sel:
                self.canvas.create_oval(x-sz-6, y-sz-6, x+sz+6, y+sz+6,
                                        fill="", outline=C["accent"], width=2)
            self.canvas.create_oval(x-sz, y-sz, x+sz, y+sz,
                                    fill=C["bg3"], outline=sc,
                                    width=2 if not is_sel else 3)
            icon = DEVICE_ICONS.get(dev.dtype, DEVICE_ICONS["other"])
            self.canvas.create_text(x+1, y-5, text=icon, font=("Segoe UI Emoji", 24), fill="#000000")
            self.canvas.create_text(x, y-6, text=icon, font=("Segoe UI Emoji", 24), fill=C["accent"])

            self.canvas.create_oval(x+sz-12, y-sz, x+sz, y-sz+12,
                                    fill=sc, outline=C["bg"], width=1.5)

            if dev.status == DeviceStatus.ONLINE and dev.latency is not None:
                lat_col = C["online"] if dev.latency < 50 else C["warning"] if dev.latency < 150 else C["danger"]
                self.canvas.create_text(x, y+20, text=f"{dev.latency:.0f}ms",
                                        font=("Consolas", 9), fill=lat_col)

            label_y = y + sz + 12
            self.canvas.create_text(x, label_y,
                                    text=dev.name,
                                    font=("Consolas", 10, "bold"), fill=C["text"])
            self.canvas.create_text(x, label_y + 15,
                                    text=dev.ip,
                                    font=("Consolas", 9), fill=C["text_dim"])

            interval_y = label_y + 28
            if self.monitoring_active and getattr(dev, "check_interval", 0) > 0:
                tab = self.current_tab
                last_ts = tab._last_checked_ts.get(dev_id, 0) if tab else 0
                elapsed = now - last_ts
                remaining = max(0, dev.check_interval - elapsed)
                if dev.status == DeviceStatus.CHECKING:
                    countdown_txt = "⏳ опрос..."
                    cnt_col = C["checking"]
                elif remaining < 5:
                    countdown_txt = f"⏱ {remaining:.0f}с"
                    cnt_col = C["warning"]
                else:
                    countdown_txt = f"⏱ {remaining:.0f}с / {dev.check_interval}с"
                    cnt_col = C["text_dim"]
                self.canvas.create_text(x, interval_y,
                                        text=countdown_txt,
                                        font=("Consolas", 8), fill=cnt_col)
                snmp_start_y = interval_y + 16
            else:
                self.canvas.create_text(x, interval_y,
                                        text=f"⏱ каждые {dev.check_interval}с",
                                        font=("Consolas", 8), fill=C["text_dim"])
                snmp_start_y = interval_y + 16

            snmp_info = getattr(dev, "snmp_last_info", None)
            if dev.snmp_enabled and snmp_info and isinstance(snmp_info, dict):
                metrics = [(k, v) for k, v in snmp_info.items()
                           if v and not str(v).startswith("Ошибка")]
                if metrics:
                    pill_h = len(metrics) * 20 + 10
                    pill_w = 190
                    self.canvas.create_rectangle(
                        x - pill_w//2, snmp_start_y - 2,
                        x + pill_w//2, snmp_start_y + pill_h,
                        fill="#0d1f0d", outline="#264f78", width=1)
                    self.canvas.create_text(
                        x, snmp_start_y + 6,
                        text="── SNMP ──",
                        font=("Consolas", 9), fill="#264f78",
                        anchor="center")
                    cy = snmp_start_y + 20
                    for label, value in metrics:
                        display = str(value)
                        if len(display) > 24:
                            display = display[:21] + "…"
                        self.canvas.create_text(
                            x, cy,
                            text=f"{label}: {display}",
                            font=("Consolas", 9), fill="#ffb454",
                            anchor="center")
                        cy += 18
            elif dev.snmp_enabled and dev.status == DeviceStatus.ONLINE:
                self.canvas.create_text(
                    x, snmp_start_y + 6,
                    text="📡 ожидание SNMP...",
                    font=("Consolas", 8), fill=C["text_dim"],
                    anchor="center")
            elif not dev.snmp_enabled and dev.status == DeviceStatus.ONLINE:
                self.canvas.create_text(
                    x, snmp_start_y + 6,
                    text="SNMP выкл. (⚙ настройки)",
                    font=("Consolas", 8), fill=C["border"],
                    anchor="center")

    # ──────────────────────────────────────────────────────────────────────────
    #  Обработка событий холста
    # ──────────────────────────────────────────────────────────────────────────

    def _get_device_at(self, cx, cy) -> Optional[str]:
        sz = 40
        for dev_id, dev in self.devices.items():
            x, y = dev.x + self.canvas_offset[0], dev.y + self.canvas_offset[1]
            if x-sz <= cx <= x+sz and y-sz <= cy <= y+sz:
                return dev_id
        return None

    def _on_canvas_click(self, event):
        dev_id = self._get_device_at(event.x, event.y)
        if hasattr(self, 'connect_mode') and self.connect_mode:
            if dev_id:
                if self.connect_first is None:
                    self.connect_first = dev_id
                    self._set_status(f"Выберите второе устройство")
                elif dev_id != self.connect_first:
                    conn, rev = (self.connect_first, dev_id), (dev_id, self.connect_first)
                    if conn not in self.connections and rev not in self.connections:
                        self._snapshot()
                        self.connections.append(conn)
                    self.connect_first = None
                    self._toggle_connect_mode()
                    self._draw_all()
            return
        if dev_id:
            self.selected_device = dev_id
            dev = self.devices[dev_id]
            self.drag_device = dev_id
            self.drag_offset = (event.x - dev.x - self.canvas_offset[0],
                                event.y - dev.y - self.canvas_offset[1])
            self._set_status(f"Выбрано: {dev.name} ({dev.ip})")
            self._update_list_selection()
        else:
            self.selected_device = self.drag_device = None
            self.pan_start = (event.x, event.y)
        self._draw_all()

    def _on_canvas_drag(self, event):
        if hasattr(self, 'drag_device') and self.drag_device:
            dev = self.devices[self.drag_device]
            dev.x = event.x - self.drag_offset[0] - self.canvas_offset[0]
            dev.y = event.y - self.drag_offset[1] - self.canvas_offset[1]
            self._draw_all()
        elif hasattr(self, 'pan_start') and self.pan_start:
            dx, dy = event.x - self.pan_start[0], event.y - self.pan_start[1]
            self.canvas_offset[0] += dx
            self.canvas_offset[1] += dy
            self.pan_start = (event.x, event.y)
            self._draw_all()

    def _on_canvas_release(self, event):
        if hasattr(self, 'drag_device') and self.drag_device:
            self._snapshot()
        self.drag_device = self.pan_start = None

    def _on_canvas_double_click(self, event):
        dev_id = self._get_device_at(event.x, event.y)
        if dev_id:
            self._open_device_settings(dev_id)

    def _on_canvas_right_click(self, event):
        dev_id = self._get_device_at(event.x, event.y)
        conn = self.conn_labels.get_connection_at(
            event.x, event.y,
            self.connections, self.devices, self.canvas_offset
        )
        if conn:
            ConnectionLabelDialog(
                self.root, conn, self.devices,
                self.conn_labels, COLORS
            )
            return

        dev_id = self._get_device_at(event.x, event.y)
        if not dev_id:
            return
        self.selected_device = dev_id
        self._draw_all()
        C = COLORS
        dev = self.devices[dev_id]
        menu = tk.Menu(self.root, tearoff=0,
                       bg=C["bg3"], fg=C["text"],
                       activebackground=C["selection"],
                       font=("Consolas", 10))
        menu.add_command(label=f"⚙ Настройки: {dev.name}",
                         command=lambda: self._open_device_settings(dev_id))
        menu.add_command(label="🏓 Опросить вручную",
                         command=lambda: self._ping_once(dev_id))
        menu.add_separator()
        menu.add_command(label="🗑 Удалить хост",
                         command=lambda: self._delete_device(dev_id),
                         foreground=C["danger"])
        menu.add_command(
            label="🔎 LLD — обнаружение элементов",
            command=lambda: self._open_lld_for(dev_id)
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _on_pan_start(self, event):
        self.pan_start = (event.x, event.y)

    def _on_pan_drag(self, event):
        self._on_canvas_drag(event)

    # ──────────────────────────────────────────────────────────────────────────
    #  Список устройств
    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_device_list(self):
        C = COLORS
        self.device_listbox.delete(0, tk.END)
        si = {DeviceStatus.ONLINE:"●", DeviceStatus.OFFLINE:"○",
              DeviceStatus.UNKNOWN:"◌", DeviceStatus.CHECKING:"◎"}
        sc = {DeviceStatus.ONLINE:C["online"], DeviceStatus.OFFLINE:C["offline"],
              DeviceStatus.UNKNOWN:C["text_dim"], DeviceStatus.CHECKING:C["checking"]}
        for dev_id, dev in self.devices.items():
            icon = si.get(dev.status, "◌")
            lat  = f" {dev.latency:.0f}ms" if dev.latency else ""
            self.device_listbox.insert(tk.END, f" {icon} {dev.name} ({dev.ip}){lat}")
            self.device_listbox.itemconfig(
                self.device_listbox.size()-1,
                fg=sc.get(dev.status, C["text_dim"]))
        total   = len(self.devices)
        online  = sum(1 for d in self.devices.values() if d.status == DeviceStatus.ONLINE)
        offline = sum(1 for d in self.devices.values() if d.status == DeviceStatus.OFFLINE)
        self.stat_total.config(text=str(total))
        self.stat_online.config(text=str(online))
        self.stat_offline.config(text=str(offline))

    def _update_list_selection(self):
        ids = list(self.devices.keys())
        if hasattr(self, 'selected_device') and self.selected_device in ids:
            idx = ids.index(self.selected_device)
            self.device_listbox.selection_clear(0, tk.END)
            self.device_listbox.selection_set(idx)

    def _on_list_select(self, event):
        sel = self.device_listbox.curselection()
        if sel:
            ids = list(self.devices.keys())
            if sel[0] < len(ids):
                self.selected_device = ids[sel[0]]
                self._draw_all()

    def _on_list_double_click(self, event):
        sel = self.device_listbox.curselection()
        if sel:
            ids = list(self.devices.keys())
            if sel[0] < len(ids):
                self._open_device_settings(ids[sel[0]])

    # ──────────────────────────────────────────────────────────────────────────
    #  CRUD устройств
    # ──────────────────────────────────────────────────────────────────────────

    def _add_device(self):
        self._snapshot()
        import uuid
        dev_id = str(uuid.uuid4())[:8]
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        dev = Device(dev_id=dev_id, name="Новый хост", ip="192.168.1.1",
                     x=max(60, w//2 - self.canvas_offset[0]),
                     y=max(60, h//2 - self.canvas_offset[1]))
        self.devices[dev_id] = dev
        self._open_device_settings(dev_id)

    def _delete_selected(self):
        if hasattr(self, 'selected_device') and self.selected_device:
            self._delete_device(self.selected_device)

    def _delete_device(self, dev_id: str):
        dev = self.devices.get(dev_id)
        if dev and messagebox.askyesno("Удаление", f"Удалить '{dev.name}'?",
                                       parent=self.root):
            self._snapshot()
            del self.devices[dev_id]
            self.connections = [(a, b) for (a, b) in self.connections
                                if a != dev_id and b != dev_id]
            if self.current_tab:
                self.current_tab._prev_stable.pop(dev_id, None)
            if hasattr(self, 'selected_device') and self.selected_device == dev_id:
                self.selected_device = None
            self._refresh_device_list()
            self._draw_all()

    def _toggle_connect_mode(self):
        self.connect_mode = not self.connect_mode
        self.connect_first = None
        if self.connect_mode:
            self.connect_label.place(relx=0.5, y=8, anchor="n")
        else:
            self.connect_label.place_forget()

    def _disconnect_selected(self):
        if hasattr(self, 'selected_device') and self.selected_device:
            self._snapshot()
            dev_id = self.selected_device
            self.connections = [(a, b) for (a, b) in self.connections
                                if a != dev_id and b != dev_id]
            self._draw_all()

    def _open_device_settings(self, dev_id: str):
        dev = self.devices.get(dev_id)
        if dev:
            dlg = DeviceSettingsDialog(self.root, dev, COLORS, DEVICE_ICONS)
            self.root.wait_window(dlg.dialog)
            self._refresh_device_list()
            self._draw_all()

    def _open_scan_dialog(self):
        dlg = ScanDialog(self.root, COLORS)
        self.root.wait_window(dlg.dialog)
        if dlg.result:
            self._import_scanned_devices(dlg.result)

    def _import_scanned_devices(self, found_ips: list[str]):
        self._snapshot()
        import uuid
        w = self.canvas.winfo_width()
        cols = max(1, int((w - 100) // 120))
        existing = {d.ip for d in self.devices.values()}
        added = 0
        for ip in found_ips:
            if ip in existing:
                continue
            dev_id = str(uuid.uuid4())[:8]
            dev = Device(dev_id=dev_id, name=f"Host-{ip.split('.')[-1]}", ip=ip,
                         x=80 + (added % cols) * 130 - self.canvas_offset[0],
                         y=100 + (added // cols) * 130 - self.canvas_offset[1])
            self.devices[dev_id] = dev
            added += 1
        self._set_status(f"Добавлено {added} устройств из сканирования")
        self._refresh_device_list()
        self._draw_all()

    # ──────────────────────────────────────────────────────────────────────────
    #  Telegram настройки
    # ──────────────────────────────────────────────────────────────────────────

    def _open_telegram_settings(self):
        C = COLORS
        dlg = tk.Toplevel(self.root)
        dlg.title("Настройки Telegram")
        dlg.geometry("500x440")
        dlg.configure(bg=C["bg"])
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        tk.Label(dlg, text="🔔 Telegram Оповещения",
                 font=("Consolas", 12, "bold"),
                 bg=C["bg"], fg=C["accent"]).pack(pady=12)

        f = tk.Frame(dlg, bg=C["bg"])
        f.pack(fill="x", padx=20)
        f.columnconfigure(1, weight=1)

        def entry(row, label, default, show=""):
            tk.Label(f, text=label, font=("Consolas", 10),
                     bg=C["bg"], fg=C["text_dim"], anchor="w"
                     ).grid(row=row, column=0, sticky="w", pady=6)
            var = tk.StringVar(value=default)
            e = tk.Entry(f, textvariable=var, font=("Consolas", 10), show=show,
                         bg=C["bg3"], fg=C["text"], relief="flat",
                         highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["accent"])
            e.grid(row=row, column=1, sticky="ew", pady=6, padx=(8, 0))
            return var

        tok_var = entry(0, "Bot Token:", self.tg_token, show="•")
        cht_var = entry(1, "Chat ID:",   self.tg_chat_id)

        show_tok = tk.BooleanVar(value=False)
        def _toggle_show():
            tok_entry = f.grid_slaves(row=0, column=1)[0]
            tok_entry.config(show="" if show_tok.get() else "•")
        tk.Checkbutton(f, text="Показать токен", variable=show_tok,
                       command=_toggle_show,
                       font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"],
                       activebackground=C["bg"], selectcolor=C["bg3"],
                       cursor="hand2").grid(row=0, column=2, padx=4)

        ev = tk.LabelFrame(dlg, text="  Когда отправлять  ",
                           font=("Consolas", 9),
                           bg=C["bg"], fg=C["accent"],
                           bd=1, relief="flat",
                           highlightbackground=C["border"], highlightthickness=1)
        ev.pack(fill="x", padx=20, pady=8)

        tk.Checkbutton(ev, text="🔴  Устройство стало Offline",
                       variable=self.alert_on_offline,
                       font=("Consolas", 10), bg=C["bg"], fg=C["text"],
                       activebackground=C["bg"], selectcolor=C["bg3"],
                       cursor="hand2").pack(anchor="w", padx=12, pady=3)
        tk.Checkbutton(ev, text="✅  Устройство восстановилось (Online)",
                       variable=self.alert_on_online,
                       font=("Consolas", 10), bg=C["bg"], fg=C["text"],
                       activebackground=C["bg"], selectcolor=C["bg3"],
                       cursor="hand2").pack(anchor="w", padx=12, pady=3)

        tk.Label(dlg,
                 text="Как получить токен: @BotFather → /newbot\n"
                      "Как получить Chat ID: напишите боту /start,\n"
                      "затем: api.telegram.org/bot<TOKEN>/getUpdates",
                 font=("Consolas", 8), bg=C["bg"], fg=C["text_dim"],
                 justify="left").pack(anchor="w", padx=20, pady=(0, 4))

        result_lbl = tk.Label(dlg, text="", font=("Consolas", 9),
                              bg=C["bg"], fg=C["text_dim"])
        result_lbl.pack(anchor="w", padx=20)

        def save():
            self.tg_token = tok_var.get().strip()
            self.tg_chat_id = cht_var.get().strip()
            self._update_tg_dot()
            self._set_status("Telegram настройки сохранены")
            dlg.destroy()

        def test():
            token = tok_var.get().strip()
            chat_id = cht_var.get().strip()
            if not token or not chat_id:
                result_lbl.config(text="⚠ Введите токен и Chat ID", fg=C["warning"])
                return
            result_lbl.config(text="Отправляем тест...", fg=C["checking"])
            def _run():
                test_text = escape_markdownv2("✅ NetMap — тест оповещений!") + "\n\n"
                test_text += escape_markdownv2("Соединение работает!") + "\n"
                test_text += f"⏰ _{escape_markdownv2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}_"
                ok, err = send_telegram_markdown(token, chat_id, test_text)
                if ok:
                    result_lbl.config(text="✅ Тест отправлен успешно!", fg=C["online"])
                else:
                    result_lbl.config(text=f"❌ {err[:70]}", fg=C["danger"])
            threading.Thread(target=_run, daemon=True).start()

        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20, pady=10)
        tk.Button(bf, text="📨 Тест", command=test,
                  bg=C["bg3"], fg=C["accent"], activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=12, pady=6, cursor="hand2").pack(side="left")
        tk.Button(bf, text="Отмена", command=dlg.destroy,
                  bg=C["bg3"], fg=C["text_dim"], activebackground=C["border"],
                  relief="flat", bd=0, font=("Consolas", 10),
                  padx=12, pady=6, cursor="hand2").pack(side="right", padx=4)
        tk.Button(bf, text="💾 Сохранить", command=save,
                  bg=C["accent2"], fg="white", activebackground="#2d8f40",
                  relief="flat", bd=0, font=("Consolas", 10, "bold"),
                  padx=14, pady=6, cursor="hand2").pack(side="right")

    def _update_tg_dot(self):
        ok = bool(self.tg_token and self.tg_chat_id)
        self.tg_dot.config(fg=COLORS["online"] if ok else COLORS["text_dim"])

    def _update_time(self):
        self.time_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.root.after(1000, self._update_time)

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    # ──────────────────────────────────────────────────────────────────────────
    #  Мониторинг и SNMP
    # ──────────────────────────────────────────────────────────────────────────

    def _toggle_monitoring(self):
        C = COLORS
        if self.monitoring_active:
            self.monitoring_active = False
            self.monitor_btn_var.set("▶ Мониторинг")
            self.monitor_btn.config(bg="#1a3a1a", fg=C["online"])
            self._set_status("Мониторинг остановлен")
        else:
            if not self.current_tab:
                return
            self.monitoring_active = True
            self.monitor_btn_var.set("⏹ Стоп")
            self.monitor_btn.config(bg="#3a1a1a", fg=C["danger"])
            self._set_status("Мониторинг запущен")
            self.monitor_thread = threading.Thread(
                target=self._monitoring_loop, daemon=True)
            self.monitor_thread.start()

    def _monitoring_loop(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            while self.monitoring_active:
                tab = self.current_tab
                if not tab:
                    time.sleep(1)
                    continue
                now = time.time()
                for dev_id, dev in list(tab.devices.items()):
                    if not self.monitoring_active:
                        break
                    interval = getattr(dev, "check_interval", 30)
                    last_ts = tab._last_checked_ts.get(dev_id, 0)
                    if now - last_ts >= interval:
                        tab._last_checked_ts[dev_id] = now
                        if dev.ping_enabled or dev.snmp_enabled:
                            self.result_queue.put(("status", dev_id, DeviceStatus.CHECKING))
                            executor.submit(
                                lambda d_id=dev_id, d_obj=dev:
                                    self.result_queue.put(
                                        ("update", d_id, self._check_device(d_obj))
                                    )
                            )
                time.sleep(0.5)

    def _check_device(self, dev: Device) -> dict:
        result = {"status": DeviceStatus.UNKNOWN, "latency": None, "snmp_info": None}
        if dev.ping_enabled:
            latency = self._do_ping(dev.ip)
            if latency is not None:
                result["status"] = DeviceStatus.ONLINE
                result["latency"] = latency
            else:
                result["status"] = DeviceStatus.OFFLINE
        if dev.snmp_enabled and SNMP_AVAILABLE:
            snmp_data, snmp_errors = self._do_snmp(dev)
            result["snmp_info"] = snmp_data
            result["snmp_errors"] = snmp_errors
            if snmp_data:
                if result["status"] != DeviceStatus.ONLINE:
                    result["status"] = DeviceStatus.ONLINE
            elif not snmp_errors and not dev.ping_enabled:
                result["status"] = DeviceStatus.OFFLINE
        return result

    def _do_ping(self, ip: str) -> Optional[float]:
        try:
            os_type = platform.system().lower()
            cmd = (["ping", "-n", "1", "-w", "1000", ip]
                   if os_type == "windows"
                   else ["ping", "-c", "1", "-W", "1", ip])
            start = time.time()
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            elapsed = (time.time() - start) * 1000
            if res.returncode == 0:
                for p in [r"time[=<]([\d.]+)\s*ms",
                          r"Average\s*=\s*([\d.]+)\s*ms",
                          r"rtt.*?=\s*[\d.]+/([\d.]+)/"]:
                    m = re.search(p, res.stdout, re.IGNORECASE)
                    if m:
                        return float(m.group(1))
                return elapsed
            return None
        except Exception:
            return None

    def _do_snmp(self, dev: Device) -> tuple[dict, dict]:
        if not SNMP_AVAILABLE:
            return {}, {}
        community = dev.snmp_community or "public"
        port = dev.snmp_port or 161
        version = dev.snmp_version or "2c"
        oid_list = []
        if hasattr(dev, "snmp_oids") and dev.snmp_oids:
            for oid_cfg in dev.snmp_oids:
                if oid_cfg.get("enabled", True):
                    label = oid_cfg.get("label", "OID")
                    oid_str = oid_cfg.get("oid", "").strip()
                    unit = oid_cfg.get("unit", "").strip()
                    if oid_str:
                        oid_list.append((label, oid_str, unit, oid_cfg))
        if not oid_list:
            oid_list = [("sysDescr", "1.3.6.1.2.1.1.1.0", "", None)]
        data = {}
        errors = {}
        for label, oid_str, unit, cfg in oid_list:
            raw, err = snmp_get_sync(dev.ip, community, port, version, oid_str)
            if err:
                errors[label] = err
                continue
            factor = cfg.get("factor", 1.0) if cfg else 1.0
            if "uptime" in label.lower() or "аптайм" in label.lower():
                val = format_uptime(raw)
            else:
                try:
                    num = float(raw)
                    num *= factor
                    if num.is_integer():
                        val = str(int(num))
                    else:
                        val = f"{num:.2f}".rstrip('0').rstrip('.')
                    if unit:
                        val = f"{val} {unit}"
                except (ValueError, TypeError):
                    val = raw
                    if unit:
                        val = f"{val} {unit}"
            data[label] = val
        return data, errors

    def _ping_once(self, dev_id: str):
        dev = self.devices.get(dev_id)
        if not dev:
            return
        self.result_queue.put(("status", dev_id, DeviceStatus.CHECKING))
        threading.Thread(
            target=lambda: self.result_queue.put(
                ("update", dev_id, self._check_device(dev))
            ), daemon=True
        ).start()

    # ──────────────────────────────────────────────────────────────────────────
    #  Триггеры SNMP
    # ──────────────────────────────────────────────────────────────────────────

    def _evaluate_triggers(self, dev: Device, snmp_data: dict):
        if not self.tg_token or not self.tg_chat_id:
            return
        now = time.time()
        for oid_cfg in dev.snmp_oids:
            if not oid_cfg.get("enabled", True):
                continue
            label = oid_cfg.get("label", "")
            condition = oid_cfg.get("condition", "").strip()
            threshold_str = oid_cfg.get("threshold", "").strip()
            if not condition or not threshold_str:
                continue
            try:
                threshold = float(threshold_str)
            except ValueError:
                continue
            raw_value = snmp_data.get(label)
            if raw_value is None:
                continue
            numeric = self._extract_number(raw_value)
            if numeric is None:
                continue
            triggered = False
            if condition == ">" and numeric > threshold:
                triggered = True
            elif condition == "<" and numeric < threshold:
                triggered = True
            elif condition == ">=" and numeric >= threshold:
                triggered = True
            elif condition == "<=" and numeric <= threshold:
                triggered = True
            elif condition == "==" and numeric == threshold:
                triggered = True
            last = oid_cfg.get("last_alert", 0)
            if triggered and (now - last) > 300:
                oid_cfg["last_alert"] = now
                self._send_snmp_trigger_alert(dev, label, numeric, condition, threshold, raw_value)

    def _extract_number(self, s: str) -> Optional[float]:
        match = re.search(r"[-+]?\d*\.?\d+", str(s))
        if match:
            try:
                return float(match.group())
            except:
                pass
        return None

    def _send_snmp_trigger_alert(self, dev: Device, metric_label: str, value: float,
                                  condition: str, threshold: float, raw_value: str):
        dev_name = escape_markdownv2(dev.name)
        dev_ip = escape_markdownv2(dev.ip)
        metric = escape_markdownv2(metric_label)
        raw_val = escape_markdownv2(raw_value)
        cond = escape_markdownv2(condition)
        threshold_str = escape_markdownv2(str(threshold))
        time_str = escape_markdownv2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        msg = (
            f"⚠️ *Сработал SNMP‑триггер*\n\n"
            f"┌ *Устройство:* {dev_name}\n"
            f"├ *IP:* `{dev_ip}`\n"
            f"├ *Метрика:* {metric}\n"
            f"├ *Значение:* `{raw_val}`\n"
            f"├ *Условие:* {cond} {threshold_str}\n"
            f"└ *Время:* _{time_str}_"
        )
        token, chat_id = self.tg_token, self.tg_chat_id
        def _run():
            ok, err = send_telegram_markdown(token, chat_id, msg)
            if ok:
                print(f"[Telegram] ✅ SNMP-триггер сработал: {dev.name} - {metric_label}")
            else:
                print(f"[Telegram] ❌ ошибка: {err}")
        threading.Thread(target=_run, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    #  Обработка очереди (включает сохранение истории)
    # ──────────────────────────────────────────────────────────────────────────

    def _start_queue_processor(self):
        self._process_queue()

    def _process_queue(self):
        try:
            while True:
                item = self.result_queue.get_nowait()
                tab = self.current_tab
                if not tab:
                    continue
                if item[0] == "status":
                    _, dev_id, status = item
                    if dev_id in tab.devices:
                        tab.devices[dev_id].status = status
                elif item[0] == "update":
                    _, dev_id, result = item
                    if dev_id not in tab.devices:
                        continue
                    dev = tab.devices[dev_id]
                    new_status = result["status"]
                    if new_status in (DeviceStatus.ONLINE, DeviceStatus.OFFLINE):
                        prev = tab._prev_stable.get(dev_id)
                        if prev != new_status:
                            if prev is not None or new_status == DeviceStatus.OFFLINE:
                                self._send_telegram_alert(
                                    dev.name, dev.ip,
                                    prev if prev is not None else DeviceStatus.UNKNOWN,
                                    new_status
                                )
                        tab._prev_stable[dev_id] = new_status
                    dev.status = new_status
                    dev.latency = result.get("latency")
                    new_snmp = result.get("snmp_info")
                    if new_snmp:
                        dev.snmp_last_info = new_snmp
                        self._evaluate_triggers(dev, new_snmp)
                        # Сохраняем историю в БД
                        reporter.store_snmp_data(dev, new_snmp)
                    dev.last_checked = datetime.now().strftime("%H:%M:%S")
                    snmp_errors = result.get("snmp_errors", {})
                    if snmp_errors:
                        self._send_snmp_error_alert(dev, snmp_errors)
        except queue.Empty:
            pass
        finally:
            self._refresh_device_list()
            self._draw_all()
            self.root.after(1000, self._process_queue)

    def _send_telegram_alert(self, dev_name: str, ip: str,
                             old_status: DeviceStatus, new_status: DeviceStatus):
        if not self.tg_token or not self.tg_chat_id:
            return
        if new_status == DeviceStatus.OFFLINE and not self.alert_on_offline.get():
            return
        if new_status == DeviceStatus.ONLINE and not self.alert_on_online.get():
            return
        status_map = {
            DeviceStatus.ONLINE:  "✅ ONLINE",
            DeviceStatus.OFFLINE: "❌ OFFLINE",
            DeviceStatus.UNKNOWN: "❔ НЕИЗВЕСТНО",
        }
        old_txt = status_map.get(old_status, "?")
        new_txt = status_map.get(new_status, "?")
        dev_name_esc = escape_markdownv2(dev_name)
        ip_esc = escape_markdownv2(ip)
        old_esc = escape_markdownv2(old_txt)
        new_esc = escape_markdownv2(new_txt)
        time_esc = escape_markdownv2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        msg = (
            f"🔔 *Изменение статуса*\n\n"
            f"┌ *Устройство:* {dev_name_esc}\n"
            f"├ *IP:* `{ip_esc}`\n"
            f"├ *Статус:* {old_esc} → {new_esc}\n"
            f"└ *Время:* _{time_esc}_"
        )
        token, chat_id = self.tg_token, self.tg_chat_id
        def _run():
            ok, err = send_telegram_markdown(token, chat_id, msg)
            if ok:
                print(f"[Telegram] ✅ отправлено: {dev_name}")
            else:
                print(f"[Telegram] ❌ ошибка для {dev_name}: {err}")
        threading.Thread(target=_run, daemon=True).start()

    def _send_snmp_error_alert(self, dev: Device, errors: dict):
        if not self.tg_token or not self.tg_chat_id:
            return
        now = time.time()
        msgs = []
        for label, err_text in errors.items():
            key = f"{dev.dev_id}:{label}"
            last_sent = self.current_tab._snmp_alert_ts.get(key, 0) if self.current_tab else 0
            if now - last_sent < 600:
                continue
            if self.current_tab:
                self.current_tab._snmp_alert_ts[key] = now
            msgs.append(f"│   • `{escape_markdownv2(label)}`: {escape_markdownv2(err_text[:60])}")
        if not msgs:
            return
        dev_name = escape_markdownv2(dev.name)
        dev_ip = escape_markdownv2(dev.ip)
        time_esc = escape_markdownv2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        msg = (
            f"⚠️ *SNMP ошибка*\n\n"
            f"┌ *Устройство:* {dev_name}\n"
            f"├ *IP:* `{dev_ip}`\n"
            f"├ *Недоступные OID:*\n"
            + "\n".join(msgs) +
            f"\n└ *Время:* _{time_esc}_"
        )
        token, chat_id = self.tg_token, self.tg_chat_id
        def _run():
            ok, err = send_telegram_markdown(token, chat_id, msg)
            if ok:
                print(f"[Telegram] ✅ SNMP-алерт отправлен: {dev.name}")
            else:
                print(f"[Telegram] ❌ SNMP-алерт не отправлен: {err}")
        threading.Thread(target=_run, daemon=True).start()

    def _snapshot(self):
        tab = self.current_tab
        if tab:
            tab.undo_stack.append(tab.snapshot())

    def _undo(self, event=None):
        tab = self.current_tab
        if not tab or not tab.undo_stack:
            self._set_status("Нечего отменять")
            return
        state = tab.undo_stack.pop()
        tab.restore_snapshot(state)
        self._refresh_device_list()
        self._draw_all()
        self._set_status(f"Отменено. Осталось в стеке: {len(tab.undo_stack)}")

    def on_close(self):
        self.monitoring_active = False
        if messagebox.askyesno("Выход", "Сохранить изменения?", parent=self.root):
            self._save_current_map()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = NetworkMapApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
"""
connection_snmp.py — SNMP метрики на линиях соединения
=======================================================
Добавляет к линиям соединения:
  • Метку с выбранной SNMP-метрикой прямо на линии (авто-обновление)
  • Всплывающий тултип при наведении мыши на линию
  • Попап-диалог при правом клике на линию — выбор метрики для отображения
  • Хранит настройки per-connection в MapData.connection_metrics
"""

from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from device import Device
    from main import NetworkMapApp


# ─── Утилиты геометрии ───────────────────────────────────────────────────────

def _point_to_segment_dist(px, py, x1, y1, x2, y2) -> float:
    """Расстояние от точки (px,py) до отрезка (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _midpoint(x1, y1, x2, y2):
    return (x1 + x2) / 2, (y1 + y2) / 2


# ─── Менеджер метрик соединений ──────────────────────────────────────────────

class ConnectionMetricsManager:
    """
    Хранит какая метрика показывается на каждой линии.

    Формат ключа: "dev_id1:dev_id2" (отсортированные)
    Значение: {"label": str, "oid": str, "unit": str, "factor": float}
              или None (не показывать)
    """

    def __init__(self, map_data):
        self._map = map_data
        # Инициализируем хранилище если нет
        if not hasattr(map_data, "connection_metrics"):
            map_data.connection_metrics = {}

    @staticmethod
    def conn_key(id1: str, id2: str) -> str:
        return ":".join(sorted([id1, id2]))

    def get(self, id1: str, id2: str) -> Optional[dict]:
        return self._map.connection_metrics.get(self.conn_key(id1, id2))

    def set(self, id1: str, id2: str, metric: Optional[dict]):
        self._map.connection_metrics[self.conn_key(id1, id2)] = metric

    def clear(self, id1: str, id2: str):
        self._map.connection_metrics.pop(self.conn_key(id1, id2), None)


# ─── Тултип ──────────────────────────────────────────────────────────────────

class ConnectionTooltip:
    """Лёгкий тултип, появляется при наведении на линию."""

    def __init__(self, canvas: tk.Canvas, colors: dict):
        self.canvas = canvas
        self.colors = colors
        self._tip_id: Optional[int] = None
        self._bg_id: Optional[int] = None

    def show(self, x: float, y: float, text: str):
        self.hide()
        C = self.colors
        pad = 6
        tmp = self.canvas.create_text(0, 0, text=text,
                                      font=("Consolas", 9), anchor="nw")
        bb = self.canvas.bbox(tmp)
        self.canvas.delete(tmp)
        if not bb:
            return
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

        rx, ry = x + 12, y - th // 2 - pad
        self._bg_id = self.canvas.create_rectangle(
            rx - pad, ry - pad,
            rx + tw + pad, ry + th + pad,
            fill=C.get("bg2", "#23233a"),
            outline=C.get("accent", "#ffb86c"),
            width=1,
            tags="conn_tooltip"
        )
        self._tip_id = self.canvas.create_text(
            rx, ry, text=text,
            font=("Consolas", 9),
            fill=C.get("text", "#ffffff"),
            anchor="nw", tags="conn_tooltip"
        )

    def hide(self):
        self.canvas.delete("conn_tooltip")
        self._tip_id = self._bg_id = None


# ─── Попап выбора метрики ─────────────────────────────────────────────────────

class ConnectionMetricPicker:
    """
    Диалог выбора SNMP-метрики для отображения на линии соединения.
    Показывает объединённый список OID обоих устройств.
    """

    def __init__(self, parent: tk.Misc, dev1: "Device", dev2: "Device",
                 current: Optional[dict], colors: dict,
                 on_apply):
        """
        on_apply(metric_dict | None) вызывается при подтверждении.
        metric_dict = {"label": ..., "oid": ..., "unit": ..., "factor": ...}
        """
        self.colors = C = colors
        self.on_apply = on_apply

        self.win = tk.Toplevel(parent)
        self.win.title(f"Метрика линии: {dev1.name} ↔ {dev2.name}")
        self.win.geometry("540x460")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(False, True)

        # Собираем все OID из обоих устройств
        self._options: list[dict] = []  # [{label, oid, unit, factor, source}]
        for dev, tag in [(dev1, dev1.name), (dev2, dev2.name)]:
            if dev.snmp_enabled:
                for o in dev.snmp_oids:
                    if o.get("enabled", True):
                        self._options.append({
                            "label":  o.get("label", o["oid"]),
                            "oid":    o["oid"],
                            "unit":   o.get("unit", ""),
                            "factor": o.get("factor", 1.0),
                            "source": tag,
                        })

        self._build(current)

    def _build(self, current: Optional[dict]):
        C = self.colors

        # Заголовок
        hdr = tk.Frame(self.win, bg=C["bg2"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="📊 Выберите метрику для линии соединения",
                 font=("Consolas", 11, "bold"),
                 bg=C["bg2"], fg=C["accent"], padx=16).pack(side="left")

        # Поиск
        sf = tk.Frame(self.win, bg=C["bg"], pady=4)
        sf.pack(fill="x", padx=12)
        tk.Label(sf, text="🔍", bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter())
        tk.Entry(sf, textvariable=self._search_var,
                 bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["border"],
                 highlightcolor=C["accent"],
                 font=("Consolas", 10)
                 ).pack(side="left", fill="x", expand=True, padx=6)

        # Список метрик
        lf = tk.Frame(self.win, bg=C["bg"])
        lf.pack(fill="both", expand=True, padx=12, pady=4)

        style = ttk.Style()
        style.configure("ConnPick.Treeview",
                        background=C["bg2"], foreground=C["text"],
                        fieldbackground=C["bg2"], rowheight=24)
        style.configure("ConnPick.Treeview.Heading",
                        background=C["bg3"], foreground=C["text_dim"],
                        font=("Consolas", 9))
        style.map("ConnPick.Treeview",
                  background=[("selected", C["selection"])])

        cols = ("source", "label", "oid", "unit")
        self._tree = ttk.Treeview(lf, columns=cols, show="headings",
                                   style="ConnPick.Treeview",
                                   selectmode="browse")
        self._tree.heading("source", text="Устройство")
        self._tree.heading("label",  text="Метрика")
        self._tree.heading("oid",    text="OID")
        self._tree.heading("unit",   text="Ед.")
        self._tree.column("source", width=110, stretch=False)
        self._tree.column("label",  width=180)
        self._tree.column("oid",    width=180)
        self._tree.column("unit",   width=50, stretch=False)

        sb = tk.Scrollbar(lf, orient="vertical",
                          command=self._tree.yview, bg=C["bg3"])
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._fill_tree(self._options)

        # Выделяем текущую метрику
        if current:
            for iid in self._tree.get_children():
                vals = self._tree.item(iid, "values")
                if vals and vals[2] == current.get("oid"):
                    self._tree.selection_set(iid)
                    self._tree.see(iid)
                    break

        # Источник данных: выбираем устройство для опроса
        src_frame = tk.Frame(self.win, bg=C["bg"], pady=4)
        src_frame.pack(fill="x", padx=12)
        tk.Label(src_frame, text="Опрашивать:",
                 font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._source_var = tk.StringVar(value=current.get("source", "") if current else "")
        self._source_cb  = ttk.Combobox(src_frame, textvariable=self._source_var,
                                         state="readonly", width=20,
                                         font=("Consolas", 9))
        sources = list({o["source"] for o in self._options})
        self._source_cb["values"] = sources
        if not self._source_var.get() and sources:
            self._source_var.set(sources[0])
        self._source_cb.pack(side="left", padx=8)

        # Интервал обновления
        tk.Label(src_frame, text="  Интервал (сек):",
                 font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._interval_var = tk.StringVar(value=str(current.get("interval", 30)) if current else "30")
        tk.Entry(src_frame, textvariable=self._interval_var,
                 width=5, bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["border"],
                 font=("Consolas", 9)).pack(side="left", padx=4)

        # Кнопки
        btn_row = tk.Frame(self.win, bg=C["bg"], pady=8)
        btn_row.pack(fill="x", padx=12)

        tk.Button(btn_row, text="✓ Применить",
                  command=self._apply,
                  bg=C["accent"], fg="white",
                  activebackground=C["accent2"],
                  font=("Consolas", 10, "bold"),
                  relief="flat", bd=0, padx=14, pady=6,
                  cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_row, text="✕ Убрать метрику",
                  command=self._remove,
                  bg=C["bg3"], fg=C["danger"],
                  activebackground=C["border"],
                  font=("Consolas", 10),
                  relief="flat", bd=0, padx=14, pady=6,
                  cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_row, text="Отмена",
                  command=self.win.destroy,
                  bg=C["bg3"], fg=C["text_dim"],
                  activebackground=C["border"],
                  font=("Consolas", 10),
                  relief="flat", bd=0, padx=14, pady=6,
                  cursor="hand2").pack(side="right", padx=4)

    def _fill_tree(self, options: list[dict]):
        self._tree.delete(*self._tree.get_children())
        for o in options:
            self._tree.insert("", "end", values=(
                o["source"], o["label"], o["oid"], o["unit"]
            ))

    def _filter(self):
        q = self._search_var.get().lower()
        filtered = [o for o in self._options
                    if q in o["label"].lower()
                    or q in o["oid"].lower()
                    or q in o["source"].lower()]
        self._fill_tree(filtered)

    def _apply(self):
        sel = self._tree.selection()
        if not sel:
            tk.messagebox.showwarning("Выбор метрики",
                                      "Выберите метрику из списка.",
                                      parent=self.win)
            return
        vals = self._tree.item(sel[0], "values")
        # Ищем полный dict по OID
        oid = vals[2]
        opt = next((o for o in self._options if o["oid"] == oid), None)
        if not opt:
            return
        try:
            interval = max(5, int(self._interval_var.get()))
        except ValueError:
            interval = 30

        metric = {
            "label":    opt["label"],
            "oid":      opt["oid"],
            "unit":     opt["unit"],
            "factor":   opt["factor"],
            "source":   self._source_var.get(),
            "interval": interval,
            "last_val": None,
            "last_ts":  0,
        }
        self.on_apply(metric)
        self.win.destroy()

    def _remove(self):
        self.on_apply(None)
        self.win.destroy()


# ─── Главный класс — интегрируется в NetworkMapApp ───────────────────────────

class ConnectionSNMPOverlay:
    """
    Встраивается в NetworkMapApp.
    Вызывайте методы из соответствующих мест main.py (см. инструкцию ниже).
    """

    HIT_RADIUS = 8   # пикселей — радиус клика на линию

    def __init__(self, app: "NetworkMapApp"):
        self.app    = app
        self.canvas: tk.Canvas = app.canvas
        self.colors = app.colors if hasattr(app, "colors") else {}
        self._tooltip = ConnectionTooltip(self.canvas, self.colors)
        self._poll_thread: Optional[threading.Thread] = None
        self._running = True

        # Запускаем фоновый опрос метрик
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True
        )
        self._poll_thread.start()

        # Привязываем события
        self.canvas.bind("<Motion>",         self._on_motion, add="+")
        self.canvas.bind("<Button-3>",       self._on_right_click, add="+")
        self.canvas.bind("<Leave>",          lambda e: self._tooltip.hide(), add="+")

    # ── Геометрия ─────────────────────────────────────────────────────────────

    def _conn_coords(self, id1: str, id2: str):
        """Экранные координаты концов линии."""
        tab = self.app.current_tab
        if not tab:
            return None
        d1, d2 = tab.devices.get(id1), tab.devices.get(id2)
        if not d1 or not d2:
            return None
        ox, oy = tab.canvas_offset
        return (d1.x + ox, d1.y + oy,
                d2.x + ox, d2.y + oy)

    def _find_connection_at(self, cx: float, cy: float):
        """Возвращает (id1, id2) линии под курсором или None."""
        tab = self.app.current_tab
        if not tab:
            return None
        for (id1, id2) in tab.connections:
            coords = self._conn_coords(id1, id2)
            if not coords:
                continue
            x1, y1, x2, y2 = coords
            if _point_to_segment_dist(cx, cy, x1, y1, x2, y2) <= self.HIT_RADIUS:
                return (id1, id2)
        return None

    # ── Рисование меток на линиях ─────────────────────────────────────────────

    def draw_labels(self):
        """
        Вызывается из _draw_connections() в main.py ПОСЛЕ рисования линий.
        Рисует метку с текущим значением SNMP прямо на середине линии.
        """
        tab = self.app.current_tab
        if not tab or not hasattr(tab, "connection_metrics"):
            return
        C = self.colors

        for (id1, id2) in tab.connections:
            key   = ConnectionMetricsManager.conn_key(id1, id2)
            mdata = tab.connection_metrics.get(key)
            if not mdata:
                continue

            coords = self._conn_coords(id1, id2)
            if not coords:
                continue
            x1, y1, x2, y2 = coords
            mx, my = _midpoint(x1, y1, x2, y2)

            val  = mdata.get("last_val")
            unit = mdata.get("unit", "")
            lbl  = mdata.get("label", "")

            if val is None:
                text  = f"⏳ {lbl}"
                color = C.get("text_dim", "#888")
            else:
                text  = f"📊 {lbl}: {val}{(' ' + unit) if unit else ''}"
                color = C.get("accent", "#ffb86c")

            # Фон-пилюля
            pad = 5
            tmp = self.canvas.create_text(mx, my, text=text,
                                           font=("Consolas", 8), anchor="center")
            bb = self.canvas.bbox(tmp)
            self.canvas.delete(tmp)
            if bb:
                self.canvas.create_rectangle(
                    bb[0] - pad, bb[1] - 2,
                    bb[2] + pad, bb[3] + 2,
                    fill=C.get("bg2", "#23233a"),
                    outline=C.get("border", "#444466"),
                    width=1,
                    tags="conn_label"
                )
            self.canvas.create_text(mx, my, text=text,
                                     font=("Consolas", 8),
                                     fill=color,
                                     anchor="center",
                                     tags="conn_label")

    # ── События мыши ─────────────────────────────────────────────────────────

    def _on_motion(self, event):
        conn = self._find_connection_at(event.x, event.y)
        if not conn:
            self._tooltip.hide()
            self.canvas.config(cursor="crosshair")
            return

        self.canvas.config(cursor="hand2")
        id1, id2 = conn
        tab = self.app.current_tab
        if not tab:
            return

        d1 = tab.devices.get(id1)
        d2 = tab.devices.get(id2)
        if not d1 or not d2:
            return

        key   = ConnectionMetricsManager.conn_key(id1, id2)
        mdata = tab.connection_metrics.get(key) if hasattr(tab, "connection_metrics") else None

        if mdata and mdata.get("last_val") is not None:
            val  = mdata["last_val"]
            unit = mdata.get("unit", "")
            lbl  = mdata.get("label", "")
            age  = int(time.time() - mdata.get("last_ts", 0))
            tip  = f"{d1.name} ↔ {d2.name}\n{lbl}: {val}{(' '+unit) if unit else ''}\nОбновлено {age}с назад"
        else:
            tip = (f"{d1.name} ({d1.ip})\n↔\n{d2.name} ({d2.ip})\n"
                   f"[ПКМ — выбрать SNMP метрику]")

        self._tooltip.show(event.x, event.y, tip)

    def _on_right_click(self, event):
        conn = self._find_connection_at(event.x, event.y)
        if not conn:
            return   # дальше обработает оригинальный handler
        id1, id2 = conn

        tab = self.app.current_tab
        if not tab:
            return

        d1 = tab.devices.get(id1)
        d2 = tab.devices.get(id2)
        if not d1 or not d2:
            return

        if not hasattr(tab, "connection_metrics"):
            tab.connection_metrics = {}

        key     = ConnectionMetricsManager.conn_key(id1, id2)
        current = tab.connection_metrics.get(key)

        # Проверяем, есть ли вообще SNMP OID у устройств
        has_oids = (
            (d1.snmp_enabled and d1.snmp_oids) or
            (d2.snmp_enabled and d2.snmp_oids)
        )

        C = self.colors
        menu = tk.Menu(self.app.root, tearoff=0,
                       bg=C.get("bg3", "#2d2d44"),
                       fg=C.get("text", "#fff"),
                       activebackground=C.get("selection", "#444466"),
                       font=("Consolas", 10))
        menu.add_command(
            label=f"🔗 {d1.name} ↔ {d2.name}",
            state="disabled"
        )
        menu.add_separator()

        if has_oids:
            menu.add_command(
                label="📊 Выбрать SNMP метрику...",
                command=lambda: self._open_picker(id1, id2, d1, d2)
            )
        else:
            menu.add_command(
                label="⚠ SNMP не настроен на устройствах",
                state="disabled"
            )

        if current:
            menu.add_command(
                label=f"🔄 Опросить сейчас: {current['label']}",
                command=lambda: self._poll_once(id1, id2)
            )
            menu.add_command(
                label="✕ Убрать метрику с линии",
                command=lambda: self._remove_metric(id1, id2),
                foreground=C.get("danger", "#ff5555")
            )

        menu.add_separator()
        menu.add_command(
            label="🗑 Удалить соединение",
            command=lambda: self._delete_connection(id1, id2),
            foreground=C.get("danger", "#ff5555")
        )

        menu.tk_popup(event.x_root, event.y_root)
        # Блокируем дальнейшую обработку правого клика
        return "break"

    # ── Пикер и управление метриками ─────────────────────────────────────────

    def _open_picker(self, id1, id2, d1, d2):
        tab = self.app.current_tab
        if not tab:
            return
        if not hasattr(tab, "connection_metrics"):
            tab.connection_metrics = {}
        key     = ConnectionMetricsManager.conn_key(id1, id2)
        current = tab.connection_metrics.get(key)

        def on_apply(metric):
            tab.connection_metrics[key] = metric
            self.app._draw_all()

        ConnectionMetricPicker(
            self.app.root, d1, d2, current, self.colors, on_apply
        )

    def _remove_metric(self, id1, id2):
        tab = self.app.current_tab
        if tab and hasattr(tab, "connection_metrics"):
            key = ConnectionMetricsManager.conn_key(id1, id2)
            tab.connection_metrics.pop(key, None)
            self.app._draw_all()

    def _delete_connection(self, id1, id2):
        tab = self.app.current_tab
        if not tab:
            return
        self.app._snapshot()
        tab.connections = [
            c for c in tab.connections
            if not (set(c) == {id1, id2})
        ]
        self._remove_metric(id1, id2)
        self.app._draw_all()

    # ── Фоновый SNMP-опрос метрик на линиях ──────────────────────────────────

    def _poll_loop(self):
        """Фоновый поток: обновляет значения метрик по расписанию."""
        while self._running:
            try:
                self._poll_all()
            except Exception as e:
                print(f"[ConnSNMP] ошибка опроса: {e}")
            time.sleep(5)

    def _poll_all(self):
        now = time.time()
        for tab in self.app.tabs.values():
            if not hasattr(tab, "connection_metrics"):
                continue
            for key, mdata in list(tab.connection_metrics.items()):
                if not mdata:
                    continue
                interval = mdata.get("interval", 30)
                if now - mdata.get("last_ts", 0) < interval:
                    continue
                # Находим устройство-источник
                id1, id2 = key.split(":", 1)
                source_name = mdata.get("source", "")
                d1 = tab.devices.get(id1)
                d2 = tab.devices.get(id2)
                dev = None
                if d1 and d1.name == source_name:
                    dev = d1
                elif d2 and d2.name == source_name:
                    dev = d2
                elif d1:
                    dev = d1
                if not dev:
                    continue

                val = self._fetch_oid(dev, mdata["oid"], mdata.get("factor", 1.0))
                mdata["last_val"] = val
                mdata["last_ts"]  = now

    def _poll_once(self, id1: str, id2: str):
        """Немедленный опрос по кнопке меню."""
        tab = self.app.current_tab
        if not tab:
            return
        key   = ConnectionMetricsManager.conn_key(id1, id2)
        mdata = tab.connection_metrics.get(key)
        if not mdata:
            return

        source_name = mdata.get("source", "")
        d1 = tab.devices.get(id1)
        d2 = tab.devices.get(id2)
        dev = None
        if d1 and d1.name == source_name:
            dev = d1
        elif d2 and d2.name == source_name:
            dev = d2
        elif d1:
            dev = d1
        if not dev:
            return

        def run():
            val = self._fetch_oid(dev, mdata["oid"], mdata.get("factor", 1.0))
            mdata["last_val"] = val
            mdata["last_ts"]  = time.time()
            self.app.root.after(0, self.app._draw_all)

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _fetch_oid(dev, oid: str, factor: float = 1.0) -> Optional[str]:
        """Синхронный SNMP GET одного OID."""
        try:
            from main import snmp_get_sync
            val, err = snmp_get_sync(
                dev.ip, dev.snmp_community,
                dev.snmp_port, dev.snmp_version, oid
            )
            if err or val is None:
                return None
            # Применяем коэффициент
            try:
                num = float(val) * factor
                return f"{num:.2f}".rstrip("0").rstrip(".")
            except ValueError:
                return val
        except Exception as e:
            print(f"[ConnSNMP] GET {oid}: {e}")
            return None

    def stop(self):
        self._running = False


# ═════════════════════════════════════════════════════════════════════════════
#  ИНСТРУКЦИЯ ПО ИНТЕГРАЦИИ В main.py
# ═════════════════════════════════════════════════════════════════════════════
#
#  1. ИМПОРТ (в начало main.py):
#     from connection_snmp import ConnectionSNMPOverlay
#
#  2. ИНИЦИАЛИЗАЦИЯ (в конце NetworkMapApp.__init__, после _build_ui()):
#     self.conn_overlay = ConnectionSNMPOverlay(self)
#     # Передаём colors в overlay:
#     self.conn_overlay.colors = COLORS
#
#  3. РИСОВАНИЕ МЕТОК (в _draw_connections(), в самом конце):
#     self.conn_overlay.draw_labels()
#
#  4. СОХРАНЕНИЕ connection_metrics в MapData.to_dict():
#     В методе to_dict() добавить:
#         "connection_metrics": getattr(self, "connection_metrics", {}),
#
#  5. ЗАГРУЗКА из файла в MapData.from_dict():
#     После self.connections = ... добавить:
#         self.connection_metrics = data.get("connection_metrics", {})
#
#  6. ОСТАНОВКА потока при закрытии (в on_close()):
#     self.conn_overlay.stop()
#
#  ИТОГ: правый клик на ЛИНИЮ (не на устройство) → меню → 
#        «📊 Выбрать SNMP метрику» → диалог → метрика отображается на линии.

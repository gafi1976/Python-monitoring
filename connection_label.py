"""
connection_label.py — Метки SNMP-метрик на линиях соединения (Zabbix-style)
============================================================================
• Zabbix-стиль прямоугольник с метриками прямо на линии
• Поддержка ↑↓ стрелок для трафика in/out
• Цвет линии зависит от статуса устройств
• Цвет бордера прямоугольника по порогу (зелёный/жёлтый/красный)
• Фоновый SNMP-опрос метрик на линиях (отдельный поток)
• Тултип при наведении мыши на линию
• Правый клик → диалог выбора метрики
"""

from __future__ import annotations
import math
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from device import Device


# ─── Геометрия ───────────────────────────────────────────────────────────────

def _dist_point_to_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / (dx*dx + dy*dy)))
    return math.hypot(px - (x1 + t*dx), py - (y1 + t*dy))


def _midpoint(x1, y1, x2, y2):
    return (x1+x2)/2, (y1+y2)/2


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def _format_bytes(val_str: str) -> str:
    """Форматирует байты в KB/MB/GB."""
    try:
        n = float(val_str)
        if n >= 1_073_741_824:
            return f"{n/1_073_741_824:.1f} GB"
        if n >= 1_048_576:
            return f"{n/1_048_576:.1f} MB"
        if n >= 1024:
            return f"{n/1024:.1f} KB"
        return f"{n:.0f} B"
    except (ValueError, TypeError):
        return str(val_str)


def _arrow_for_label(label: str) -> str:
    """Возвращает стрелку ↑ для Out-метрик, ↓ для In-метрик."""
    l = label.lower()
    if any(k in l for k in ("out", "tx", "sent", "upload")):
        return "↑"
    if any(k in l for k in ("in", "rx", "recv", "download")):
        return "↓"
    return "⬡"



# ═════════════════════════════════════════════════════════════════════════════
#  Менеджер меток соединений
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelManager:
    """
    Хранит конфигурацию меток для каждого соединения.
    Ключ: (id1, id2) — нормализованный (меньший id первым).
    Значение: список слотов:
      {"source_dev": dev_id, "oid_label": label_str,
       "last_val": str|None, "last_ts": float, "threshold_warn": float|None,
       "threshold_crit": float|None}
    """

    POLL_INTERVAL = 30   # сек между SNMP-опросами метрик линий
    HIT_RADIUS    = 10   # пикселей — чувствительность клика на линию

    def __init__(self):
        self._labels: dict[tuple, list[dict]] = {}
        self._tooltip_win: Optional[tk.Toplevel] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._app = None   # устанавливается при старте мониторинга

    @staticmethod
    def _key(id1: str, id2: str) -> tuple:
        return (id1, id2) if id1 < id2 else (id2, id1)

    def set(self, id1: str, id2: str, slots: list[dict]):
        k = self._key(id1, id2)
        if slots:
            self._labels[k] = slots
        elif k in self._labels:
            del self._labels[k]

    def get(self, id1: str, id2: str) -> list[dict]:
        return self._labels.get(self._key(id1, id2), [])

    def remove(self, id1: str, id2: str):
        self._labels.pop(self._key(id1, id2), None)

    def to_dict(self) -> dict:
        out = {}
        for k, slots in self._labels.items():
            safe = []
            for s in slots:
                safe.append({
                    "source_dev":     s.get("source_dev", ""),
                    "oid_label":      s.get("oid_label", ""),
                    "threshold_warn": s.get("threshold_warn"),
                    "threshold_crit": s.get("threshold_crit"),
                })
            out[f"{k[0]}__{k[1]}"] = safe
        return out

    def from_dict(self, data: dict):
        self._labels.clear()
        for raw_key, slots in data.items():
            parts = raw_key.split("__", 1)
            if len(parts) == 2:
                for s in slots:
                    s.setdefault("last_val", None)
                    s.setdefault("last_ts", 0.0)
                self._labels[(parts[0], parts[1])] = slots


    # ── Фоновый SNMP-опрос ────────────────────────────────────────────────────

    def start_polling(self, app):
        """Запускает фоновый поток опроса метрик линий."""
        self._app = app
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop_polling(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_all()
            except Exception as e:
                print(f"[ConnLabel] ошибка опроса: {e}")
            time.sleep(5)

    def _poll_all(self):
        if not self._app:
            return
        now = time.time()
        for tab in self._app.tabs.values():
            for (k, slots) in list(self._labels.items()):
                id1, id2 = k
                for slot in slots:
                    if now - slot.get("last_ts", 0) < self.POLL_INTERVAL:
                        continue
                    src_id = slot.get("source_dev", "")
                    dev = tab.devices.get(src_id)
                    if not dev or not dev.snmp_enabled:
                        continue
                    label = slot.get("oid_label", "")
                    # Найти OID по метке
                    oid_str = None
                    for o in dev.snmp_oids:
                        if o.get("label") == label and o.get("enabled", True):
                            oid_str = o.get("oid", "")
                            break
                    if not oid_str:
                        continue
                    try:
                        from main import snmp_get_sync
                        raw, err = snmp_get_sync(
                            dev.ip, dev.snmp_community,
                            dev.snmp_port, dev.snmp_version, oid_str
                        )
                        if not err and raw is not None:
                            slot["last_val"] = raw
                            slot["last_ts"]  = now
                    except Exception:
                        pass

    def poll_slot_now(self, slot: dict, dev):
        """Немедленный опрос одного слота (для ручного обновления)."""
        label = slot.get("oid_label", "")
        oid_str = None
        for o in dev.snmp_oids:
            if o.get("label") == label and o.get("enabled", True):
                oid_str = o.get("oid", "")
                break
        if not oid_str:
            return
        def _run():
            try:
                from main import snmp_get_sync
                raw, err = snmp_get_sync(
                    dev.ip, dev.snmp_community,
                    dev.snmp_port, dev.snmp_version, oid_str
                )
                if not err and raw is not None:
                    slot["last_val"] = raw
                    slot["last_ts"]  = time.time()
                    if self._app:
                        self._app.root.after(0, self._app._draw_all)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()


    # ── Рисование (Zabbix-стиль) ──────────────────────────────────────────────

    def draw(self, canvas: tk.Canvas,
             connections: list,
             devices: dict,
             canvas_offset: list,
             colors: dict):
        """
        Рисует линии соединений + Zabbix-стиль метки SNMP на линиях.
        Полностью заменяет _draw_connections() в main.py.
        """
        C = colors
        from device import DeviceStatus

        for (id1, id2) in connections:
            d1: Optional[Device] = devices.get(id1)
            d2: Optional[Device] = devices.get(id2)
            if not d1 or not d2:
                continue

            ox, oy = canvas_offset
            x1, y1 = d1.x + ox, d1.y + oy
            x2, y2 = d2.x + ox, d2.y + oy
            both_on = (d1.status == DeviceStatus.ONLINE and
                       d2.status == DeviceStatus.ONLINE)
            one_off  = (d1.status == DeviceStatus.OFFLINE or
                        d2.status == DeviceStatus.OFFLINE)

            # Цвет и толщина линии
            if both_on:
                line_color = C["conn_active"]
                line_width = 2
                line_dash  = ()
            elif one_off:
                line_color = C["danger"]
                line_width = 2
                line_dash  = (6, 4)
            else:
                line_color = C["connection"]
                line_width = 1
                line_dash  = (4, 4)

            canvas.create_line(
                x1, y1, x2, y2,
                fill=line_color, width=line_width, dash=line_dash,
                tags=("conn_line", f"conn_{id1}_{id2}"),
            )

            # Направление стрелки на линии
            if both_on:
                mx, my = _midpoint(x1, y1, x2, y2)
                angle = math.atan2(y2 - y1, x2 - x1)
                arrow_len = 8
                ax1 = mx - arrow_len * math.cos(angle - 0.4)
                ay1 = my - arrow_len * math.sin(angle - 0.4)
                ax2 = mx - arrow_len * math.cos(angle + 0.4)
                ay2 = my - arrow_len * math.sin(angle + 0.4)
                canvas.create_line(mx, my, ax1, ay1,
                                   fill=line_color, width=1)
                canvas.create_line(mx, my, ax2, ay2,
                                   fill=line_color, width=1)

            # Метки SNMP
            slots = self.get(id1, id2)
            if slots:
                self._draw_label_box(canvas, x1, y1, x2, y2,
                                     id1, id2, slots, devices, C)

    def _draw_label_box(self, canvas, x1, y1, x2, y2,
                        id1, id2, slots, devices, C):
        """Рисует Zabbix-стиль прямоугольник с метриками на середине линии."""
        mx, my = _midpoint(x1, y1, x2, y2)

        lines: list[tuple] = []   # (arrow, label_short, value_str, border_color)
        for slot in slots:
            src_id    = slot.get("source_dev", "")
            oid_label = slot.get("oid_label", "")
            if not src_id or not oid_label:
                continue
            src_dev = devices.get(src_id)
            if not src_dev:
                continue

            # Берём значение: сначала из кэша опроса линий, затем из snmp_last_info
            raw_val = slot.get("last_val")
            if raw_val is None:
                snmp_info = getattr(src_dev, "snmp_last_info", None) or {}
                raw_val = snmp_info.get(oid_label)

            arrow = _arrow_for_label(oid_label)

            # Форматирование значения
            if raw_val is None:
                val_str = "—"
                border_c = C.get("border", "#444466")
            else:
                # Попробуем форматировать байты для трафика
                lbl_low = oid_label.lower()
                if any(k in lbl_low for k in ("octet", "byte", "traffic", "bps")):
                    val_str = _format_bytes(raw_val)
                else:
                    try:
                        val_str = f"{float(raw_val):.1f}".rstrip("0").rstrip(".")
                    except (ValueError, TypeError):
                        val_str = str(raw_val)
                    if len(val_str) > 18:
                        val_str = val_str[:15] + "…"

                # Цвет рамки по порогу
                border_c = C.get("conn_active", "#50fa7b")
                try:
                    num = float(raw_val)
                    crit = slot.get("threshold_crit")
                    warn = slot.get("threshold_warn")
                    if crit is not None and num >= float(crit):
                        border_c = C.get("danger", "#ff5555")
                    elif warn is not None and num >= float(warn):
                        border_c = C.get("warning", "#f1fa8c")
                except (ValueError, TypeError):
                    pass

            # Укорачиваем метку
            short = oid_label if len(oid_label) <= 16 else oid_label[:14] + "…"
            lines.append((arrow, short, val_str, border_c))

        if not lines:
            return

        # Размеры блока
        pad_x, pad_y = 7, 4
        line_h = 17
        col_w  = 140
        box_w  = col_w + pad_x * 2
        box_h  = len(lines) * line_h + pad_y * 2 + 14  # +14 под заголовок

        bx1 = mx - box_w / 2
        by1 = my - box_h / 2
        bx2 = mx + box_w / 2
        by2 = my + box_h / 2

        # Определяем общий цвет рамки (worst status)
        worst = C.get("conn_active", "#50fa7b")
        for _, _, _, bc in lines:
            if bc == C.get("danger", "#ff5555"):
                worst = bc
                break
            if bc == C.get("warning", "#f1fa8c"):
                worst = bc

        # Фоновый прямоугольник
        canvas.create_rectangle(
            bx1, by1, bx2, by2,
            fill=C.get("bg2", "#23233a"),
            outline=worst,
            width=2,
            tags=("conn_label_bg",),
        )

        # Заголовок «SNMP»
        canvas.create_text(
            mx, by1 + 7,
            text="── SNMP ──",
            font=("Consolas", 7),
            fill=worst,
            anchor="center",
            tags=("conn_label_text",),
        )

        # Строки метрик
        for i, (arrow, short, val_str, bc) in enumerate(lines):
            ty = by1 + 14 + pad_y + i * line_h + line_h / 2
            # Стрелка (цветная)
            arrow_col = (C.get("online", "#50fa7b")  if arrow == "↓" else
                         C.get("accent", "#ffb86c")  if arrow == "↑" else
                         C.get("text_dim", "#b0b0d0"))
            canvas.create_text(
                bx1 + 9, ty,
                text=arrow,
                font=("Consolas", 9, "bold"),
                fill=arrow_col,
                anchor="w",
                tags=("conn_label_text",),
            )
            # Метка: название метрики
            canvas.create_text(
                bx1 + 20, ty,
                text=short + ":",
                font=("Consolas", 8),
                fill=C.get("text_dim", "#b0b0d0"),
                anchor="w",
                tags=("conn_label_text",),
            )
            # Значение
            canvas.create_text(
                bx2 - pad_x, ty,
                text=val_str,
                font=("Consolas", 8, "bold"),
                fill=bc,
                anchor="e",
                tags=("conn_label_text",),
            )


    # ── Hit-test ──────────────────────────────────────────────────────────────

    def get_connection_at(self, cx: float, cy: float,
                          connections: list,
                          devices: dict,
                          canvas_offset: list,
                          tolerance: int = 10) -> Optional[tuple]:
        """Возвращает (id1, id2) соединения под курсором или None."""
        ox, oy = canvas_offset
        for (id1, id2) in connections:
            d1 = devices.get(id1)
            d2 = devices.get(id2)
            if not d1 or not d2:
                continue
            x1, y1 = d1.x + ox, d1.y + oy
            x2, y2 = d2.x + ox, d2.y + oy
            if _dist_point_to_segment(cx, cy, x1, y1, x2, y2) <= tolerance:
                return (id1, id2)
        return None

    # ── Тултип ───────────────────────────────────────────────────────────────

    def show_tooltip(self, canvas: tk.Canvas, x: float, y: float,
                     id1: str, id2: str, devices: dict, colors: dict):
        C = colors
        canvas.delete("conn_tooltip")
        d1 = devices.get(id1)
        d2 = devices.get(id2)
        if not d1 or not d2:
            return
        slots = self.get(id1, id2)
        if slots:
            lines = [f"{d1.name} ↔ {d2.name}"]
            for s in slots:
                lbl = s.get("oid_label", "")
                val = s.get("last_val")
                src = devices.get(s.get("source_dev", ""))
                if val is None and src:
                    info = getattr(src, "snmp_last_info", None) or {}
                    val = info.get(lbl)
                age = int(time.time() - s.get("last_ts", 0))
                val_str = val if val is not None else "—"
                lines.append(f"  {_arrow_for_label(lbl)} {lbl}: {val_str}  ({age}с назад)")
            tip_text = "\n".join(lines)
        else:
            tip_text = f"{d1.name} ({d1.ip})\n↔\n{d2.name} ({d2.ip})\n[ПКМ → выбрать метрику]"

        pad = 6
        tmp = canvas.create_text(0, 0, text=tip_text, font=("Consolas", 9), anchor="nw")
        bb = canvas.bbox(tmp)
        canvas.delete(tmp)
        if not bb:
            return
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        rx, ry = x + 14, y - th//2 - pad
        canvas.create_rectangle(
            rx-pad, ry-pad, rx+tw+pad, ry+th+pad,
            fill=C.get("bg2", "#23233a"),
            outline=C.get("accent", "#ffb86c"),
            width=1, tags="conn_tooltip"
        )
        canvas.create_text(rx, ry, text=tip_text,
                           font=("Consolas", 9),
                           fill=C.get("text", "#fff"),
                           anchor="nw", tags="conn_tooltip")

    def hide_tooltip(self, canvas: tk.Canvas):
        canvas.delete("conn_tooltip")



# ═════════════════════════════════════════════════════════════════════════════
#  Диалог настройки меток соединения (улучшенный Zabbix-стиль)
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelDialog:
    """
    Диалог для назначения SNMP-метрик на линию соединения.
    Вызов: ConnectionLabelDialog(parent, (id1, id2), devices, conn_labels, colors)
    """

    def __init__(self, parent: tk.Misc,
                 conn_key: tuple,
                 devices: dict,
                 manager: ConnectionLabelManager,
                 colors: dict):
        self.conn_key = conn_key
        self.devices  = devices
        self.manager  = manager
        self.colors   = C = colors

        id1, id2  = conn_key
        self.dev1 = devices.get(id1)
        self.dev2 = devices.get(id2)

        self.win = tk.Toplevel(parent)
        self.win.title("Метки SNMP на линии соединения")
        self.win.geometry("580x520")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(False, True)

        self._slots: list[dict] = []
        self._build()
        self._load_existing()

    def _build(self):
        C = self.colors
        id1, id2 = self.conn_key
        n1 = self.dev1.name if self.dev1 else id1
        n2 = self.dev2.name if self.dev2 else id2

        hdr = tk.Frame(self.win, bg=C["bg2"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="📊 Метки SNMP на линии",
                 font=("Consolas", 12, "bold"),
                 bg=C["bg2"], fg=C["accent"], padx=14).pack(side="left")
        tk.Label(hdr, text=f"{n1}  ↔  {n2}",
                 font=("Consolas", 9),
                 bg=C["bg2"], fg=C["text_dim"], padx=8).pack(side="left")

        tk.Label(self.win,
                 text="Значение выбранной метрики будет показано прямо на линии карты.",
                 font=("Consolas", 9), wraplength=540,
                 bg=C["bg"], fg=C["text_dim"]).pack(padx=14, pady=6, anchor="w")

        # Фрейм добавления
        add_frame = tk.LabelFrame(self.win, text=" Добавить метрику ",
                                  font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"],
                                  bd=1, relief="flat",
                                  highlightthickness=1,
                                  highlightbackground=C["border"])
        add_frame.pack(fill="x", padx=14, pady=6)
        add_frame.columnconfigure(1, weight=1)

        # Источник
        tk.Label(add_frame, text="Источник:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]).grid(row=0, column=0, sticky="w", padx=8, pady=5)
        dev_names = {}
        for did in [id1, id2]:
            dev = self.devices.get(did)
            if dev:
                dev_names[did] = f"{dev.name} ({dev.ip})"
        self._dev_id_map = {v: k for k, v in dev_names.items()}
        dev_choices = list(dev_names.values())

        self._var_src = tk.StringVar(value=dev_choices[0] if dev_choices else "")
        cb_src = ttk.Combobox(add_frame, textvariable=self._var_src,
                              values=dev_choices, state="readonly", width=30,
                              font=("Consolas", 9))
        cb_src.grid(row=0, column=1, sticky="w", padx=8, pady=5)
        cb_src.bind("<<ComboboxSelected>>", self._on_src_change)

        # Метрика
        tk.Label(add_frame, text="Метрика:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]).grid(row=1, column=0, sticky="w", padx=8, pady=5)
        self._var_oid = tk.StringVar()
        self._cb_oid = ttk.Combobox(add_frame, textvariable=self._var_oid,
                                    state="readonly", width=38, font=("Consolas", 9))
        self._cb_oid.grid(row=1, column=1, sticky="w", padx=8, pady=5)
        self._refresh_oid_list()

        # Пороги
        thresh_frame = tk.Frame(add_frame, bg=C["bg"])
        thresh_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=3)
        tk.Label(thresh_frame, text="Порог ⚠:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["warning"]).pack(side="left")
        self._var_warn = tk.StringVar()
        tk.Entry(thresh_frame, textvariable=self._var_warn, width=8,
                 bg=C["bg3"], fg=C["text"], insertbackground=C["accent"],
                 relief="flat", highlightthickness=1,
                 highlightbackground=C["border"],
                 font=("Consolas", 9)).pack(side="left", padx=(4, 12))
        tk.Label(thresh_frame, text="Порог 🔴:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["danger"]).pack(side="left")
        self._var_crit = tk.StringVar()
        tk.Entry(thresh_frame, textvariable=self._var_crit, width=8,
                 bg=C["bg3"], fg=C["text"], insertbackground=C["accent"],
                 relief="flat", highlightthickness=1,
                 highlightbackground=C["border"],
                 font=("Consolas", 9)).pack(side="left", padx=4)

        tk.Button(add_frame, text="➕ Добавить",
                  command=self._add_slot,
                  bg=C["accent2"], fg="white",
                  font=("Consolas", 9, "bold"),
                  relief="flat", bd=0, padx=10, pady=4,
                  cursor="hand2").grid(row=0, column=2, rowspan=2, padx=10)

        # Список активных меток
        tk.Label(self.win, text="Активные метки на линии:",
                 font=("Consolas", 10, "bold"),
                 bg=C["bg"], fg=C["text_dim"]).pack(padx=14, pady=(8, 0), anchor="w")

        list_frame = tk.Frame(self.win, bg=C["bg"])
        list_frame.pack(fill="both", expand=True, padx=14, pady=4)
        sb = tk.Scrollbar(list_frame, bg=C["bg3"])
        sb.pack(side="right", fill="y")
        self._listbox = tk.Listbox(
            list_frame, bg=C["bg2"], fg=C["accent"],
            selectbackground=C["selection"],
            font=("Consolas", 10), relief="flat", bd=0,
            yscrollcommand=sb.set)
        self._listbox.pack(fill="both", expand=True)
        sb.config(command=self._listbox.yview)

        # Кнопки
        btn_row = tk.Frame(self.win, bg=C["bg"], pady=8)
        btn_row.pack(fill="x", padx=14)
        tk.Button(btn_row, text="❌ Удалить выбранную",
                  command=self._remove_slot,
                  bg=C["bg3"], fg=C["danger"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="🗑 Очистить все",
                  command=self._clear_all,
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="Отмена",
                  command=self.win.destroy,
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2").pack(side="right", padx=4)
        tk.Button(btn_row, text="✓ Сохранить",
                  command=self._save,
                  bg=C["accent"], fg="white",
                  font=("Consolas", 10, "bold"), relief="flat", bd=0,
                  padx=14, pady=5, cursor="hand2").pack(side="right", padx=4)

    def _get_src_dev_id(self) -> Optional[str]:
        return self._dev_id_map.get(self._var_src.get())

    def _refresh_oid_list(self):
        src_id = self._get_src_dev_id()
        dev    = self.devices.get(src_id) if src_id else None
        if dev and hasattr(dev, "snmp_oids") and dev.snmp_oids:
            oids = [o.get("label", "") for o in dev.snmp_oids if o.get("label")]
        else:
            oids = ["(нет SNMP-метрик — настройте в ⚙ устройства)"]
        self._cb_oid["values"] = oids
        self._var_oid.set(oids[0] if oids else "")

    def _on_src_change(self, _=None):
        self._refresh_oid_list()

    def _load_existing(self):
        self._slots = list(self.manager.get(self.conn_key[0], self.conn_key[1]))
        self._refresh_listbox()

    def _refresh_listbox(self):
        self._listbox.delete(0, tk.END)
        for slot in self._slots:
            src_id    = slot.get("source_dev", "")
            oid_label = slot.get("oid_label", "")
            dev       = self.devices.get(src_id)
            dev_name  = dev.name if dev else src_id
            warn = slot.get("threshold_warn")
            crit = slot.get("threshold_crit")
            thresh = ""
            if warn: thresh += f"  ⚠{warn}"
            if crit: thresh += f"  🔴{crit}"
            arr = _arrow_for_label(oid_label)
            self._listbox.insert(tk.END, f"  {arr} {dev_name}  →  {oid_label}{thresh}")

    def _add_slot(self):
        src_id    = self._get_src_dev_id()
        oid_label = self._var_oid.get().strip()
        if not src_id or not oid_label or oid_label.startswith("("):
            return
        for s in self._slots:
            if s["source_dev"] == src_id and s["oid_label"] == oid_label:
                return
        warn_val = None
        crit_val = None
        try: warn_val = float(self._var_warn.get()) if self._var_warn.get().strip() else None
        except ValueError: pass
        try: crit_val = float(self._var_crit.get()) if self._var_crit.get().strip() else None
        except ValueError: pass
        self._slots.append({
            "source_dev":     src_id,
            "oid_label":      oid_label,
            "last_val":       None,
            "last_ts":        0.0,
            "threshold_warn": warn_val,
            "threshold_crit": crit_val,
        })
        self._var_warn.set("")
        self._var_crit.set("")
        self._refresh_listbox()

    def _remove_slot(self):
        sel = self._listbox.curselection()
        if sel and 0 <= sel[0] < len(self._slots):
            del self._slots[sel[0]]
            self._refresh_listbox()

    def _clear_all(self):
        self._slots.clear()
        self._refresh_listbox()

    def _save(self):
        self.manager.set(self.conn_key[0], self.conn_key[1], self._slots)
        self.win.destroy()

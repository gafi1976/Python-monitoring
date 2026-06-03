"""
connection_label.py — Метки SNMP-метрик на линиях соединения
=============================================================
Позволяет назначить каждому соединению (id1, id2) одну или несколько
SNMP-метрик от любого из двух устройств, которые отображаются
прямо на линии на карте.

Использование в main.py:
    from connection_label import ConnectionLabelManager, ConnectionLabelDialog

    # В __init__ NetworkMapApp:
    self.conn_labels = ConnectionLabelManager()

    # В _draw_connections() — вместо текущего метода
    self.conn_labels.draw(self.canvas, self.connections,
                          self.devices, self.canvas_offset, COLORS)

    # В контекстном меню линии / правый клик:
    ConnectionLabelDialog(self.root, conn_key, self.devices, 
                          self.conn_labels, COLORS)

    # Сохранение / загрузка (добавить в to_dict / from_dict MapData):
    data["conn_labels"] = self.conn_labels.to_dict()
    self.conn_labels.from_dict(data.get("conn_labels", {}))
"""

from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Optional
import re

if TYPE_CHECKING:
    from device import Device


# ─── Форматирование значений ──────────────────────────────────────────────────

def _format_value(value: str, label: str = "") -> str:
    """Форматирует значение SNMP метрики для красивого отображения на линии."""
    lo = label.lower()
    # Трафик в байтах → KB / MB / GB
    if any(x in lo for x in ("octets", "bytes", "rxbytes", "txbytes")):
        try:
            n = float(re.sub(r"[^\d.]", "", value))
            if n >= 1_073_741_824:
                return f"{n/1_073_741_824:.1f} GB"
            elif n >= 1_048_576:
                return f"{n/1_048_576:.1f} MB"
            elif n >= 1_024:
                return f"{n/1_024:.1f} KB"
            else:
                return f"{n:.0f} B"
        except (ValueError, TypeError):
            pass
    # Скорость bps → Mbps
    if "speed" in lo:
        try:
            n = float(re.sub(r"[^\d.]", "", value))
            if n >= 1_000_000_000:
                return f"{n/1_000_000_000:.0f} Gbps"
            elif n >= 1_000_000:
                return f"{n/1_000_000:.0f} Mbps"
            elif n >= 1_000:
                return f"{n/1_000:.0f} Kbps"
        except (ValueError, TypeError):
            pass
    # ifOperStatus: 1=up, 2=down
    if "operstatus" in lo:
        if value.strip() == "1":
            return "up ✅"
        elif value.strip() == "2":
            return "down ❌"
    # Температура
    if any(x in lo for x in ("temp", "sensor")):
        try:
            n = float(re.sub(r"[^\d.]", "", value))
            return f"{n:.1f}°C"
        except (ValueError, TypeError):
            pass
    return value


# ═════════════════════════════════════════════════════════════════════════════
#  Менеджер меток соединений
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelManager:
    """
    Хранит конфигурацию меток для каждого соединения.
    Ключ: (id1, id2) — нормализованный (меньший id первым).
    Значение: список слотов {"source_dev": id, "oid_label": str}
    """

    def __init__(self):
        # conn_key → [{"source_dev": dev_id, "oid_label": label_str}, ...]
        self._labels: dict[tuple, list[dict]] = {}

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
        return {f"{k[0]}__{k[1]}": v for k, v in self._labels.items()}

    def from_dict(self, data: dict):
        self._labels.clear()
        for raw_key, slots in data.items():
            parts = raw_key.split("__", 1)
            if len(parts) == 2:
                self._labels[(parts[0], parts[1])] = slots

    # ── Drawing ───────────────────────────────────────────────────────────────

    def draw(self, canvas: tk.Canvas,
             connections: list,
             devices: dict,
             canvas_offset: list,
             colors: dict):
        """
        Полная замена _draw_connections() в main.py.
        Рисует линии + метки SNMP по середине каждой линии.
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

            # ─── Линия ────────────────────────────────────────────────────
            canvas.create_line(
                x1, y1, x2, y2,
                fill=C["conn_active"] if both_on else C["connection"],
                width=2 if both_on else 1,
                dash=() if both_on else (4, 4),
                tags=("conn_line", f"conn_{id1}_{id2}"),
            )

            # ─── Метки ────────────────────────────────────────────────────
            slots = self.get(id1, id2)
            if not slots:
                continue

            mx, my = (x1 + x2) / 2, (y1 + y2) / 2

            # Собираем строки для отображения
            lines: list[str] = []
            for slot in slots:
                src_id    = slot.get("source_dev", "")
                oid_label = slot.get("oid_label", "")
                if not src_id or not oid_label:
                    continue
                src_dev = devices.get(src_id)
                if not src_dev:
                    continue
                snmp_info = getattr(src_dev, "snmp_last_info", None) or {}
                value = snmp_info.get(oid_label)

                # Формируем короткое имя метрики
                bracket = oid_label.find(" [")
                short_label = oid_label[:bracket] if bracket != -1 else oid_label
                # Иконка для известных метрик
                icon = ""
                lo = oid_label.lower()
                if "inoctets" in lo or "rxbytes" in lo:
                    icon = "⬇"
                elif "outoctets" in lo or "txbytes" in lo:
                    icon = "⬆"
                elif "error" in lo:
                    icon = "⚠"
                elif "cpu" in lo or "processor" in lo:
                    icon = "🧠"
                elif "storage" in lo or "disk" in lo:
                    icon = "💾"
                elif "temp" in lo or "sensor" in lo:
                    icon = "🌡"
                elif "status" in lo:
                    icon = "●"

                if value is None:
                    lines.append(f"{icon}⏳ {short_label}")
                else:
                    display = _format_value(str(value), oid_label)
                    if len(display) > 22:
                        display = display[:19] + "…"
                    prefix = f"{icon} " if icon else ""
                    lines.append(f"{prefix}{short_label}: {display}")

            if not lines:
                continue

            # Фоновый прямоугольник
            line_h   = 16
            pad_x    = 6
            pad_y    = 4
            box_w    = max(len(l) for l in lines) * 7 + pad_x * 2
            box_h    = len(lines) * line_h + pad_y * 2
            bx1, by1 = mx - box_w / 2, my - box_h / 2
            bx2, by2 = mx + box_w / 2, my + box_h / 2

            canvas.create_rectangle(
                bx1, by1, bx2, by2,
                fill=C["bg2"], outline=C["border"],
                width=1,
                tags=("conn_label_bg",),
            )

            # Текстовые строки
            for i, line in enumerate(lines):
                ty = by1 + pad_y + i * line_h + line_h / 2
                canvas.create_text(
                    mx, ty,
                    text=line,
                    font=("Consolas", 8),
                    fill=C["accent"],
                    anchor="center",
                    tags=("conn_label_text",),
                )

    def get_connection_at(self, cx: float, cy: float,
                          connections: list,
                          devices: dict,
                          canvas_offset: list,
                          tolerance: int = 8) -> Optional[tuple]:
        """
        Возвращает (id1, id2) соединения под курсором,
        или None если ни одно не попало.
        Используется для правого клика на линию.
        """
        import math
        ox, oy = canvas_offset
        for (id1, id2) in connections:
            d1 = devices.get(id1)
            d2 = devices.get(id2)
            if not d1 or not d2:
                continue
            x1, y1 = d1.x + ox, d1.y + oy
            x2, y2 = d2.x + ox, d2.y + oy
            # Расстояние от точки до отрезка
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                continue
            t = max(0, min(1, ((cx - x1) * dx + (cy - y1) * dy) / length_sq))
            proj_x = x1 + t * dx
            proj_y = y1 + t * dy
            dist = math.hypot(cx - proj_x, cy - proj_y)
            if dist <= tolerance:
                return (id1, id2)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Диалог настройки меток соединения
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelDialog:
    """
    Диалог для назначения SNMP-метрик на линию соединения.

    Вызов:
        ConnectionLabelDialog(parent, (id1, id2), devices, conn_labels, colors)
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

        id1, id2   = conn_key
        self.dev1  = devices.get(id1)
        self.dev2  = devices.get(id2)

        self.win = tk.Toplevel(parent)
        self.win.title("Метки на линии соединения")
        self.win.geometry("560x480")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(False, True)

        self._build()
        self._load_existing()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        C = self.colors
        id1, id2 = self.conn_key
        n1 = self.dev1.name if self.dev1 else id1
        n2 = self.dev2.name if self.dev2 else id2

        # Header
        hdr = tk.Frame(self.win, bg=C["bg2"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr,
                 text="📊 Метки SNMP на линии",
                 font=("Consolas", 12, "bold"),
                 bg=C["bg2"], fg=C["accent"], padx=14).pack(side="left")
        tk.Label(hdr,
                 text=f"{n1}  ↔  {n2}",
                 font=("Consolas", 9),
                 bg=C["bg2"], fg=C["text_dim"], padx=8).pack(side="left")

        # Info
        tk.Label(self.win,
                 text="Выберите устройство-источник и метрику (OID) — "
                      "значение будет показано прямо на линии.",
                 font=("Consolas", 9), wraplength=520,
                 bg=C["bg"], fg=C["text_dim"]).pack(padx=14, pady=6, anchor="w")

        # Add slot frame
        add_frame = tk.LabelFrame(
            self.win, text=" Добавить метрику ",
            font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"],
            bd=1, relief="flat",
            highlightthickness=1, highlightbackground=C["border"],
        )
        add_frame.pack(fill="x", padx=14, pady=6)

        # Source device
        tk.Label(add_frame, text="Источник:",
                 font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"]
                 ).grid(row=0, column=0, sticky="w", padx=8, pady=6)

        dev_names = {}
        for did in [id1, id2]:
            dev = self.devices.get(did)
            if dev:
                dev_names[did] = f"{dev.name} ({dev.ip})"
        self._dev_id_map = {v: k for k, v in dev_names.items()}
        dev_choices = list(dev_names.values())

        self._var_src = tk.StringVar(value=dev_choices[0] if dev_choices else "")
        cb_src = ttk.Combobox(add_frame, textvariable=self._var_src,
                              values=dev_choices, state="readonly", width=28,
                              font=("Consolas", 9))
        cb_src.grid(row=0, column=1, sticky="w", padx=8, pady=6)
        cb_src.bind("<<ComboboxSelected>>", self._on_src_change)

        # OID label
        tk.Label(add_frame, text="Метрика:",
                 font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"]
                 ).grid(row=1, column=0, sticky="w", padx=8, pady=6)

        self._var_oid = tk.StringVar()
        self._cb_oid = ttk.Combobox(add_frame, textvariable=self._var_oid,
                                    state="readonly", width=36,
                                    font=("Consolas", 9))
        self._cb_oid.grid(row=1, column=1, sticky="w", padx=8, pady=6)
        self._refresh_oid_list()

        tk.Button(add_frame, text="➕ Добавить",
                  command=self._add_slot,
                  bg=C["accent2"], fg="white",
                  font=("Consolas", 9, "bold"),
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2"
                  ).grid(row=0, column=2, rowspan=2, padx=10)

        add_frame.columnconfigure(1, weight=1)

        # Current slots list
        list_hdr = tk.Frame(self.win, bg=C["bg"])
        list_hdr.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(list_hdr, text="Активные метки:",
                 font=("Consolas", 10, "bold"),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")

        list_frame = tk.Frame(self.win, bg=C["bg"])
        list_frame.pack(fill="both", expand=True, padx=14, pady=4)

        sb = tk.Scrollbar(list_frame, bg=C["bg3"])
        sb.pack(side="right", fill="y")

        self._listbox = tk.Listbox(
            list_frame,
            bg=C["bg2"], fg=C["accent"],
            selectbackground=C["selection"],
            font=("Consolas", 10),
            relief="flat", bd=0,
            yscrollcommand=sb.set,
        )
        self._listbox.pack(fill="both", expand=True)
        sb.config(command=self._listbox.yview)

        # Buttons
        btn_row = tk.Frame(self.win, bg=C["bg"], pady=8)
        btn_row.pack(fill="x", padx=14)

        tk.Button(btn_row, text="❌ Удалить выбранную",
                  command=self._remove_slot,
                  bg=C["bg3"], fg=C["danger"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2"
                  ).pack(side="left", padx=4)

        tk.Button(btn_row, text="🗑 Очистить все",
                  command=self._clear_all,
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2"
                  ).pack(side="left", padx=4)

        tk.Button(btn_row, text="✓ Сохранить",
                  command=self._save,
                  bg=C["accent"], fg="white",
                  font=("Consolas", 10, "bold"), relief="flat", bd=0,
                  padx=14, pady=5, cursor="hand2"
                  ).pack(side="right", padx=4)

        tk.Button(btn_row, text="Отмена",
                  command=self.win.destroy,
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2"
                  ).pack(side="right", padx=4)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_src_dev_id(self) -> Optional[str]:
        label = self._var_src.get()
        return self._dev_id_map.get(label)

    def _refresh_oid_list(self):
        src_id = self._get_src_dev_id()
        dev    = self.devices.get(src_id) if src_id else None
        if dev and hasattr(dev, "snmp_oids") and dev.snmp_oids:
            oids = []
            for o in dev.snmp_oids:
                label = o.get("label", "")
                if not label:
                    continue
                # Помечаем LLD-метрики иконкой
                if o.get("_lld_rule"):
                    instance = o.get("_lld_instance", "")
                    rule     = o.get("_lld_rule", "")
                    # Убираем дублирующий суффикс [instance] из label если он есть
                    bracket = label.find(" [")
                    base    = label[:bracket] if bracket != -1 else label
                    lld_icons = {
                        "net_interfaces": "🔌",
                        "storage":        "💾",
                        "processors":     "🧠",
                        "processes":      "⚙️",
                        "temperature":    "🌡️",
                    }
                    icon = lld_icons.get(rule, "📡")
                    oids.append(f"{icon} [LLD] {base} [{instance}]")
                else:
                    oids.append(label)
        else:
            oids = ["(нет SNMP-метрик — настройте в ⚙ устройства)"]
        self._cb_oid["values"] = oids
        self._var_oid.set(oids[0] if oids else "")

    def _get_real_label(self, display_label: str, src_id: str) -> str:
        """Конвертирует отображаемый label обратно в реальный label из snmp_oids."""
        dev = self.devices.get(src_id)
        if not dev or not hasattr(dev, "snmp_oids"):
            return display_label
        # Для LLD-меток — ищем по базовому имени и instance
        if display_label.startswith(("🔌 [LLD]", "💾 [LLD]", "🧠 [LLD]",
                                      "⚙️ [LLD]", "🌡️ [LLD]", "📡 [LLD]")):
            # Формат: "🔌 [LLD] ifInOctets [eth0]"
            # Ищем соответствующий label в snmp_oids
            try:
                without_prefix = display_label.split("] ", 1)[1]  # "ifInOctets [eth0]"
            except IndexError:
                return display_label
            for o in dev.snmp_oids:
                if o.get("label", "") == without_prefix:
                    return without_prefix
                # Также проверим что base + instance совпадает
                bracket = o.get("label", "").find(" [")
                base_o = o.get("label", "")[:bracket] if bracket != -1 else o.get("label", "")
                inst_o = o.get("_lld_instance", "")
                if without_prefix == f"{base_o} [{inst_o}]":
                    return o.get("label", display_label)
            return without_prefix
        return display_label

    def _on_src_change(self, _event=None):
        self._refresh_oid_list()

    def _load_existing(self):
        """Загружает уже сохранённые слоты."""
        self._slots: list[dict] = list(
            self.manager.get(self.conn_key[0], self.conn_key[1])
        )
        self._refresh_listbox()

    def _refresh_listbox(self):
        self._listbox.delete(0, tk.END)
        for slot in self._slots:
            src_id    = slot.get("source_dev", "")
            oid_label = slot.get("oid_label", "")
            dev       = self.devices.get(src_id)
            dev_name  = dev.name if dev else src_id
            self._listbox.insert(tk.END, f"  📡 {dev_name}  →  {oid_label}")

    def _add_slot(self):
        src_id    = self._get_src_dev_id()
        oid_label = self._var_oid.get().strip()
        if not src_id or not oid_label:
            return
        if oid_label.startswith("("):
            return  # Placeholder — нет метрик
        # Конвертируем display-label в реальный label
        real_label = self._get_real_label(oid_label, src_id)
        # Дедупликация
        for s in self._slots:
            if s["source_dev"] == src_id and s["oid_label"] == real_label:
                return
        self._slots.append({"source_dev": src_id, "oid_label": real_label})
        self._refresh_listbox()

    def _remove_slot(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._slots):
            del self._slots[idx]
            self._refresh_listbox()

    def _clear_all(self):
        self._slots.clear()
        self._refresh_listbox()

    def _save(self):
        self.manager.set(self.conn_key[0], self.conn_key[1], self._slots)
        self.win.destroy()

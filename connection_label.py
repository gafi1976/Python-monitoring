"""
connection_label.py — Автономные SNMP/LLD метрики на линиях соединения
=======================================================================
• Каждый слот хранит OID + IP + community + interval → значение опрашивается
  НЕЗАВИСИМО от мониторинга устройств (свой фоновый поток)
• Поддержка LLD-метрик: интерфейсный трафик, ошибки, статус и т.д.
• Диалог показывает как обычные OID, так и LLD-метрики с иконками
• Умное форматирование: байты→KB/MB/GB, bps→Mbps, статус→up/down
"""

from __future__ import annotations
import math
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from device import Device


# ─── Форматирование значений ─────────────────────────────────────────────────

def _format_value(value: str, label: str = "") -> str:
    lo = label.lower()
    if any(x in lo for x in ("octets", "bytes", "rxbytes", "txbytes")):
        try:
            n = float(re.sub(r"[^\d.]", "", value))
            if n >= 1_073_741_824: return f"{n/1_073_741_824:.1f} GB"
            elif n >= 1_048_576:   return f"{n/1_048_576:.1f} MB"
            elif n >= 1_024:       return f"{n/1_024:.1f} KB"
            else:                  return f"{n:.0f} B"
        except (ValueError, TypeError):
            pass
    if "speed" in lo:
        try:
            n = float(re.sub(r"[^\d.]", "", value))
            if n >= 1_000_000_000: return f"{n/1_000_000_000:.0f} Gbps"
            elif n >= 1_000_000:   return f"{n/1_000_000:.0f} Mbps"
            elif n >= 1_000:       return f"{n/1_000:.0f} Kbps"
        except (ValueError, TypeError):
            pass
    if "operstatus" in lo:
        return "up ✅" if value.strip() == "1" else ("down ❌" if value.strip() == "2" else value)
    if any(x in lo for x in ("temp", "sensor")):
        try:
            return f"{float(re.sub(r'[^\d.]', '', value)):.1f}°C"
        except (ValueError, TypeError):
            pass
    return value



def _metric_icon(label: str) -> str:
    lo = label.lower()
    if "inoctets" in lo or "rxbytes" in lo:  return "⬇"
    if "outoctets" in lo or "txbytes" in lo: return "⬆"
    if "error" in lo:                         return "⚠"
    if "cpu" in lo or "processor" in lo:      return "🧠"
    if "storage" in lo or "disk" in lo:       return "💾"
    if "temp" in lo or "sensor" in lo:        return "🌡"
    if "operstatus" in lo:                    return "●"
    if "speed" in lo:                         return "⚡"
    return "📊"


LLD_ICONS = {
    "net_interfaces": "🔌",
    "storage":        "💾",
    "processors":     "🧠",
    "processes":      "⚙",
    "temperature":    "🌡",
}


# ═════════════════════════════════════════════════════════════════════════════
#  Slot: одна метрика на одной линии
# ═════════════════════════════════════════════════════════════════════════════
#
#  Структура слота (dict):
#    source_dev  : str   — dev_id устройства-источника
#    oid_label   : str   — метка для отображения (короткое имя)
#    oid         : str   — числовой OID для опроса
#    unit        : str   — единица измерения
#    factor      : float — множитель значения
#    interval    : int   — интервал опроса в секундах (по умолчанию 30)
#    _last_val   : str|None  — последнее полученное значение (runtime, не сохраняется)
#    _last_ts    : float     — время последнего опроса (runtime)
#    _error      : str|None  — текст ошибки если опрос не удался (runtime)



# ═════════════════════════════════════════════════════════════════════════════
#  ConnectionLabelManager — хранилище + автономный SNMP-опрос
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelManager:
    """
    Хранит метки для каждого соединения и автономно опрашивает SNMP
    независимо от основного мониторинга устройств.
    """

    def __init__(self):
        # (id1,id2) нормализованный → [slot, ...]
        self._labels: dict[tuple, list[dict]] = {}
        # Реестр устройств — передаётся из NetworkMapApp при каждом опросе
        self._devices_ref: dict = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Ключ ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _key(id1: str, id2: str) -> tuple:
        return (id1, id2) if id1 < id2 else (id2, id1)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def set(self, id1: str, id2: str, slots: list[dict]):
        k = self._key(id1, id2)
        with self._lock:
            if slots:
                # Сбрасываем runtime-поля при обновлении
                for s in slots:
                    s.setdefault("_last_val", None)
                    s.setdefault("_last_ts",  0.0)
                    s.setdefault("_error",    None)
                self._labels[k] = slots
            elif k in self._labels:
                del self._labels[k]

    def get(self, id1: str, id2: str) -> list[dict]:
        with self._lock:
            return list(self._labels.get(self._key(id1, id2), []))

    def remove(self, id1: str, id2: str):
        with self._lock:
            self._labels.pop(self._key(id1, id2), None)

    def update_devices(self, devices: dict):
        """Обновляет ссылку на словарь устройств (вызывать из main.py)."""
        self._devices_ref = devices


    # ── Сериализация ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        result = {}
        with self._lock:
            for (a, b), slots in self._labels.items():
                # Сохраняем только постоянные поля, без runtime (_last_val, etc.)
                clean = []
                for s in slots:
                    clean.append({k: v for k, v in s.items()
                                  if not k.startswith("_")})
                result[f"{a}__{b}"] = clean
        return result

    def from_dict(self, data: dict):
        with self._lock:
            self._labels.clear()
            for raw_key, slots in data.items():
                parts = raw_key.split("__", 1)
                if len(parts) == 2:
                    for s in slots:
                        s.setdefault("oid",      "")
                        s.setdefault("unit",     "")
                        s.setdefault("factor",   1.0)
                        s.setdefault("interval", 30)
                        s["_last_val"] = None
                        s["_last_ts"]  = 0.0
                        s["_error"]    = None
                    self._labels[(parts[0], parts[1])] = slots

    # ── Фоновый поток опроса ─────────────────────────────────────────────────

    def start_polling(self):
        """Запустить автономный опрос. Вызывать один раз при старте приложения."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="ConnLabelPoller")
        self._thread.start()

    def stop_polling(self):
        """Остановить опрос при закрытии приложения."""
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_all()
            except Exception as e:
                print(f"[ConnLabel] poll error: {e}")
            time.sleep(2)

    def _poll_all(self):
        now = time.time()
        with self._lock:
            items = [(k, list(slots)) for k, slots in self._labels.items()]

        for (id1, id2), slots in items:
            for slot in slots:
                interval = slot.get("interval", 30)
                if now - slot.get("_last_ts", 0) < interval:
                    continue
                oid = slot.get("oid", "").strip()
                if not oid:
                    continue
                # Берём параметры устройства-источника
                src_id = slot.get("source_dev", "")
                dev = self._devices_ref.get(src_id)
                if not dev:
                    slot["_error"] = "нет устройства"
                    continue
                val, err = self._snmp_get(
                    dev.ip,
                    getattr(dev, "snmp_community", "public"),
                    getattr(dev, "snmp_port", 161),
                    getattr(dev, "snmp_version", "2c"),
                    oid,
                )
                factor = slot.get("factor", 1.0)
                if err or val is None:
                    slot["_error"]    = err or "нет ответа"
                    slot["_last_val"] = None
                else:
                    slot["_error"] = None
                    try:
                        num = float(val) * factor
                        slot["_last_val"] = (
                            str(int(num)) if num == int(num) else f"{num:.2f}"
                        )
                    except (ValueError, TypeError):
                        slot["_last_val"] = val
                slot["_last_ts"] = now


    @staticmethod
    def _snmp_get(ip, community, port, version, oid,
                  timeout=3.0) -> tuple[Optional[str], Optional[str]]:
        """Синхронный SNMP GET без зависимости от main.py."""
        try:
            import asyncio
            from pysnmp.hlapi.v3arch.asyncio import (
                get_cmd, SnmpEngine, CommunityData,
                UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity,
            )

            async def _get():
                engine = SnmpEngine()
                try:
                    mp = 0 if version == "1" else 1
                    err_ind, err_st, _, var_binds = await get_cmd(
                        engine,
                        CommunityData(community, mpModel=mp),
                        await UdpTransportTarget.create(
                            (ip, port), timeout=timeout, retries=0),
                        ContextData(),
                        ObjectType(ObjectIdentity(oid)),
                    )
                    if err_ind: return None, str(err_ind)
                    if err_st:  return None, str(err_st)
                    for _, val in var_binds:
                        return str(val), None
                    return None, "нет данных"
                finally:
                    engine.close_dispatcher()

            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_get())
            finally:
                loop.close()
        except ImportError:
            return None, "pysnmp не установлен"
        except Exception as e:
            return None, str(e)

    # ── Рисование ────────────────────────────────────────────────────────────

    def draw(self, canvas: tk.Canvas,
             connections: list,
             devices: dict,
             canvas_offset: list,
             colors: dict):
        """Рисует линии соединений + автономные SNMP-метки."""
        C = colors
        from device import DeviceStatus

        # Обновляем ссылку на устройства для фонового опроса
        self._devices_ref = devices

        for (id1, id2) in connections:
            d1 = devices.get(id1)
            d2 = devices.get(id2)
            if not d1 or not d2:
                continue

            ox, oy = canvas_offset
            x1, y1 = d1.x + ox, d1.y + oy
            x2, y2 = d2.x + ox, d2.y + oy
            both_on = (d1.status == DeviceStatus.ONLINE and
                       d2.status == DeviceStatus.ONLINE)

            # Линия
            canvas.create_line(
                x1, y1, x2, y2,
                fill=C["conn_active"] if both_on else C["connection"],
                width=2 if both_on else 1,
                dash=() if both_on else (4, 4),
                tags=("conn_line", f"conn_{id1}_{id2}"),
            )

            # Метки
            slots = self.get(id1, id2)
            if not slots:
                continue

            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            lines: list[tuple[str, str]] = []  # (text, color)

            for slot in slots:
                lbl   = slot.get("oid_label", slot.get("oid", "?"))
                bracket = lbl.find(" [")
                short = lbl[:bracket] if bracket != -1 else lbl
                icon  = _metric_icon(lbl)
                val   = slot.get("_last_val")
                err   = slot.get("_error")
                last_ts = slot.get("_last_ts", 0)
                age   = int(time.time() - last_ts) if last_ts else None

                if err:
                    lines.append((f"{icon} {short}: ⚠", C.get("danger", "#ff5555")))
                elif val is None:
                    lines.append((f"{icon} {short}: ⏳", C.get("text_dim", "#888")))
                else:
                    display = _format_value(val, lbl)
                    age_txt = f" [{age}с]" if age is not None and age < 999 else ""
                    lines.append((f"{icon} {short}: {display}{age_txt}",
                                  C.get("accent", "#ffb86c")))

            if not lines:
                continue

            # Фон-пилюля
            line_h = 16
            pad_x, pad_y = 6, 4
            box_w = max(len(t) for t, _ in lines) * 7 + pad_x * 2
            box_h = len(lines) * line_h + pad_y * 2
            bx1 = mx - box_w / 2
            by1 = my - box_h / 2

            canvas.create_rectangle(
                bx1, by1, bx1 + box_w, by1 + box_h,
                fill=C["bg2"], outline=C["border"], width=1,
                tags=("conn_label_bg",),
            )
            for i, (text, color) in enumerate(lines):
                canvas.create_text(
                    mx, by1 + pad_y + i * line_h + line_h / 2,
                    text=text, font=("Consolas", 8),
                    fill=color, anchor="center",
                    tags=("conn_label_text",),
                )


    def get_connection_at(self, cx: float, cy: float,
                          connections: list,
                          devices: dict,
                          canvas_offset: list,
                          tolerance: int = 8) -> Optional[tuple]:
        ox, oy = canvas_offset
        for (id1, id2) in connections:
            d1, d2 = devices.get(id1), devices.get(id2)
            if not d1 or not d2:
                continue
            x1, y1 = d1.x + ox, d1.y + oy
            x2, y2 = d2.x + ox, d2.y + oy
            dx, dy = x2 - x1, y2 - y1
            lsq = dx * dx + dy * dy
            if lsq == 0:
                continue
            t = max(0, min(1, ((cx - x1) * dx + (cy - y1) * dy) / lsq))
            dist = math.hypot(cx - (x1 + t * dx), cy - (y1 + t * dy))
            if dist <= tolerance:
                return (id1, id2)
        return None



# ═════════════════════════════════════════════════════════════════════════════
#  Диалог настройки меток соединения
# ═════════════════════════════════════════════════════════════════════════════

class ConnectionLabelDialog:
    """
    Диалог настройки автономных SNMP-меток на линии соединения.
    Слот теперь хранит OID напрямую — опрос полностью независим.
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
        self.win.title("Метки на линии соединения")
        self.win.geometry("640x540")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(True, True)

        self._slots: list[dict] = []
        self._build()
        self._load_existing()

    # ── Построение UI ────────────────────────────────────────────────────────

    def _build(self):
        C = self.colors
        id1, id2 = self.conn_key
        n1 = self.dev1.name if self.dev1 else id1
        n2 = self.dev2.name if self.dev2 else id2

        # Заголовок
        hdr = tk.Frame(self.win, bg=C["bg2"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="📡 Автономные SNMP-метки на линии",
                 font=("Consolas", 12, "bold"),
                 bg=C["bg2"], fg=C["accent"], padx=14).pack(side="left")
        tk.Label(hdr, text=f"{n1}  ↔  {n2}",
                 font=("Consolas", 9),
                 bg=C["bg2"], fg=C["text_dim"], padx=8).pack(side="left")

        # Пояснение
        tk.Label(self.win,
                 text="Метрики опрашиваются автономно — независимо от мониторинга устройств.\n"
                      "Поддерживаются обычные OID и LLD-метрики (интерфейсы, диски, CPU).",
                 font=("Consolas", 9), wraplength=600, justify="left",
                 bg=C["bg"], fg=C["text_dim"]).pack(padx=14, pady=6, anchor="w")

        # Фрейм добавления метрики
        add_frame = tk.LabelFrame(
            self.win, text=" ➕ Добавить метрику ",
            font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"],
            bd=1, relief="flat",
            highlightthickness=1, highlightbackground=C["border"],
        )
        add_frame.pack(fill="x", padx=14, pady=6)
        add_frame.columnconfigure(1, weight=1)

        # Строка 0: Источник
        tk.Label(add_frame, text="Источник:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]
                 ).grid(row=0, column=0, sticky="w", padx=8, pady=5)

        dev_names = {}
        for did in [self.conn_key[0], self.conn_key[1]]:
            dev = self.devices.get(did)
            if dev:
                dev_names[did] = f"{dev.name}  ({dev.ip})"
        self._dev_id_map = {v: k for k, v in dev_names.items()}
        dev_choices = list(dev_names.values())

        self._var_src = tk.StringVar(value=dev_choices[0] if dev_choices else "")
        cb_src = ttk.Combobox(add_frame, textvariable=self._var_src,
                              values=dev_choices, state="readonly", width=32,
                              font=("Consolas", 9))
        cb_src.grid(row=0, column=1, sticky="w", padx=8, pady=5)
        cb_src.bind("<<ComboboxSelected>>", self._on_src_change)

        # Строка 1: Метрика
        tk.Label(add_frame, text="Метрика:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]
                 ).grid(row=1, column=0, sticky="w", padx=8, pady=5)

        self._var_oid_display = tk.StringVar()
        self._cb_oid = ttk.Combobox(add_frame, textvariable=self._var_oid_display,
                                    state="readonly", width=46,
                                    font=("Consolas", 9))
        self._cb_oid.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
        self._cb_oid.bind("<<ComboboxSelected>>", self._on_oid_select)

        # Строка 2: OID + интервал
        mid_row = tk.Frame(add_frame, bg=C["bg"])
        mid_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=4)

        tk.Label(mid_row, text="OID:", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._var_oid_raw = tk.StringVar()
        tk.Entry(mid_row, textvariable=self._var_oid_raw,
                 font=("Consolas", 9), width=34,
                 bg=C["bg3"], fg=C["text_dim"],
                 insertbackground=C["accent"],
                 relief="flat", highlightthickness=1,
                 highlightbackground=C["border"],
                 state="readonly"
                 ).pack(side="left", padx=(4, 12))

        tk.Label(mid_row, text="Интервал (сек):", font=("Consolas", 9),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._var_interval = tk.StringVar(value="30")
        tk.Entry(mid_row, textvariable=self._var_interval,
                 font=("Consolas", 9), width=5,
                 bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", highlightthickness=1,
                 highlightbackground=C["border"]
                 ).pack(side="left", padx=4)

        tk.Button(add_frame, text="➕ Добавить",
                  command=self._add_slot,
                  bg=C["accent2"], fg="white",
                  font=("Consolas", 9, "bold"),
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2"
                  ).grid(row=0, column=2, rowspan=2, padx=10)

        self._refresh_oid_list()

        # Список активных меток
        tk.Label(self.win, text="Активные метки (опрашиваются автономно):",
                 font=("Consolas", 10, "bold"),
                 bg=C["bg"], fg=C["text_dim"]).pack(padx=14, pady=(8, 0), anchor="w")

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

        tk.Button(btn_row, text="✓ Сохранить",
                  command=self._save,
                  bg=C["accent"], fg="white",
                  font=("Consolas", 10, "bold"), relief="flat", bd=0,
                  padx=14, pady=5, cursor="hand2").pack(side="right", padx=4)

        tk.Button(btn_row, text="Отмена",
                  command=self.win.destroy,
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2").pack(side="right", padx=4)


    # ── Вспомогательные методы диалога ───────────────────────────────────────

    def _get_src_dev_id(self) -> Optional[str]:
        return self._dev_id_map.get(self._var_src.get())

    def _build_options(self, dev) -> list[dict]:
        """Строит список опций из snmp_oids устройства."""
        options = []
        if not dev or not getattr(dev, "snmp_oids", None):
            return options
        for o in dev.snmp_oids:
            lbl  = o.get("label", "")
            oid  = o.get("oid",   "")
            if not lbl or not oid:
                continue
            rule = o.get("_lld_rule", "")
            inst = o.get("_lld_instance", "")
            if rule:
                icon = LLD_ICONS.get(rule, "📡")
                bracket = lbl.find(" [")
                base = lbl[:bracket] if bracket != -1 else lbl
                display = f"{icon} [LLD] {base} [{inst}]"
            else:
                display = f"  {lbl}"
            options.append({
                "display": display,
                "label":   lbl,
                "oid":     oid,
                "unit":    o.get("unit", ""),
                "factor":  o.get("factor", 1.0),
            })
        return options

    def _refresh_oid_list(self):
        src_id = self._get_src_dev_id()
        dev    = self.devices.get(src_id) if src_id else None
        self._options = self._build_options(dev)
        if self._options:
            choices = [o["display"] for o in self._options]
        else:
            choices = ["(нет SNMP-метрик — сначала настройте OID или запустите LLD)"]
        self._cb_oid["values"] = choices
        self._var_oid_display.set(choices[0] if choices else "")
        self._on_oid_select()

    def _on_src_change(self, _=None):
        self._refresh_oid_list()

    def _on_oid_select(self, _=None):
        disp = self._var_oid_display.get()
        opt  = next((o for o in getattr(self, "_options", [])
                     if o["display"] == disp), None)
        self._var_oid_raw.set(opt["oid"] if opt else "")

    def _load_existing(self):
        self._slots = list(self.manager.get(self.conn_key[0], self.conn_key[1]))
        self._refresh_listbox()

    def _refresh_listbox(self):
        self._listbox.delete(0, tk.END)
        for slot in self._slots:
            src_id  = slot.get("source_dev", "")
            lbl     = slot.get("oid_label", slot.get("oid", "?"))
            dev     = self.devices.get(src_id)
            dname   = dev.name if dev else src_id
            ivl     = slot.get("interval", 30)
            icon    = _metric_icon(lbl)
            bracket = lbl.find(" [")
            short   = lbl[:bracket] if bracket != -1 else lbl
            val     = slot.get("_last_val")
            val_txt = f"  → {_format_value(val, lbl)}" if val else "  → ⏳"
            self._listbox.insert(
                tk.END,
                f"  {icon} {dname}  ›  {short}  [{ivl}с]{val_txt}"
            )

    def _add_slot(self):
        src_id = self._get_src_dev_id()
        disp   = self._var_oid_display.get()
        if not src_id or not disp or disp.startswith("("):
            return
        opt = next((o for o in getattr(self, "_options", [])
                    if o["display"] == disp), None)
        if not opt:
            return
        try:
            interval = max(5, int(self._var_interval.get()))
        except ValueError:
            interval = 30
        # Дедупликация
        for s in self._slots:
            if s["source_dev"] == src_id and s["oid"] == opt["oid"]:
                return
        self._slots.append({
            "source_dev": src_id,
            "oid_label":  opt["label"],
            "oid":        opt["oid"],
            "unit":       opt["unit"],
            "factor":     opt["factor"],
            "interval":   interval,
            "_last_val":  None,
            "_last_ts":   0.0,
            "_error":     None,
        })
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

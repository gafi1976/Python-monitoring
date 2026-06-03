"""
snmp_lld.py — SNMP Low-Level Discovery (LLD) для Network Map
=============================================================
Автоматически обнаруживает динамические элементы мониторинга:
  • Сетевые интерфейсы (ifTable)
  • Диски / файловые системы (hrStorageTable)
  • Процессоры (hrProcessorTable)
  • Запущенные процессы (hrSWRunTable)
  • Температурные датчики (entPhysicalTable)

Каждый «правило LLD» хранится в device.snmp_lld_rules.
После обнаружения элементы превращаются в OID-записи в device.snmp_oids.
"""

from __future__ import annotations
import asyncio
import time
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from device import Device

# ── pysnmp ───────────────────────────────────────────────────────────────────
try:
    from pysnmp.hlapi.v3arch.asyncio import (
        get_cmd, next_cmd, bulk_cmd,
        SnmpEngine, CommunityData, UdpTransportTarget,
        ContextData, ObjectType, ObjectIdentity,
    )
    SNMP_OK = True
except Exception as _e:
    SNMP_OK = False
    print(f"[LLD] pysnmp недоступен: {_e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Встроенные шаблоны LLD
# ═════════════════════════════════════════════════════════════════════════════

LLD_TEMPLATES: dict[str, dict] = {
    "net_interfaces": {
        "label":    "Сетевые интерфейсы",
        "icon":     "🔌",
        "walk_oid": "1.3.6.1.2.1.2.2.1.2",   # ifDescr
        # Дополнительные атрибуты объекта → макросы Zabbix-стиля
        "macros": [
            {"macro": "{#IFINDEX}",   "oid_tpl": None,                          "note": "Индекс интерфейса"},
            {"macro": "{#IFNAME}",    "oid_tpl": "1.3.6.1.2.1.2.2.1.2.{idx}",  "note": "Имя (ifDescr)"},
            {"macro": "{#IFALIAS}",   "oid_tpl": "1.3.6.1.2.1.31.1.1.1.18.{idx}", "note": "Псевдоним (ifAlias)"},
            {"macro": "{#IFTYPE}",    "oid_tpl": "1.3.6.1.2.1.2.2.1.3.{idx}",  "note": "Тип интерфейса"},
            {"macro": "{#IFSPEED}",   "oid_tpl": "1.3.6.1.2.1.2.2.1.5.{idx}",  "note": "Скорость (bps)"},
            {"macro": "{#IFADMINSTATUS}", "oid_tpl": "1.3.6.1.2.1.2.2.1.7.{idx}", "note": "Admin статус"},
            {"macro": "{#IFOPERSTATUS}", "oid_tpl": "1.3.6.1.2.1.2.2.1.8.{idx}", "note": "Oper статус"},
        ],
        "metrics": [
            {"label": "ifInOctets [{iface}]",    "oid_tpl": "1.3.6.1.2.1.2.2.1.10.{idx}", "unit": "B",   "factor": 1.0},
            {"label": "ifOutOctets [{iface}]",   "oid_tpl": "1.3.6.1.2.1.2.2.1.16.{idx}", "unit": "B",   "factor": 1.0},
            {"label": "ifInErrors [{iface}]",    "oid_tpl": "1.3.6.1.2.1.2.2.1.14.{idx}", "unit": "",    "factor": 1.0},
            {"label": "ifOutErrors [{iface}]",   "oid_tpl": "1.3.6.1.2.1.2.2.1.20.{idx}", "unit": "",    "factor": 1.0},
            {"label": "ifOperStatus [{iface}]",  "oid_tpl": "1.3.6.1.2.1.2.2.1.8.{idx}",  "unit": "",    "factor": 1.0},
            {"label": "ifSpeed [{iface}]",       "oid_tpl": "1.3.6.1.2.1.2.2.1.5.{idx}",  "unit": "bps", "factor": 1.0},
        ],
    },
    "storage": {
        "label":    "Диски / файловые системы",
        "icon":     "💾",
        "walk_oid": "1.3.6.1.2.1.25.2.3.1.3",  # hrStorageDescr
        "macros": [
            {"macro": "{#SNMPINDEX}",     "oid_tpl": None,                              "note": "Индекс"},
            {"macro": "{#STORAGEDESCR}",  "oid_tpl": "1.3.6.1.2.1.25.2.3.1.3.{idx}",  "note": "Описание раздела"},
            {"macro": "{#STORAGETYPE}",   "oid_tpl": "1.3.6.1.2.1.25.2.3.1.2.{idx}",  "note": "Тип хранилища"},
            {"macro": "{#ALLOCATIONUNITS}","oid_tpl": "1.3.6.1.2.1.25.2.3.1.4.{idx}", "note": "Размер блока (байт)"},
        ],
        "metrics": [
            {"label": "hrStorageSize [{disk}]",  "oid_tpl": "1.3.6.1.2.1.25.2.3.1.5.{idx}", "unit": "alloc-units", "factor": 1.0},
            {"label": "hrStorageUsed [{disk}]",  "oid_tpl": "1.3.6.1.2.1.25.2.3.1.6.{idx}", "unit": "alloc-units", "factor": 1.0},
        ],
    },
    "processors": {
        "label":    "Процессоры",
        "icon":     "🧠",
        "walk_oid": "1.3.6.1.2.1.25.3.3.1.2",
        "macros": [
            {"macro": "{#SNMPINDEX}", "oid_tpl": None,                               "note": "Индекс CPU"},
            {"macro": "{#CPULOAD}",  "oid_tpl": "1.3.6.1.2.1.25.3.3.1.2.{idx}",    "note": "Загрузка (%)"},
        ],
        "metrics": [
            {"label": "CPU Load [{idx}] %", "oid_tpl": "1.3.6.1.2.1.25.3.3.1.2.{idx}", "unit": "%", "factor": 1.0},
        ],
    },
    "processes": {
        "label":    "Запущенные процессы",
        "icon":     "⚙️",
        "walk_oid": "1.3.6.1.2.1.25.4.2.1.2",  # hrSWRunName
        "macros": [
            {"macro": "{#SNMPINDEX}",  "oid_tpl": None,                               "note": "Индекс процесса"},
            {"macro": "{#SWRUNNAME}",  "oid_tpl": "1.3.6.1.2.1.25.4.2.1.2.{idx}",   "note": "Имя процесса"},
            {"macro": "{#SWRUNPATH}",  "oid_tpl": "1.3.6.1.2.1.25.4.2.1.4.{idx}",   "note": "Путь к файлу"},
            {"macro": "{#SWRUNTYPE}",  "oid_tpl": "1.3.6.1.2.1.25.4.2.1.6.{idx}",   "note": "Тип процесса"},
        ],
        "metrics": [
            {"label": "ProcStatus [{proc}]", "oid_tpl": "1.3.6.1.2.1.25.4.2.1.7.{idx}",  "unit": "",   "factor": 1.0},
            {"label": "ProcCPU [{proc}]",    "oid_tpl": "1.3.6.1.2.1.25.4.2.1.11.{idx}", "unit": "cs", "factor": 1.0},
        ],
    },
    "temperature": {
        "label":    "Температурные датчики",
        "icon":     "🌡️",
        "walk_oid": "1.3.6.1.2.1.99.1.1.1.4",
        "macros": [
            {"macro": "{#SNMPINDEX}",      "oid_tpl": None,                              "note": "Индекс датчика"},
            {"macro": "{#SENSORVALUE}",    "oid_tpl": "1.3.6.1.2.1.99.1.1.1.4.{idx}",  "note": "Значение датчика"},
            {"macro": "{#SENSORTYPE}",     "oid_tpl": "1.3.6.1.2.1.99.1.1.1.1.{idx}",  "note": "Тип датчика"},
            {"macro": "{#SENSORDATASCALE}","oid_tpl": "1.3.6.1.2.1.99.1.1.1.2.{idx}",  "note": "Масштаб"},
        ],
        "metrics": [
            {"label": "SensorTemp [{idx}]", "oid_tpl": "1.3.6.1.2.1.99.1.1.1.4.{idx}", "unit": "°C", "factor": 1.0},
        ],
    },
}


# ═════════════════════════════════════════════════════════════════════════════
#  Низкоуровневые SNMP-walk функции
# ═════════════════════════════════════════════════════════════════════════════

def _snmp_walk_sync(ip: str, community: str, port: int, version: str,
                    base_oid: str, timeout: float = 5.0) -> list[tuple[str, str]]:
    """Синхронный SNMP walk. Возвращает [(oid_suffix, value), ...]."""
    if not SNMP_OK:
        return []

    async def _walk():
        results: list[tuple[str, str]] = []
        engine = SnmpEngine()
        mp = 0 if version == "1" else 1
        try:
            transport = await UdpTransportTarget.create(
                (ip, port), timeout=timeout, retries=1
            )
            current_obj = ObjectType(ObjectIdentity(base_oid))
            for _ in range(256):  # максимум 256 шагов
                err_ind, err_st, _, var_binds = await next_cmd(
                    engine,
                    CommunityData(community, mpModel=mp),
                    transport,
                    ContextData(),
                    current_obj,
                    lexicographicMode=False,
                )
                if err_ind or err_st:
                    break
                if not var_binds:
                    break
                oid, val = var_binds[0]
                oid_str = str(oid)
                if not oid_str.startswith(base_oid):
                    break
                suffix = oid_str[len(base_oid):].lstrip(".")
                results.append((suffix, str(val)))
                # Переходим к следующему OID
                current_obj = ObjectType(ObjectIdentity(oid_str))
        finally:
            engine.close_dispatcher()
        return results

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_walk())
    except Exception as exc:
        print(f"[LLD walk] {ip} {base_oid}: {exc}")
        return []
    finally:
        loop.close()


def _snmp_get_sync(ip: str, community: str, port: int, version: str,
                   oid: str, timeout: float = 3.0) -> Optional[str]:
    if not SNMP_OK:
        return None

    async def _get():
        engine = SnmpEngine()
        try:
            mp = 0 if version == "1" else 1
            err_ind, err_st, _, var_binds = await get_cmd(
                engine,
                CommunityData(community, mpModel=mp),
                await UdpTransportTarget.create((ip, port), timeout=timeout, retries=0),
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
            if err_ind or err_st:
                return None
            for _, val in var_binds:
                return str(val)
        finally:
            engine.close_dispatcher()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_get())
    except Exception:
        return None
    finally:
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
#  LLD Discovery Engine
# ═════════════════════════════════════════════════════════════════════════════

class LLDEngine:
    """Выполняет LLD для одного устройства по заданным правилам."""

    @staticmethod
    def run_discovery(device: "Device", rule_key: str) -> list[dict]:
        """
        Быстрое обнаружение: один bulk-walk на всю таблицу макросов,
        без отдельных GET на каждый объект.
        """
        tmpl = LLD_TEMPLATES.get(rule_key)
        if not tmpl:
            return []

        # sysName одним GET — быстро, один раз
        sys_name  = _snmp_get_sync(device.ip, device.snmp_community,
                                   device.snmp_port, device.snmp_version,
                                   "1.3.6.1.2.1.1.5.0")
        sys_descr = _snmp_get_sync(device.ip, device.snmp_community,
                                   device.snmp_port, device.snmp_version,
                                   "1.3.6.1.2.1.1.1.0")
        hw_name = (sys_name or "").strip() or                   (sys_descr or "").strip().split()[0][:40] or                   device.name

        # ── Шаг 1: walk по базовому OID → получаем индексы + имена объектов ──
        base_rows = _snmp_walk_sync(device.ip, device.snmp_community,
                                    device.snmp_port, device.snmp_version,
                                    tmpl["walk_oid"])
        if not base_rows:
            return []

        # idx -> имя объекта
        idx_map: dict[str, str] = {}
        for suffix, name in base_rows:
            m = re.match(r"^(\d+)", suffix)
            if m:
                idx = m.group(1)
                if idx not in idx_map:
                    idx_map[idx] = name.strip() or f"idx{idx}"

        if not idx_map:
            return []

        # ── Шаг 2: bulk-walk всех колонок макросов за один проход ────────────
        # Собираем уникальные базовые OID колонок (без .{idx})
        col_walks: dict[str, dict[str, str]] = {}  # base_oid -> {idx -> value}
        mac_defs = tmpl.get("macros", [])

        for mac_def in mac_defs:
            if mac_def["oid_tpl"] is None:
                continue
            # base = OID без последнего .{idx}
            base_col = mac_def["oid_tpl"].replace(".{idx}", "")
            if base_col in col_walks:
                continue
            rows = _snmp_walk_sync(device.ip, device.snmp_community,
                                   device.snmp_port, device.snmp_version,
                                   base_col)
            col_data: dict[str, str] = {}
            for suffix, val in rows:
                m = re.match(r"^(\d+)", suffix)
                if m:
                    col_data[m.group(1)] = val
            col_walks[base_col] = col_data

        # ── Шаг 3: собираем macro_values для каждого индекса ─────────────────
        oid_entries: list[dict] = []

        for idx, iface_name in idx_map.items():
            macro_values: dict[str, str] = {}
            for mac_def in mac_defs:
                macro = mac_def["macro"]
                if mac_def["oid_tpl"] is None:
                    macro_values[macro] = idx
                else:
                    base_col = mac_def["oid_tpl"].replace(".{idx}", "")
                    macro_values[macro] = col_walks.get(base_col, {}).get(idx, "")

            for metric in tmpl["metrics"]:
                oid_str = metric["oid_tpl"].format(
                    idx=idx, iface=iface_name,
                    disk=iface_name, proc=iface_name)
                label = (metric["label"]
                         .replace("{iface}", iface_name)
                         .replace("{disk}",  iface_name)
                         .replace("{proc}",  iface_name)
                         .replace("{idx}",   idx))
                entry = {
                    "label":         label,
                    "oid":           oid_str,
                    "unit":          metric.get("unit", ""),
                    "factor":        metric.get("factor", 1.0),
                    "condition":     "",
                    "threshold":     "",
                    "enabled":       True,
                    "last_alert":    0,
                    "_lld_rule":     rule_key,
                    "_lld_idx":      idx,
                    "_lld_instance": iface_name,
                    "_lld_device":   hw_name,
                    "_lld_macros":   macro_values,
                }
                oid_entries.append(entry)

        return oid_entries

    @staticmethod
    def merge_into_device(device: "Device", new_entries: list[dict],
                          rule_key: str, replace: bool = False):
        """
        Добавляет обнаруженные OID в device.snmp_oids.
        Если replace=True — сначала удаляет старые записи этого rule_key.
        """
        if replace:
            device.snmp_oids = [
                o for o in device.snmp_oids
                if o.get("_lld_rule") != rule_key
            ]
        existing_oids = {o["oid"] for o in device.snmp_oids}
        added = 0
        for entry in new_entries:
            if entry["oid"] not in existing_oids:
                device.snmp_oids.append(entry)
                existing_oids.add(entry["oid"])
                added += 1
        return added


# ═════════════════════════════════════════════════════════════════════════════
#  GUI: LLD Dialog
# ═════════════════════════════════════════════════════════════════════════════

class LLDDialog:
    """
    Диалог Low-Level Discovery.
    Вызывается из DeviceSettingsDialog или контекстного меню устройства.

    Использование:
        dlg = LLDDialog(parent_window, device, colors)
        # После закрытия: device.snmp_oids уже обновлён
    """

    def __init__(self, parent: tk.Misc, device: "Device", colors: dict):
        self.device  = device
        self.colors  = C = colors
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._results: dict[str, list[dict]] = {}   # rule_key → entries

        self.win = tk.Toplevel(parent)
        self.win.title(f"LLD — {device.name} ({device.ip})")
        self.win.geometry("720x600")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(True, True)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        C = self.colors

        # Header
        hdr = tk.Frame(self.win, bg=C["bg2"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr,
                 text="🔎 Low-Level Discovery (LLD)",
                 font=("Consolas", 13, "bold"),
                 bg=C["bg2"], fg=C["accent"],
                 padx=16).pack(side="left")
        tk.Label(hdr,
                 text=f"{self.device.name}  ·  {self.device.ip}",
                 font=("Consolas", 10),
                 bg=C["bg2"], fg=C["text_dim"],
                 padx=8).pack(side="left")

        # Rule checkboxes
        rules_frame = tk.LabelFrame(
            self.win, text=" Шаблоны обнаружения ",
            font=("Consolas", 10), bg=C["bg"], fg=C["text_dim"],
            bd=1, relief="flat", highlightthickness=1,
            highlightbackground=C["border"],
        )
        rules_frame.pack(fill="x", padx=16, pady=8)

        self._rule_vars: dict[str, tk.BooleanVar] = {}
        cols = 2
        for i, (key, tmpl) in enumerate(LLD_TEMPLATES.items()):
            var = tk.BooleanVar(value=True)
            self._rule_vars[key] = var
            cb = tk.Checkbutton(
                rules_frame,
                text=f"{tmpl['icon']}  {tmpl['label']}",
                variable=var,
                font=("Consolas", 10),
                bg=C["bg"], fg=C["text"],
                activebackground=C["bg"],
                activeforeground=C["accent"],
                selectcolor=C["bg3"],
                cursor="hand2",
            )
            cb.grid(row=i // cols, column=i % cols,
                    sticky="w", padx=16, pady=4)

        # Options
        opt_frame = tk.Frame(self.win, bg=C["bg"])
        opt_frame.pack(fill="x", padx=16, pady=4)

        self._replace_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opt_frame,
            text="Заменить существующие LLD-OID (очистить перед обнаружением)",
            variable=self._replace_var,
            font=("Consolas", 9),
            bg=C["bg"], fg=C["text_dim"],
            activebackground=C["bg"],
            selectcolor=C["bg3"],
        ).pack(side="left")

        # Progress
        prog_frame = tk.Frame(self.win, bg=C["bg"])
        prog_frame.pack(fill="x", padx=16, pady=4)

        self._prog_var = tk.DoubleVar(value=0)
        style = ttk.Style()
        style.configure("LLD.Horizontal.TProgressbar",
                        troughcolor=C["bg3"],
                        background=C["accent"],
                        borderwidth=0)
        self._prog = ttk.Progressbar(prog_frame,
                                     variable=self._prog_var,
                                     maximum=100,
                                     style="LLD.Horizontal.TProgressbar")
        self._prog.pack(fill="x")
        self._prog_lbl = tk.Label(prog_frame,
                                   text="Нажмите «Запустить» для начала обнаружения",
                                   font=("Consolas", 9),
                                   bg=C["bg"], fg=C["text_dim"], anchor="w")
        self._prog_lbl.pack(fill="x", pady=2)

        # Results tree
        tree_frame = tk.Frame(self.win, bg=C["bg"])
        tree_frame.pack(fill="both", expand=True, padx=16, pady=4)

        # Столбцы: key, value, note, dev (оборудование), line (линия)
        cols_def = ("key", "value", "note", "dev", "line")
        self._tree = ttk.Treeview(tree_frame, columns=cols_def, show="tree headings",
                                   selectmode="extended")
        self._tree.heading("#0",    text="")
        self._tree.heading("key",   text="Макрос / Метрика")
        self._tree.heading("value", text="OID / Значение")
        self._tree.heading("note",  text="Ед.")
        self._tree.heading("dev",   text="🖥 Оборуд.")
        self._tree.heading("line",  text="🔗 Линия")

        self._tree.column("#0",    width=22,  stretch=False)
        self._tree.column("key",   width=210, stretch=True)
        self._tree.column("value", width=190, stretch=True)
        self._tree.column("note",  width=55,  stretch=False)
        self._tree.column("dev",   width=80,  stretch=False, anchor="center")
        self._tree.column("line",  width=80,  stretch=False, anchor="center")

        sb_y = tk.Scrollbar(tree_frame, orient="vertical",
                            command=self._tree.yview, bg=C["bg3"])
        sb_x = tk.Scrollbar(tree_frame, orient="horizontal",
                            command=self._tree.xview, bg=C["bg3"])
        self._tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        style.configure("Treeview",
                        background=C["bg2"],
                        foreground=C["text"],
                        fieldbackground=C["bg2"],
                        rowheight=22)
        style.configure("Treeview.Heading",
                        background=C["bg3"],
                        foreground=C["text_dim"],
                        font=("Consolas", 9))
        style.map("Treeview", background=[("selected", C["selection"])])

        # Цветовые теги
        self._tree.tag_configure("parent", font=("Consolas", 9, "bold"),
                                  foreground=C.get("accent", "#ffb86c"))
        self._tree.tag_configure("macro",  font=("Consolas", 9),
                                  foreground=C.get("text_dim", "#888888"))
        self._tree.tag_configure("metric", font=("Consolas", 9),
                                  foreground=C.get("online", "#50fa7b"))

        # Клик по столбцам dev/line — переключает чекбокс
        self._tree.bind("<Button-1>", self._on_tree_click)

        # Хранилище состояния чекбоксов: iid → {"dev": bool, "line": bool}
        self._check_state: dict[str, dict] = {}

        # Counter label
        self._count_lbl = tk.Label(self.win,
                                    text="Обнаружено: 0 элементов",
                                    font=("Consolas", 9),
                                    bg=C["bg"], fg=C["text_dim"], anchor="w")
        self._count_lbl.pack(fill="x", padx=16)

        # Buttons
        btn_row = tk.Frame(self.win, bg=C["bg"], pady=10)
        btn_row.pack(fill="x", padx=16)

        self._run_btn = tk.Button(
            btn_row, text="▶ Запустить обнаружение",
            command=self._start,
            bg=C["accent"], fg="white",
            activebackground=C["accent2"],
            font=("Consolas", 10, "bold"),
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        )
        self._run_btn.pack(side="left", padx=4)

        self._stop_btn = tk.Button(
            btn_row, text="⏹ Стоп",
            command=self._stop,
            bg=C["bg3"], fg=C["danger"],
            activebackground=C["border"],
            font=("Consolas", 10),
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
            state="disabled",
        )
        self._stop_btn.pack(side="left", padx=4)

        # Быстрые переключатели чекбоксов
        chk_frame = tk.Frame(btn_row, bg=C["bg"])
        chk_frame.pack(side="left", padx=12)
        tk.Label(chk_frame, text="Выбрать все:",
                 font=("Consolas", 9), bg=C["bg"], fg=C["text_dim"]
                 ).pack(side="left", padx=(0,4))
        tk.Button(chk_frame, text="🖥 Оборуд.",
                  command=lambda: self._toggle_all_checks("dev", True),
                  bg=C["bg3"], fg=C["accent2"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=8, pady=4, cursor="hand2"
                  ).pack(side="left", padx=2)
        tk.Button(chk_frame, text="🔗 Линии",
                  command=lambda: self._toggle_all_checks("line", True),
                  bg=C["bg3"], fg=C["accent"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=8, pady=4, cursor="hand2"
                  ).pack(side="left", padx=2)
        tk.Button(chk_frame, text="✕ Сброс",
                  command=lambda: [
                      self._toggle_all_checks("dev", False),
                      self._toggle_all_checks("line", False)
                  ],
                  bg=C["bg3"], fg=C["text_dim"],
                  font=("Consolas", 9), relief="flat", bd=0,
                  padx=8, pady=4, cursor="hand2"
                  ).pack(side="left", padx=2)

        tk.Button(
            btn_row, text="✓ Применить выбранные",
            command=self._apply_selected,
            bg=C["accent2"], fg="white",
            activebackground="#2d8f40",
            font=("Consolas", 10, "bold"),
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        ).pack(side="right", padx=4)

        tk.Button(
            btn_row, text="✓ Применить все",
            command=self._apply_all,
            bg=C["bg3"], fg=C["text"],
            activebackground=C["border"],
            font=("Consolas", 10),
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        ).pack(side="right", padx=4)

    # ── Переключение чекбоксов dev/line ──────────────────────────────────────

    def _on_tree_click(self, event):
        """Переключает чекбокс в столбце dev или line при клике."""
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col  = self._tree.identify_column(event.x)  # "#4" или "#5"
        iid  = self._tree.identify_row(event.y)
        if not iid:
            return
        tags = self._tree.item(iid, "tags")
        if "metric" not in tags:
            return

        # columns=("key","value","note","dev","line")
        # #0=tree, #1=key, #2=value, #3=note, #4=dev, #5=line
        if col == "#4":
            field = "dev"
        elif col == "#5":
            field = "line"
        else:
            return

        state = self._check_state.get(iid, {"dev": False, "line": False})
        state[field] = not state[field]
        self._check_state[iid] = state

        # Обновляем отображение
        vals = list(self._tree.item(iid, "values"))
        # vals: [key, value, note, dev_cb, line_cb]
        dev_sym  = "✅" if state["dev"]  else "☐"
        line_sym = "✅" if state["line"] else "☐"
        vals[3] = dev_sym
        vals[4] = line_sym
        self._tree.item(iid, values=vals)

    def _toggle_all_checks(self, field: str, value: bool):
        """Устанавливает все чекбоксы field в value (для кнопок «Все»)."""
        for iid, state in self._check_state.items():
            state[field] = value
            vals = list(self._tree.item(iid, "values"))
            vals[3] = "✅" if state["dev"]  else "☐"
            vals[4] = "✅" if state["line"] else "☐"
            self._tree.item(iid, values=vals)

    # ── Discovery control ─────────────────────────────────────────────────────

    def _start(self):
        selected_rules = [k for k, v in self._rule_vars.items() if v.get()]
        if not selected_rules:
            messagebox.showwarning("LLD", "Выберите хотя бы один шаблон.",
                                   parent=self.win)
            return

        if not self.device.snmp_enabled:
            messagebox.showwarning(
                "LLD", "SNMP не включён для этого устройства.\n"
                        "Включите SNMP в настройках устройства.",
                parent=self.win,
            )
            return

        self._running = True
        self._results.clear()
        self._tree.delete(*self._tree.get_children())
        self._count_lbl.config(text="Обнаружено: 0 элементов")
        self._run_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._prog_var.set(0)
        self._prog_lbl.config(text="Запуск...")

        self._thread = threading.Thread(
            target=self._worker,
            args=(selected_rules,),
            daemon=True,
        )
        self._thread.start()
        self._poll()

    def _stop(self):
        self._running = False
        self._stop_btn.config(state="disabled")
        self._prog_lbl.config(text="Остановлено пользователем")

    # ── Background worker ─────────────────────────────────────────────────────

    def _worker(self, rules: list[str]):
        import queue as _q
        self._q: _q.Queue = _q.Queue()
        total = len(rules)
        for step, rule_key in enumerate(rules):
            if not self._running:
                break
            tmpl = LLD_TEMPLATES[rule_key]
            self._q.put(("status", f"[{step+1}/{total}] {tmpl['label']} …"))
            entries = LLDEngine.run_discovery(self.device, rule_key)
            self._results[rule_key] = entries
            self._q.put(("found", rule_key, entries))
            self._q.put(("progress", (step + 1) / total * 100))

        self._q.put(("done",))

    def _poll(self):
        try:
            q = getattr(self, "_q", None)
            if q:
                while True:
                    item = q.get_nowait()
                    if item[0] == "status":
                        self._prog_lbl.config(text=item[1])
                    elif item[0] == "progress":
                        self._prog_var.set(item[1])
                    elif item[0] == "found":
                        _, rule_key, entries = item
                        tmpl = LLD_TEMPLATES[rule_key]
                        # Группируем по индексу объекта (один объект = один раздел)
                        seen_idx: dict[str, str] = {}   # idx -> iid родителя
                        for e in entries:
                            idx  = e.get("_lld_idx", "?")
                            inst = e.get("_lld_instance", idx)
                            macros = e.get("_lld_macros", {})
                            hw   = e.get("_lld_device", "")

                            if idx not in seen_idx:
                                # Родительская строка — сам объект
                                parent_text = f"{tmpl['icon']} {inst}"
                                parent_iid = self._tree.insert(
                                    "", "end",
                                    text="▶",
                                    values=(
                                        parent_text,
                                        hw,
                                        f"{tmpl['label']} · индекс {idx}",
                                    ),
                                    tags=(rule_key, "parent"),
                                    open=False,
                                )
                                seen_idx[idx] = parent_iid

                                # Дочерние строки — макросы уточнения
                                mac_defs = tmpl.get("macros", [])
                                for mac_def in mac_defs:
                                    macro = mac_def["macro"]
                                    val   = macros.get(macro, "")
                                    note  = mac_def.get("note", "")
                                    self._tree.insert(
                                        parent_iid, "end",
                                        text="",
                                        values=(macro, val, note),
                                        tags=(rule_key, "macro"),
                                    )

                            # Метрика — дочерняя строка под объектом
                            raw_label = e["label"]
                            bracket = raw_label.find(" [")
                            metric_name = raw_label[:bracket] if bracket != -1 else raw_label
                            iid = self._tree.insert(
                                seen_idx[idx], "end",
                                text="",
                                values=(
                                    f"  📊 {metric_name}",
                                    e["oid"],
                                    e["unit"],
                                    "☐",   # dev checkbox
                                    "☐",   # line checkbox
                                ),
                                tags=(rule_key, "metric"),
                            )
                            # Инициализируем состояние чекбоксов
                            self._check_state[iid] = {"dev": False, "line": False}

                        total = sum(len(v) for v in self._results.values())
                        self._count_lbl.config(text=f"Обнаружено: {total} элементов")
                    elif item[0] == "done":
                        self._running = False
                        self._run_btn.config(state="normal")
                        self._stop_btn.config(state="disabled")
                        self._prog_var.set(100)
                        total = sum(len(v) for v in self._results.values())
                        self._prog_lbl.config(
                            text=f"✓ Готово. Обнаружено: {total} элементов"
                        )
                        return
        except Exception:
            pass

        if self.win.winfo_exists():
            self.win.after(300, self._poll)

    # ── Apply results ─────────────────────────────────────────────────────────

    def _apply_selected(self):
        """Применяет только отмеченные в дереве строки.
        При выборе родителя (объекта) добавляются все его метрики."""
        sel = self._tree.selection()
        if not sel:
            self._apply_all()
            return

        # Раскрываем выбор: если выбран родитель — берём все дочерние metric
        expanded_iids: set[str] = set()
        for iid in sel:
            tags = self._tree.item(iid, "tags")
            if "parent" in tags:
                for child in self._tree.get_children(iid):
                    child_tags = self._tree.item(child, "tags")
                    if "metric" in child_tags:
                        expanded_iids.add(child)
            elif "metric" in tags:
                expanded_iids.add(iid)

        # Собираем OID и флаги из metric-строк
        # values: [key, oid, unit, dev_cb, line_cb]
        selected_oids: dict[str, dict] = {}   # oid → {show_dev, show_line}
        for iid in expanded_iids:
            vals = self._tree.item(iid, "values")
            if vals and len(vals) >= 2:
                oid_candidate = str(vals[1]).strip()
                if oid_candidate.startswith("1."):
                    state = self._check_state.get(iid, {"dev": False, "line": False})
                    selected_oids[oid_candidate] = state

        replace    = self._replace_var.get()
        added_dev  = 0
        added_line = 0

        for rule_key, entries in self._results.items():
            filtered = [e for e in entries if e["oid"] in selected_oids]
            if not filtered:
                continue
            if replace:
                self.device.snmp_oids = [
                    o for o in self.device.snmp_oids
                    if o.get("_lld_rule") != rule_key
                ]
            for entry in filtered:
                state = selected_oids[entry["oid"]]
                entry_copy = dict(entry)
                entry_copy["show_on_device"] = state["dev"]
                entry_copy["show_on_line"]   = state["line"]
                added_dev  += LLDEngine.merge_into_device(
                    self.device, [entry_copy], rule_key, replace=False)
            # Добавляем метку на линию если выбрано
            self._apply_line_labels(filtered, selected_oids)

        msg = f"Добавлено в оборудование: {added_dev} OID"
        messagebox.showinfo("LLD", msg, parent=self.win)
        self.win.destroy()

    def _apply_all(self):
        """Применяет все обнаруженные элементы с учётом чекбоксов."""
        replace    = self._replace_var.get()
        added_dev  = 0

        # Собираем состояние всех metric iid
        all_states: dict[str, dict] = {}  # oid → state
        for iid, state in self._check_state.items():
            vals = self._tree.item(iid, "values")
            if vals and len(vals) >= 2:
                oid = str(vals[1]).strip()
                if oid.startswith("1."):
                    all_states[oid] = state

        for rule_key, entries in self._results.items():
            for entry in entries:
                state = all_states.get(entry["oid"], {"dev": False, "line": False})
                entry_copy = dict(entry)
                entry_copy["show_on_device"] = state["dev"]
                entry_copy["show_on_line"]   = state["line"]
                added_dev += LLDEngine.merge_into_device(
                    self.device, [entry_copy], rule_key, replace=replace)
            self._apply_line_labels(entries, all_states)

        messagebox.showinfo("LLD",
                            f"Добавлено в оборудование: {added_dev} OID",
                            parent=self.win)
        self.win.destroy()

    def _apply_line_labels(self, entries: list[dict], states: dict):
        """
        Добавляет метки на линию соединения для записей с show_on_line=True.
        Использует ConnectionLabelManager из main через импорт.
        Ищет все соединения устройства в текущей вкладке.
        """
        line_entries = [e for e in entries
                        if states.get(e["oid"], {}).get("line", False)]
        if not line_entries:
            return
        try:
            # Получаем conn_labels и устройства из основного приложения
            import main as _main
            app = None
            # Ищем экземпляр NetworkMapApp через Tk
            import tkinter as _tk
            for widget in _tk.Misc._default_root.winfo_children() if _tk.Misc._default_root else []:
                pass
            # Более надёжный способ — через глобальный реестр
            for obj in _main.__dict__.values():
                if isinstance(obj, _main.NetworkMapApp):
                    app = obj
                    break
            if app is None:
                # Пробуем через список всех Toplevel
                import gc
                for obj in gc.get_objects():
                    if type(obj).__name__ == "NetworkMapApp":
                        app = obj
                        break
            if app is None:
                return

            tab = app.current_tab
            if not tab:
                return
            dev_id = self.device.dev_id

            # Находим все соединения этого устройства
            for (id1, id2) in tab.connections:
                if id1 != dev_id and id2 != dev_id:
                    continue
                # Для каждой line_entry добавляем слот в conn_labels
                existing = app.conn_labels.get(id1, id2)
                existing_oids = {s.get("oid") for s in existing}
                new_slots = list(existing)
                for entry in line_entries:
                    if entry["oid"] in existing_oids:
                        continue
                    new_slots.append({
                        "source_dev": dev_id,
                        "oid_label":  entry["label"],
                        "oid":        entry["oid"],
                        "unit":       entry.get("unit", ""),
                        "factor":     entry.get("factor", 1.0),
                        "interval":   30,
                        "_last_val":  None,
                        "_last_ts":   0.0,
                        "_error":     None,
                    })
                app.conn_labels.set(id1, id2, new_slots)
        except Exception as e:
            print(f"[LLD] Ошибка добавления меток на линию: {e}")

    def _on_close(self):
        self._running = False
        self.win.destroy()


# ═════════════════════════════════════════════════════════════════════════════
#  Утилита: быстрый тест из командной строки
# ═════════════════════════════════════════════════════════════════════════════

def cli_test(ip: str, community: str = "public",
             port: int = 161, version: str = "2c"):
    """
    Запустите из командной строки для проверки LLD:
        python snmp_lld.py 192.168.1.1 public
    """
    print(f"\n[LLD CLI] Устройство: {ip}  community={community}\n")
    for rule_key, tmpl in LLD_TEMPLATES.items():
        print(f"  {tmpl['icon']}  {tmpl['label']} ({rule_key})")
        rows = _snmp_walk_sync(ip, community, port, version, tmpl["walk_oid"])
        if not rows:
            print("      ⚠  нет данных (устройство недоступно или OID не поддерживается)")
            continue
        for suffix, val in rows[:6]:
            print(f"      .{suffix} = {val}")
        if len(rows) > 6:
            print(f"      … ещё {len(rows)-6} записей")
        print()


if __name__ == "__main__":
    import sys
    ip  = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    com = sys.argv[2] if len(sys.argv) > 2 else "public"
    cli_test(ip, com)

"""Device settings dialog with SNMP triggers and factor (coefficient)"""

import tkinter as tk
from tkinter import ttk
from device import Device
from snmp_lld import LLDDialog


class DeviceSettingsDialog:
    def __init__(self, parent, dev: Device, colors: dict, icons: dict):
        self.dev = dev
        self.colors = C = colors
        self.icons = icons
        self.result = False
        self.editing_item = None

        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Настройки: {dev.name}")
        self.dialog.geometry("780x800")  # увеличен под новую колонку
        self.dialog.configure(bg=C["bg"])
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)

        # Переменные
        self.var_name = tk.StringVar(value=dev.name)
        self.var_ip = tk.StringVar(value=dev.ip)
        self.var_dtype = tk.StringVar(value=dev.dtype)
        self.var_loc = tk.StringVar(value=dev.location)
        self.var_desc = tk.StringVar(value=dev.description)
        self.var_interval = tk.StringVar(value=str(getattr(dev, 'check_interval', 30)))
        self.var_ping = tk.BooleanVar(value=dev.ping_enabled)
        self.var_snmp = tk.BooleanVar(value=dev.snmp_enabled)
        self.var_community = tk.StringVar(value=dev.snmp_community)
        self.var_port = tk.StringVar(value=str(dev.snmp_port))
        self.var_version = tk.StringVar(value=dev.snmp_version)

        self._build()

    def _open_lld(self):
        """Открывает диалог Low-Level Discovery."""
        # Сохраняем текущие настройки SNMP во временный dev
        self.dev.snmp_community = self.var_community.get()
        self.dev.snmp_port = int(self.var_port.get() or 161)
        self.dev.snmp_version = self.var_version.get()
        self.dev.snmp_enabled = self.var_snmp.get()

        from snmp_lld import LLDDialog
        LLDDialog(self.dialog, self.dev, self.colors)
            # После закрытия LLDDialog — обновляем дерево OID
        self.oid_tree.delete(*self.oid_tree.get_children())
        for oid in self.dev.snmp_oids:
            self.oid_tree.insert("", "end", values=(
                oid.get("label", ""),
                oid.get("oid", ""),
                oid.get("unit", ""),
                oid.get("factor", 1.0),
                oid.get("condition", ""),
                oid.get("threshold", ""),
                oid.get("enabled", True),
                ))

    def _build(self):
        C = self.colors

        # Шапка
        hdr = tk.Frame(self.dialog, bg=C["bg2"], pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚙ Настройки устройства",
                 font=("Consolas", 13, "bold"),
                 bg=C["bg2"], fg=C["accent"],
                 padx=20).pack(side="left")

        # Стиль вкладок
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TNotebook", background=C["bg"], borderwidth=0)
        style.configure("Dark.TNotebook.Tab",
                        background=C["bg3"], foreground=C["text_dim"],
                        padding=[12, 6], borderwidth=0)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", C["bg2"])],
                  foreground=[("selected", C["text"])])

        notebook = ttk.Notebook(self.dialog, style="Dark.TNotebook")
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # ─── Общие настройки ───────────────────────────────────────────────
        general_tab = tk.Frame(notebook, bg=C["bg"], padx=15, pady=15)
        notebook.add(general_tab, text="📋 Общие")

        row = 0
        tk.Label(general_tab, text="Имя устройства:", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(general_tab, textvariable=self.var_name, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, relief="flat").grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        tk.Label(general_tab, text="IP адрес:", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(general_tab, textvariable=self.var_ip, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, relief="flat").grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        tk.Label(general_tab, text="Тип устройства:", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        cb_type = ttk.Combobox(general_tab, textvariable=self.var_dtype, values=["router", "switch", "server", "pc", "printer", "camera", "phone", "ups", "other"], state="readonly")
        cb_type.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        tk.Label(general_tab, text="Расположение:", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(general_tab, textvariable=self.var_loc, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, relief="flat").grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        tk.Label(general_tab, text="Описание:", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(general_tab, textvariable=self.var_desc, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, relief="flat").grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        tk.Label(general_tab, text="Интервал опроса (сек):", bg=C["bg"], fg=C["text_dim"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(general_tab, textvariable=self.var_interval, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, relief="flat").grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1

        general_tab.columnconfigure(1, weight=1)

        ping_frame = tk.Frame(general_tab, bg=C["bg"], pady=10)
        ping_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        cb_ping = tk.Checkbutton(ping_frame, text="Включить Ping мониторинг", variable=self.var_ping,
                                 bg=C["bg"], fg=C["text"], activebackground=C["bg"], activeforeground=C["text"],
                                 selectcolor=C["bg3"], command=self._update_ping_label)
        cb_ping.pack(side="left")
        self.ping_status_lbl = tk.Label(ping_frame, text="", bg=C["bg"], padx=10)
        self.ping_status_lbl.pack(side="left")
        self._update_ping_label()

        # ─── SNMP Метрики и триггеры (с коэффициентом) ────────────────────
        snmp_tab = tk.Frame(notebook, bg=C["bg"], padx=15, pady=15)
        notebook.add(snmp_tab, text="📊 SNMP Метрики")

        cb_snmp = tk.Checkbutton(snmp_tab, text="Включить SNMP опрос", variable=self.var_snmp,
                                 bg=C["bg"], fg=C["text"], activebackground=C["bg"], activeforeground=C["text"],
                                 selectcolor=C["bg3"], command=self._update_snmp_state)
        cb_snmp.pack(anchor="w", pady=5)

        self.snmp_params = tk.Frame(snmp_tab, bg=C["bg"])
        self.snmp_params.pack(fill="both", expand=True)

        conn_frame = tk.Frame(self.snmp_params, bg=C["bg"])
        conn_frame.pack(fill="x", pady=5)

        tk.Label(conn_frame, text="Community:", bg=C["bg"], fg=C["text_dim"]).grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(conn_frame, textvariable=self.var_community, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, width=12).grid(row=0, column=1, sticky="w", padx=5, pady=2)

        tk.Label(conn_frame, text="Порт:", bg=C["bg"], fg=C["text_dim"]).grid(row=0, column=2, sticky="w", padx=10, pady=2)
        tk.Entry(conn_frame, textvariable=self.var_port, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, width=6).grid(row=0, column=3, sticky="w", padx=5, pady=2)

        tk.Label(conn_frame, text="Версия:", bg=C["bg"], fg=C["text_dim"]).grid(row=0, column=4, sticky="w", padx=10, pady=2)
        ttk.Combobox(conn_frame, textvariable=self.var_version, values=["1", "2c"], state="readonly", width=5).grid(row=0, column=5, sticky="w", padx=5, pady=2)

        # Таблица OID с 7 колонками (добавлен factor)
        cols = ("label", "oid", "unit", "factor", "condition", "threshold", "enabled")
        tv_style = ttk.Style()
        tv_style.configure("Dark.Treeview", background=C["bg3"], foreground=C["text"], fieldbackground=C["bg3"], borderwidth=0, rowheight=24)
        tv_style.configure("Dark.Treeview.Heading", background=C["bg2"], foreground=C["text"], borderwidth=1)
        tv_style.map("Dark.Treeview", background=[("selected", C["accent"])], foreground=[("selected", C["bg"])])

        self.oid_tree = ttk.Treeview(self.snmp_params, columns=cols, show="headings", height=6, style="Dark.Treeview")
        self.oid_tree.heading("label", text="Название метрики")
        self.oid_tree.heading("oid", text="OID")
        self.oid_tree.heading("unit", text="Ед. изм.")
        self.oid_tree.heading("factor", text="Коэф.")
        self.oid_tree.heading("condition", text="Условие")
        self.oid_tree.heading("threshold", text="Порог")
        self.oid_tree.heading("enabled", text="Вкл")

        self.oid_tree.column("label", width=120, anchor="w")
        self.oid_tree.column("oid", width=180, anchor="w")
        self.oid_tree.column("unit", width=60, anchor="center")
        self.oid_tree.column("factor", width=60, anchor="center")
        self.oid_tree.column("condition", width=70, anchor="center")
        self.oid_tree.column("threshold", width=70, anchor="center")
        self.oid_tree.column("enabled", width=45, anchor="center")
        self.oid_tree.pack(fill="x", pady=5)

        # Загрузка существующих OID
        if hasattr(self.dev, "snmp_oids") and self.dev.snmp_oids:
            for oid in self.dev.snmp_oids:
                self.oid_tree.insert("", "end", values=(
                    oid.get("label", ""),
                    oid.get("oid", ""),
                    oid.get("unit", ""),
                    oid.get("factor", 1.0),
                    oid.get("condition", ""),
                    oid.get("threshold", ""),
                    oid.get("enabled", True)
                ))

        # Форма добавления/редактирования
        add_frame = tk.LabelFrame(self.snmp_params, text="Параметры метрики", bg=C["bg"], fg=C["accent"], pady=5, padx=5)
        add_frame.pack(fill="x", pady=5)

        # Название
        tk.Label(add_frame, text="Название:", bg=C["bg"], fg=C["text"]).grid(row=0, column=0, sticky="w", padx=2)
        self.ent_label = tk.Entry(add_frame, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1)
        self.ent_label.grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        # OID
        tk.Label(add_frame, text="OID:", bg=C["bg"], fg=C["text"]).grid(row=1, column=0, sticky="w", padx=2)
        self.ent_oid = tk.Entry(add_frame, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1)
        self.ent_oid.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        # Единица измерения
        tk.Label(add_frame, text="Ед. изм:", bg=C["bg"], fg=C["text"]).grid(row=2, column=0, sticky="w", padx=2)
        self.ent_unit = tk.Entry(add_frame, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, width=10)
        self.ent_unit.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        # Коэффициент (factor)
        tk.Label(add_frame, text="Коэф.:", bg=C["bg"], fg=C["text"]).grid(row=3, column=0, sticky="w", padx=2)
        self.ent_factor = tk.Entry(add_frame, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, width=8)
        self.ent_factor.grid(row=3, column=1, sticky="w", padx=5, pady=2)
        self.ent_factor.insert(0, "1.0")

        # Условие и порог
        tk.Label(add_frame, text="Условие:", bg=C["bg"], fg=C["text"]).grid(row=4, column=0, sticky="w", padx=2)
        self.cond_combo = ttk.Combobox(add_frame, values=[">", "<", ">=", "<=", "=="], state="readonly", width=5)
        self.cond_combo.grid(row=4, column=1, sticky="w", padx=5, pady=2)

        tk.Label(add_frame, text="Порог:", bg=C["bg"], fg=C["text"]).grid(row=4, column=2, sticky="w", padx=10)
        self.ent_threshold = tk.Entry(add_frame, bg=C["bg3"], fg=C["text"], insertbackground=C["text"], borderwidth=1, width=8)
        self.ent_threshold.grid(row=4, column=3, sticky="w", padx=5, pady=2)

        self.trig_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(add_frame, text="Активен", variable=self.trig_enabled,
                       bg=C["bg"], fg=C["text"], selectcolor=C["bg3"]).grid(row=4, column=4, padx=10)

        add_frame.columnconfigure(1, weight=1)

        # Кнопки управления
        btn_frame = tk.Frame(add_frame, bg=C["bg"])
        btn_frame.grid(row=5, column=0, columnspan=5, sticky="ew", pady=5)

        tk.Button(
            btn_frame, text="🔎 LLD",
            bg="#6272a4", fg="white",
            command=self._open_lld,
            font=("Consolas", 9, "bold"),
            relief="flat", padx=6
        ).pack(side="left", padx=5)
        tk.Button(btn_frame, text="➕ Добавить", bg=C["accent2"], fg=C["bg"],
                  command=self._add_oid_to_tree, font=("Consolas", 9, "bold"), relief="flat", padx=6).pack(side="left", padx=5)

        self.btn_save_edit = tk.Button(btn_frame, text="💾 Обновить", bg=C["accent"], fg=C["bg"],
                  command=self._update_selected_oid, font=("Consolas", 9, "bold"), relief="flat", padx=6, state="disabled")
        self.btn_save_edit.pack(side="left", padx=5)

        tk.Button(btn_frame, text="✏ Изменить", bg=C["bg3"], fg=C["text"],
                  command=self._load_oid_to_edit, font=("Consolas", 9), relief="flat", padx=6).pack(side="left", padx=5)

        tk.Button(btn_frame, text="❌ Удалить", bg=C["danger"], fg=C["text"],
                  command=self._delete_oid_from_tree, font=("Consolas", 9), relief="flat", padx=6).pack(side="right", padx=5)

        self._update_snmp_state()

        # Футер
        dlg_btns = tk.Frame(self.dialog, bg=C["bg"], pady=10)
        dlg_btns.pack(fill="x", side="bottom")

        tk.Button(dlg_btns, text="Сохранить", bg=C["accent"], fg=C["bg"], font=("Consolas", 10, "bold"),
                  command=self._save, width=12, relief="flat").pack(side="right", padx=15)
        tk.Button(dlg_btns, text="Отмена", bg=C["bg3"], fg=C["text"], font=("Consolas", 10),
                  command=self.dialog.destroy, width=10, relief="flat").pack(side="right")

    # Вспомогательные методы
    def _update_ping_label(self):
        C = self.colors
        enabled = self.var_ping.get()
        self.ping_status_lbl.config(
            text="●  Ping включён" if enabled else "○  Ping выключен",
            fg=C["online"] if enabled else C["text_dim"]
        )

    def _update_snmp_state(self):
        state = "normal" if self.var_snmp.get() else "disabled"
        for child in self.snmp_params.winfo_children():
            self._set_widget_state(child, state)

    def _set_widget_state(self, widget, state):
        try: widget.config(state=state)
        except: pass
        for child in widget.winfo_children():
            self._set_widget_state(child, state)

    def _clear_oid_form(self):
        self.ent_label.delete(0, tk.END)
        self.ent_oid.delete(0, tk.END)
        self.ent_unit.delete(0, tk.END)
        self.ent_factor.delete(0, tk.END)
        self.ent_factor.insert(0, "1.0")
        self.cond_combo.set("")
        self.ent_threshold.delete(0, tk.END)
        self.trig_enabled.set(True)

    def _add_oid_to_tree(self):
        lbl = self.ent_label.get().strip()
        oid = self.ent_oid.get().strip()
        unit = self.ent_unit.get().strip()
        factor = self.ent_factor.get().strip()
        condition = self.cond_combo.get()
        threshold = self.ent_threshold.get().strip()
        enabled = self.trig_enabled.get()
        if lbl and oid:
            self.oid_tree.insert("", "end", values=(lbl, oid, unit, factor, condition, threshold, enabled))
            self._clear_oid_form()

    def _load_oid_to_edit(self):
        selected = self.oid_tree.selection()
        if not selected:
            return
        self.editing_item = selected[0]
        values = self.oid_tree.item(self.editing_item)["values"]
        self.ent_label.delete(0, tk.END); self.ent_label.insert(0, str(values[0]))
        self.ent_oid.delete(0, tk.END); self.ent_oid.insert(0, str(values[1]))
        self.ent_unit.delete(0, tk.END); self.ent_unit.insert(0, str(values[2]) if len(values) > 2 else "")
        self.ent_factor.delete(0, tk.END); self.ent_factor.insert(0, str(values[3]) if len(values) > 3 else "1.0")
        self.cond_combo.set(str(values[4]) if len(values) > 4 else "")
        self.ent_threshold.delete(0, tk.END); self.ent_threshold.insert(0, str(values[5]) if len(values) > 5 else "")
        self.trig_enabled.set(bool(values[6]) if len(values) > 6 else True)
        self.btn_save_edit.config(state="normal")

    def _update_selected_oid(self):
        if hasattr(self, 'editing_item') and self.editing_item and self.oid_tree.exists(self.editing_item):
            lbl = self.ent_label.get().strip()
            oid = self.ent_oid.get().strip()
            unit = self.ent_unit.get().strip()
            factor = self.ent_factor.get().strip()
            condition = self.cond_combo.get()
            threshold = self.ent_threshold.get().strip()
            enabled = self.trig_enabled.get()
            if lbl and oid:
                self.oid_tree.item(self.editing_item, values=(lbl, oid, unit, factor, condition, threshold, enabled))
                self._clear_oid_form()
                self.btn_save_edit.config(state="disabled")
                self.editing_item = None

    def _delete_oid_from_tree(self):
        selected = self.oid_tree.selection()
        for item in selected:
            self.oid_tree.delete(item)
            if hasattr(self, 'editing_item') and self.editing_item == item:
                self.btn_save_edit.config(state="disabled")
                self.editing_item = None

    def _save(self):
        dev = self.dev
        dev.name         = self.var_name.get().strip() or "Устройство"
        dev.ip           = self.var_ip.get().strip()
        dev.dtype        = self.var_dtype.get()
        dev.location     = self.var_loc.get().strip()
        dev.description  = self.var_desc.get().strip()
        try:
            val = int(self.var_interval.get().strip())
            dev.check_interval = val if val > 0 else 30
        except:
            dev.check_interval = 30
        dev.ping_enabled = self.var_ping.get()
        dev.snmp_enabled = self.var_snmp.get()
        dev.snmp_community = self.var_community.get().strip() or "public"
        try: dev.snmp_port = int(self.var_port.get().strip())
        except: dev.snmp_port = 161
        dev.snmp_version = self.var_version.get()

        dev.snmp_oids = []
        for child in self.oid_tree.get_children():
            values = self.oid_tree.item(child)["values"]
            factor_val = 1.0
            try:
                factor_val = float(values[3]) if len(values) > 3 and values[3] else 1.0
            except:
                pass
            dev.snmp_oids.append({
                "label": str(values[0]),
                "oid": str(values[1]),
                "unit": str(values[2]) if len(values) > 2 else "",
                "factor": factor_val,
                "condition": str(values[4]) if len(values) > 4 else "",
                "threshold": str(values[5]) if len(values) > 5 else "",
                "enabled": bool(values[6]) if len(values) > 6 else True,
                "last_alert": 0
            })
        self.result = True
        self.dialog.destroy()
"""
reporter.py – модуль для сбора истории SNMP-метрик в SQLite3 и генерации отчётов в Excel с графиками.
"""
import sqlite3
import os
import datetime
import threading
import tempfile
from typing import Dict, List, Tuple, Optional

# Для Excel и графиков
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

from device import Device

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snmp_history.db")

class HistoryDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snmp_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metric_name TEXT NOT NULL,
                    raw_value TEXT,
                    numeric_value REAL,
                    unit TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_time ON snmp_history(device_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metric ON snmp_history(metric_name)")

    def add_record(self, device_id: str, metric_name: str, raw_value: str,
                   numeric_value: Optional[float], unit: str, timestamp: float = None):
        if timestamp is None:
            timestamp = datetime.datetime.now().timestamp()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO snmp_history (device_id, timestamp, metric_name, raw_value, numeric_value, unit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (device_id, timestamp, metric_name, raw_value, numeric_value, unit))

    def purge_old_records(self, max_age_days: int = 90):
        """Удаляет записи старше max_age_days дней. Вызывать периодически."""
        cutoff = datetime.datetime.now().timestamp() - max_age_days * 86400
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM snmp_history WHERE timestamp < ?", (cutoff,)
            )
            deleted = cur.rowcount
        return deleted

    def get_db_size_mb(self) -> float:
        """Размер файла базы данных в МБ."""
        try:
            return os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return 0.0

    def get_metrics_for_device(self, device_id: str, metric_name: str = None,
                                start_ts: float = None, end_ts: float = None,
                                limit: int = None) -> List[Tuple]:
        query = "SELECT timestamp, metric_name, raw_value, numeric_value, unit FROM snmp_history WHERE device_id = ?"
        params = [device_id]
        if metric_name:
            query += " AND metric_name = ?"
            params.append(metric_name)
        if start_ts is not None:
            query += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            query += " AND timestamp <= ?"
            params.append(end_ts)
        query += " ORDER BY timestamp ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall()

    def get_all_metric_names(self, device_id: str) -> List[str]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT DISTINCT metric_name FROM snmp_history WHERE device_id = ? ORDER BY metric_name", (device_id,))
            return [row[0] for row in cur.fetchall()]

history_db = HistoryDB()

# Авто-очистка: удаляем записи старше 90 дней при запуске
def _auto_purge():
    """Запускается в фоне при старте — удаляет устаревшие записи."""
    try:
        deleted = history_db.purge_old_records(max_age_days=90)
        if deleted:
            print(f"[History] Удалено {deleted} устаревших записей (>90 дней)")
    except Exception as e:
        print(f"[History] Ошибка очистки: {e}")

import threading as _threading
_threading.Thread(target=_auto_purge, daemon=True).start()

def store_snmp_data(dev: Device, snmp_data: dict):
    """Сохраняет все числовые SNMP-метрики устройства в БД."""
    if not dev.snmp_enabled:
        return
    now = datetime.datetime.now().timestamp()
    import re
    for metric_name, raw_value in snmp_data.items():
        numeric = None
        match = re.search(r"[-+]?\d*\.?\d+", str(raw_value))
        if match:
            try:
                numeric = float(match.group())
            except:
                pass
        unit = ""
        if numeric is not None and numeric != raw_value:
            rest = str(raw_value).replace(str(numeric), "").strip()
            if rest:
                unit = rest
        history_db.add_record(dev.dev_id, metric_name, str(raw_value), numeric, unit, now)

def generate_report(device_id: str, device_name: str, start_time: datetime.datetime,
                    end_time: datetime.datetime, metrics: List[str] = None,
                    output_path: str = None) -> Tuple[bool, str]:
    if not HAS_OPENPYXL:
        return False, "Библиотека openpyxl не установлена. Установите: pip install openpyxl"
    if not HAS_MATPLOTLIB:
        return False, "Библиотека matplotlib не установлена. Установите: pip install matplotlib"

    if output_path is None:
        safe_name = device_name.replace(" ", "_").replace("/", "_")
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   f"report_{safe_name}_{start_time.strftime('%Y%m%d_%H%M')}_{end_time.strftime('%Y%m%d_%H%M')}.xlsx")

    start_ts = start_time.timestamp()
    end_ts = end_time.timestamp()
    if metrics is None:
        all_metrics = history_db.get_all_metric_names(device_id)
        metrics = []
        for m in all_metrics:
            rows = history_db.get_metrics_for_device(device_id, m, start_ts, end_ts, limit=1)
            if rows and rows[0][3] is not None:
                metrics.append(m)
    if not metrics:
        return False, "Нет числовых данных для построения отчёта за выбранный период."

    try:
        wb = openpyxl.Workbook()
        if 'Sheet' in wb.sheetnames:
            wb.remove(wb['Sheet'])

        # Лист с данными
        data_sheet = wb.create_sheet("Данные")
        headers = ["Время", "Метрика", "Исходное значение", "Числовое значение", "Единица"]
        data_sheet.append(headers)
        for col_idx, header in enumerate(headers, 1):
            cell = data_sheet.cell(row=1, column=col_idx)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2E75B6")
            cell.alignment = Alignment(horizontal="center")

        # Заполняем данные
        for metric in metrics:
            rows = history_db.get_metrics_for_device(device_id, metric, start_ts, end_ts)
            for ts, m_name, raw_val, num_val, unit in rows:
                dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                data_sheet.append([dt, m_name, raw_val, num_val, unit])

        # Автоширина колонок
        for col in data_sheet.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    if len(str(cell.value)) > max_len:
                        max_len = len(str(cell.value))
                except:
                    pass
            data_sheet.column_dimensions[col_letter].width = min(max_len + 2, 30)

        # Лист с графиками
        chart_sheet = wb.create_sheet("Графики")
        row_pos = 2
        temp_images = []  # для хранения временных файлов, чтобы удалить после сохранения

        for metric in metrics:
            rows = history_db.get_metrics_for_device(device_id, metric, start_ts, end_ts)
            if not rows or len(rows) < 2:
                continue
            timestamps = [datetime.datetime.fromtimestamp(r[0]) for r in rows]
            values = [r[3] for r in rows]
            # Удаляем None значения
            valid = [(t, v) for t, v in zip(timestamps, values) if v is not None]
            if not valid or len(valid) < 2:
                continue
            timestamps, values = zip(*valid)

            # Создаём график
            plt.figure(figsize=(8, 4))
            plt.plot(timestamps, values, marker='o', linestyle='-', linewidth=1, markersize=3, color='#2E75B6')
            plt.title(f"{metric} – {device_name}")
            plt.xlabel("Время")
            ylabel = f"Значение {rows[0][4] if rows[0][4] else ''}"
            plt.ylabel(ylabel)
            plt.grid(True, linestyle='--', alpha=0.6)
            plt.xticks(rotation=45)
            plt.tight_layout()
            # Сохраняем во временный файл
            tmp_path = tempfile.mktemp(suffix=".png")
            plt.savefig(tmp_path, dpi=100)
            plt.close()
            temp_images.append(tmp_path)
            # Вставляем изображение
            img = openpyxl.drawing.image.Image(tmp_path)
            img.anchor = f'A{row_pos}'
            chart_sheet.add_image(img)
            # Заголовок под графиком
            chart_sheet.cell(row=row_pos + int(img.height / 15) + 2, column=1, value=metric).font = Font(bold=True)
            row_pos += int(img.height / 15) + 6

        # Сохраняем книгу
        wb.save(output_path)

        # Удаляем временные файлы изображений
        for tmp in temp_images:
            try:
                os.unlink(tmp)
            except:
                pass

        return True, output_path

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"Ошибка при создании отчёта: {str(e)}"
def show_report_dialog(parent, devices_dict: Dict[str, Device]):
    import tkinter as tk
    from tkinter import ttk, messagebox
    # Полный словарь цветов, совместимый с main.py
    C = {
        'bg': '#1a1a2e',
        'bg2': '#23233a',
        'bg3': '#2d2d44',
        'text': '#ffffff',
        'text_dim': '#b0b0d0',
        'accent': '#ffb86c',
        'border': '#444466',
        'online': '#50fa7b'
    }

    dialog = tk.Toplevel(parent)
    dialog.title("Генерация отчёта")
    dialog.geometry("520x550")
    dialog.configure(bg=C['bg'])
    dialog.transient(parent)
    dialog.grab_set()

    # Устройство
    tk.Label(dialog, text="Устройство:", bg=C['bg'], fg=C['text']).pack(anchor="w", padx=20, pady=(15,5))
    device_frame = tk.Frame(dialog, bg=C['bg'])
    device_frame.pack(fill="x", padx=20)
    device_var = tk.StringVar()
    device_combo = ttk.Combobox(device_frame, textvariable=device_var, state="readonly", width=40)
    device_list = [(dev_id, dev.name) for dev_id, dev in devices_dict.items() if dev.snmp_enabled]
    device_combo['values'] = [f"{name} ({dev_id})" for dev_id, name in device_list]
    if device_list:
        device_combo.current(0)
    device_combo.pack(fill="x")

    # Период
    tk.Label(dialog, text="Выберите период:", bg=C['bg'], fg=C['text']).pack(anchor="w", padx=20, pady=(15,5))
    period_var = tk.StringVar(value="day")
    period_frame = tk.Frame(dialog, bg=C['bg'])
    period_frame.pack(fill="x", padx=20)
    periods = [("Последний час", "hour"), ("Последний день", "day"), ("Последняя неделя", "week"), ("Последний месяц", "month"), ("Произвольный", "custom")]
    for text, val in periods:
        tk.Radiobutton(period_frame, text=text, variable=period_var, value=val, bg=C['bg'], fg=C['text'], selectcolor=C['bg2'], activebackground=C['bg']).pack(anchor="w")

    # Кастомные даты
    custom_frame = tk.Frame(dialog, bg=C['bg'])
    custom_frame.pack(fill="x", padx=20, pady=10)
    tk.Label(custom_frame, text="С (ГГГГ-ММ-ДД):", bg=C['bg'], fg=C['text']).grid(row=0, column=0, sticky="w")
    start_entry = tk.Entry(custom_frame, width=12, bg=C['bg3'], fg=C['text'], insertbackground=C['text'])
    start_entry.grid(row=0, column=1, padx=5)
    tk.Label(custom_frame, text="По (ГГГГ-ММ-ДД):", bg=C['bg'], fg=C['text']).grid(row=0, column=2, sticky="w", padx=5)
    end_entry = tk.Entry(custom_frame, width=12, bg=C['bg3'], fg=C['text'], insertbackground=C['text'])
    end_entry.grid(row=0, column=3)

    # Метрики
    tk.Label(dialog, text="Метрики (оставьте пустым для всех числовых):", bg=C['bg'], fg=C['text']).pack(anchor="w", padx=20, pady=(10,0))
    metrics_text = tk.Text(dialog, height=4, width=55, bg=C['bg3'], fg=C['text'], insertbackground=C['text'])
    metrics_text.pack(padx=20, pady=5)
    tk.Label(dialog, text="Укажите названия метрик через запятую (например: Temperature, Humidity)", bg=C['bg'], fg=C['text_dim'], font=("Consolas", 8)).pack(anchor="w", padx=20)

    # Кнопки
    btn_frame = tk.Frame(dialog, bg=C['bg'])
    btn_frame.pack(fill="x", pady=20)

    def do_generate():
        sel = device_combo.get()
        if not sel:
            messagebox.showerror("Ошибка", "Выберите устройство", parent=dialog)
            return
        dev_id = sel.split("(")[-1].rstrip(")")
        dev = devices_dict.get(dev_id)
        if not dev:
            messagebox.showerror("Ошибка", "Устройство не найдено", parent=dialog)
            return

        now = datetime.datetime.now()
        period = period_var.get()
        if period == "hour":
            start = now - datetime.timedelta(hours=1)
            end = now
        elif period == "day":
            start = now - datetime.timedelta(days=1)
            end = now
        elif period == "week":
            start = now - datetime.timedelta(days=7)
            end = now
        elif period == "month":
            start = now - datetime.timedelta(days=30)
            end = now
        else:
            try:
                start = datetime.datetime.strptime(start_entry.get(), "%Y-%m-%d")
                end = datetime.datetime.strptime(end_entry.get(), "%Y-%m-%d") + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
            except Exception as e:
                messagebox.showerror("Ошибка", f"Неверный формат даты: {e}\nИспользуйте ГГГГ-ММ-ДД", parent=dialog)
                return
            if start > end:
                messagebox.showerror("Ошибка", "Начальная дата позже конечной", parent=dialog)
                return

        metrics_str = metrics_text.get("1.0", tk.END).strip()
        metrics = [m.strip() for m in metrics_str.split(",") if m.strip()] if metrics_str else None

        dialog.destroy()
        def run():
            success, result = generate_report(dev_id, dev.name, start, end, metrics)
            if success:
                messagebox.showinfo("Отчёт готов", f"Файл сохранён:\n{result}", parent=parent)
                import subprocess
                import platform
                if platform.system() == "Windows":
                    os.startfile(result)
                elif platform.system() == "Darwin":
                    subprocess.call(["open", result])
                else:
                    subprocess.call(["xdg-open", result])
            else:
                messagebox.showerror("Ошибка", result, parent=parent)
        threading.Thread(target=run, daemon=True).start()

    tk.Button(btn_frame, text="Сгенерировать", command=do_generate, bg=C['accent'], fg=C['bg'], padx=12, pady=4).pack(side="right", padx=10)
    tk.Button(btn_frame, text="Отмена", command=dialog.destroy, bg=C['bg3'], fg=C['text'], padx=12, pady=4).pack(side="right")
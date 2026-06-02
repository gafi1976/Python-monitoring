"""IP range scanner dialog"""

import tkinter as tk
from tkinter import ttk
import threading
import subprocess
import platform
import ipaddress
import socket
import time
import queue
from typing import Optional


class ScanDialog:
    def __init__(self, parent, colors: dict):
        self.colors = C = colors
        self.result: list[str] = []
        self.scanning = False
        self.scan_thread: Optional[threading.Thread] = None
        self.q: queue.Queue = queue.Queue()

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Сканирование IP диапазона")
        self.dialog.geometry("560x600")
        self.dialog.configure(bg=C["bg"])
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, True)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._poll()

    def _build(self):
        C = self.colors

        # Header
        hdr = tk.Frame(self.dialog, bg=C["bg2"], pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🔍 Сканирование IP диапазона",
                 font=("Consolas", 13, "bold"),
                 bg=C["bg2"], fg=C["accent"],
                 padx=20).pack(side="left")

        # Form
        form = tk.Frame(self.dialog, bg=C["bg"], pady=8)
        form.pack(fill="x", padx=20)
        form.columnconfigure(1, weight=1)

        self._row(form, "Начальный IP:", "192.168.1.1",  0, "var_start")
        self._row(form, "Конечный IP:",  "192.168.1.254", 1, "var_end")

        # Timeout
        tk.Label(form, text="Таймаут (сек):", font=("Consolas", 10),
                 bg=C["bg"], fg=C["text_dim"], anchor="w"
                 ).grid(row=2, column=0, sticky="w", pady=6)
        self.var_timeout = tk.StringVar(value="0.5")
        tk.Entry(form, textvariable=self.var_timeout,
                 font=("Consolas", 10), width=10,
                 bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["border"],
                 highlightcolor=C["accent"]
                 ).grid(row=2, column=1, sticky="w", pady=6, padx=(8, 0))

        # Threads
        tk.Label(form, text="Потоков:", font=("Consolas", 10),
                 bg=C["bg"], fg=C["text_dim"], anchor="w"
                 ).grid(row=3, column=0, sticky="w", pady=6)
        self.var_threads = tk.StringVar(value="50")
        tk.Entry(form, textvariable=self.var_threads,
                 font=("Consolas", 10), width=10,
                 bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["border"],
                 highlightcolor=C["accent"]
                 ).grid(row=3, column=1, sticky="w", pady=6, padx=(8, 0))

        # Method
        tk.Label(form, text="Метод:", font=("Consolas", 10),
                 bg=C["bg"], fg=C["text_dim"], anchor="w"
                 ).grid(row=4, column=0, sticky="w", pady=6)
        self.var_method = tk.StringVar(value="ping")
        methods = tk.Frame(form, bg=C["bg"])
        methods.grid(row=4, column=1, sticky="w", pady=6, padx=(8, 0))

        for val, label in [("ping", "🏓 Ping"), ("tcp", "🔌 TCP порт 80")]:
            tk.Radiobutton(methods, text=label, variable=self.var_method,
                           value=val,
                           font=("Consolas", 10),
                           bg=C["bg"], fg=C["text"],
                           activebackground=C["bg"],
                           activeforeground=C["accent"],
                           selectcolor=C["bg3"],
                           cursor="hand2"
                           ).pack(side="left", padx=4)

        # Divider
        tk.Frame(self.dialog, bg=C["border"], height=1).pack(fill="x", padx=20, pady=4)

        # Progress
        prog_frame = tk.Frame(self.dialog, bg=C["bg"])
        prog_frame.pack(fill="x", padx=20, pady=4)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(prog_frame,
                                        variable=self.progress_var,
                                        maximum=100,
                                        length=500)

        style = ttk.Style()
        style.configure("Scan.Horizontal.TProgressbar",
                        troughcolor=C["bg3"],
                        background=C["accent"],
                        borderwidth=0)
        self.progress.config(style="Scan.Horizontal.TProgressbar")
        self.progress.pack(fill="x")

        self.progress_lbl = tk.Label(prog_frame, text="Готово к сканированию",
                                     font=("Consolas", 9),
                                     bg=C["bg"], fg=C["text_dim"])
        self.progress_lbl.pack(anchor="w", pady=2)

        # Results
        result_hdr = tk.Frame(self.dialog, bg=C["bg"])
        result_hdr.pack(fill="x", padx=20, pady=(4, 0))
        tk.Label(result_hdr, text="Найденные хосты:",
                 font=("Consolas", 10, "bold"),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self.found_count_lbl = tk.Label(result_hdr, text="0",
                                        font=("Consolas", 10, "bold"),
                                        bg=C["bg"], fg=C["online"])
        self.found_count_lbl.pack(side="left", padx=8)

        list_frame = tk.Frame(self.dialog, bg=C["bg"])
        list_frame.pack(fill="both", expand=True, padx=20, pady=4)

        scrollbar = tk.Scrollbar(list_frame, bg=C["bg3"])
        scrollbar.pack(side="right", fill="y")

        self.result_listbox = tk.Listbox(
            list_frame,
            bg=C["bg2"], fg=C["online"],
            selectbackground=C["selection"],
            relief="flat", bd=0,
            font=("Consolas", 10),
            yscrollcommand=scrollbar.set
        )
        self.result_listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.result_listbox.yview)

        # Buttons
        btn_row = tk.Frame(self.dialog, bg=C["bg"], pady=10)
        btn_row.pack(fill="x", padx=20)

        self.scan_btn = tk.Button(btn_row, text="▶ Начать сканирование",
                                  command=self._start_scan,
                                  bg=C["accent"], fg="white",
                                  activebackground=C["accent2"],
                                  relief="flat", bd=0,
                                  font=("Consolas", 10, "bold"),
                                  padx=14, pady=7, cursor="hand2")
        self.scan_btn.pack(side="left", padx=4)

        self.stop_btn = tk.Button(btn_row, text="⏹ Стоп",
                                  command=self._stop_scan,
                                  bg=C["bg3"], fg=C["danger"],
                                  activebackground=C["border"],
                                  relief="flat", bd=0,
                                  font=("Consolas", 10),
                                  padx=14, pady=7, cursor="hand2",
                                  state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        tk.Button(btn_row, text="✓ Добавить выбранные",
                  command=self._add_selected,
                  bg=C["accent2"], fg="white",
                  activebackground="#2d8f40",
                  relief="flat", bd=0,
                  font=("Consolas", 10, "bold"),
                  padx=14, pady=7, cursor="hand2"
                  ).pack(side="right", padx=4)

        tk.Button(btn_row, text="✓ Добавить все",
                  command=self._add_all,
                  bg=C["bg3"], fg=C["text"],
                  activebackground=C["border"],
                  relief="flat", bd=0,
                  font=("Consolas", 10),
                  padx=14, pady=7, cursor="hand2"
                  ).pack(side="right", padx=4)

    def _row(self, parent, label, default, row, attr):
        C = self.colors
        tk.Label(parent, text=label, font=("Consolas", 10),
                 bg=C["bg"], fg=C["text_dim"], anchor="w"
                 ).grid(row=row, column=0, sticky="w", pady=6)
        var = tk.StringVar(value=default)
        setattr(self, attr, var)
        tk.Entry(parent, textvariable=var,
                 font=("Consolas", 10),
                 bg=C["bg3"], fg=C["text"],
                 insertbackground=C["accent"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["border"],
                 highlightcolor=C["accent"]
                 ).grid(row=row, column=1, sticky="ew", pady=6, padx=(8, 0))

    # ─── Scan Logic ───────────────────────────────────────────────────────────

    def _start_scan(self):
        try:
            start_ip = ipaddress.ip_address(self.var_start.get().strip())
            end_ip   = ipaddress.ip_address(self.var_end.get().strip())
        except ValueError as e:
            tk.messagebox.showerror("Ошибка", f"Неверный IP адрес: {e}",
                                    parent=self.dialog)
            return

        if int(end_ip) < int(start_ip):
            tk.messagebox.showerror("Ошибка", "Конечный IP должен быть больше начального",
                                    parent=self.dialog)
            return

        try:
            timeout = float(self.var_timeout.get())
            threads = int(self.var_threads.get())
        except ValueError:
            timeout, threads = 0.5, 50

        self.scanning = True
        self.result_listbox.delete(0, tk.END)
        self.found_ips: list[str] = []
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        all_ips = []
        current = int(start_ip)
        end     = int(end_ip)
        while current <= end:
            all_ips.append(str(ipaddress.ip_address(current)))
            current += 1

        self.total_ips = len(all_ips)
        self.scanned_count = 0

        method = self.var_method.get()

        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(all_ips, timeout, threads, method),
            daemon=True
        )
        self.scan_thread.start()

    def _scan_worker(self, all_ips: list[str], timeout: float,
                     max_threads: int, method: str):
        import concurrent.futures

        def check(ip):
            if not self.scanning:
                return None
            if method == "ping":
                alive = self._ping_check(ip, timeout)
            else:
                alive = self._tcp_check(ip, 80, timeout)
            return ip if alive else None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = {executor.submit(check, ip): ip for ip in all_ips}
            for future in concurrent.futures.as_completed(futures):
                if not self.scanning:
                    break
                self.scanned_count += 1
                result = future.result()
                if result:
                    self.q.put(("found", result))
                self.q.put(("progress",
                            self.scanned_count / self.total_ips * 100,
                            self.scanned_count, self.total_ips))

        self.q.put(("done",))

    def _ping_check(self, ip: str, timeout: float) -> bool:
        try:
            os_name = platform.system().lower()
            if os_name == "windows":
                cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
            else:
                cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
            result = subprocess.run(cmd, capture_output=True,
                                    timeout=timeout + 2)
            return result.returncode == 0
        except Exception:
            return False

    def _tcp_check(self, ip: str, port: int, timeout: float) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _stop_scan(self):
        self.scanning = False
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.progress_lbl.config(text="Сканирование остановлено")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "found":
                    ip = item[1]
                    self.found_ips.append(ip)
                    self.result_listbox.insert(tk.END, f"  ● {ip}")
                    self.result_listbox.see(tk.END)
                    self.found_count_lbl.config(text=str(len(self.found_ips)))
                elif item[0] == "progress":
                    _, pct, done, total = item
                    self.progress_var.set(pct)
                    self.progress_lbl.config(
                        text=f"Проверено: {done}/{total}  |  Найдено: {len(self.found_ips)}"
                    )
                elif item[0] == "done":
                    self.scanning = False
                    self.scan_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.progress_var.set(100)
                    self.progress_lbl.config(
                        text=f"✓ Готово. Найдено хостов: {len(self.found_ips)}")
        except Exception:
            pass
        finally:
            if not self.dialog.winfo_exists():
                return
            self.dialog.after(200, self._poll)

    def _add_selected(self):
        sel = self.result_listbox.curselection()
        if not sel:
            # If nothing selected, add all
            self._add_all()
            return
        ips = []
        for idx in sel:
            text = self.result_listbox.get(idx).strip().lstrip("● ").strip()
            if text:
                ips.append(text)
        self.result = ips
        self.dialog.destroy()

    def _add_all(self):
        self.result = list(self.found_ips)
        self.dialog.destroy()

    def _on_close(self):
        self.scanning = False
        self.dialog.destroy()

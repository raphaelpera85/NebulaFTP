import os
import ctypes
import json
import sys
import time
import subprocess
import threading
import queue
import re
import shutil
import socket
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tools.check_deps import ensure_runtime_dependencies
    ensure_runtime_dependencies()
except RuntimeError as exc:
    raise SystemExit(f"[deps] {exc}") from None

import pystray
from PIL import Image, ImageDraw
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

_INSTANCE_MUTEX = None


def acquire_single_instance():
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\NebulaFTP_GUI")
    return bool(_INSTANCE_MUTEX) and kernel32.GetLastError() != 183


def get_python_exe() -> str:
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        target = exe[:-5] + ".exe"
        if os.path.exists(target):
            return target
    return exe


class NebulaGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("NebulaFTP Server & Feeder")
        self.root.geometry("850x650")
        self.root.minsize(750, 550)
        # Configuração de Estilo Windows / Dark Modern
        self.style = ttk.Style()
        self.style.theme_use("clam")

        # Cores e fontes
        self.bg_color = "#1e1e2e"
        self.fg_color = "#cdd6f4"
        self.card_bg = "#313244"
        self.accent_color = "#89b4fa"
        self.success_color = "#a6e3a1"
        self.stop_color = "#f38ba8"

        self.root.configure(bg=self.bg_color)

        self.proc_server = None
        self.proc_feed = None
        self.proc_mount = None
        self.proc_cleanup = None
        self.server_ready = threading.Event()
        self.is_running = False
        self.stream_only = False
        self.auto_restart_attempts = 0
        self.turbo_active = False
        self.turbo_closed_apps = []

        self.mongo_uri = os.environ.get("MONGODB", "mongodb://localhost:27017")

        self._create_widgets()
        self._create_tray()
        self._start_status_loop()
        self.root.after(5000, self._check_idle_turbo)

    def _create_tray(self):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill="#89b4fa")
        draw.text((20, 13), "N", fill="#11111b", stroke_width=1)
        self.tray_icon = pystray.Icon(
            "NebulaFTP",
            image,
            "NebulaFTP",
            menu=pystray.Menu(
                pystray.MenuItem("Abrir", self._restore_from_tray, default=True),
                pystray.MenuItem("Sair", self._quit_application),
            ),
        )
        self.tray_icon.run_detached()
        self.root.protocol("WM_DELETE_WINDOW", self._confirm_close_or_minimize)
        self.root.bind("<Unmap>", self._on_minimize)

    def _confirm_close_or_minimize(self):
        close_app = messagebox.askyesno(
            "Fechar NebulaFTP",
            "Deseja fechar completamente o NebulaFTP?\n\n"
            "Sim: encerrar o programa e os servicos.\n"
            "Nao: minimizar para a area de notificacoes.",
            parent=self.root,
        )
        if close_app:
            self._finish_quit()
        else:
            self._hide_to_tray()

    def _on_minimize(self, _event):
        if self.root.state() == "iconic":
            self.root.after(100, self._hide_to_tray)

    def _hide_to_tray(self):
        self.root.withdraw()

    def _restore_from_tray(self, _icon=None, _item=None):
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _quit_application(self, _icon=None, _item=None):
        self.root.after(0, self._finish_quit)

    def _finish_quit(self):
        self.stop_services()
        self.tray_icon.stop()
        self.root.destroy()

    def _create_widgets(self):
        # Header / Status
        header_frame = tk.Frame(self.root, bg=self.card_bg, pady=10, padx=15)
        header_frame.pack(fill="x", padx=15, pady=10)

        title_lbl = tk.Label(header_frame, text="NebulaFTP Dashboard", font=("Segoe UI", 16, "bold"), bg=self.card_bg, fg=self.accent_color)
        title_lbl.pack(side="left")

        self.status_lbl = tk.Label(header_frame, text="Parado", font=("Segoe UI", 11, "bold"), bg=self.card_bg, fg=self.stop_color)
        self.status_lbl.pack(side="right")
        # Painel Seleção de Pasta / Monitoramento
        folder_frame = tk.Frame(self.root, bg=self.card_bg, pady=10, padx=15)
        folder_frame.pack(fill="x", padx=15, pady=5)

        folder_lbl = tk.Label(folder_frame, text="Pasta/Disco para Monitorar:", font=("Segoe UI", 10), bg=self.card_bg, fg=self.fg_color)
        folder_lbl.pack(anchor="w")

        entry_btn_frame = tk.Frame(folder_frame, bg=self.card_bg)
        entry_btn_frame.pack(fill="x", pady=5)

        self.path_var = tk.StringVar(value=os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "D:/midias")))
        self.path_entry = tk.Entry(entry_btn_frame, textvariable=self.path_var, font=("Consolas", 10), bg="#181825", fg=self.fg_color, insertbackground="white", bd=1, relief="solid")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 10))

        browse_btn = tk.Button(entry_btn_frame, text="Adicionar Pasta", command=self._browse_folder, font=("Segoe UI", 9, "bold"), bg="#45475a", fg="white", bd=0, padx=12, pady=4, cursor="hand2")
        browse_btn.pack(side="right")
        multi_frame = tk.Frame(folder_frame, bg=self.card_bg, pady=2)
        multi_frame.pack(fill="x", pady=(0, 4))

        multi_header = tk.Frame(multi_frame, bg=self.card_bg)
        multi_header.pack(fill="x")
        multi_lbl = tk.Label(multi_header, text="Lista de pastas ativas:", font=("Segoe UI", 9), bg=self.card_bg, fg=self.fg_color)
        multi_lbl.pack(side="left")
        multi_hint = tk.Label(multi_header, text="use Adicionar Pasta para incluir mais uma", font=("Segoe UI", 8), bg=self.card_bg, fg="#a6adc8")
        multi_hint.pack(side="right")

        multi_body = tk.Frame(multi_frame, bg="#181825", bd=1, relief="solid")
        multi_body.pack(fill="x", pady=(4, 0))

        self.paths_listbox = tk.Listbox(
            multi_body,
            height=4,
            font=("Consolas", 10),
            bg="#181825",
            fg=self.fg_color,
            selectbackground=self.accent_color,
            selectforeground="#11111b",
            bd=0,
            highlightthickness=0,
            selectmode="extended",
            exportselection=False,
            activestyle="none",
        )
        multi_scroll = ttk.Scrollbar(multi_body, orient="vertical", command=self.paths_listbox.yview)
        self.paths_listbox.configure(yscrollcommand=multi_scroll.set)
        self.paths_listbox.pack(side="left", fill="x", expand=True)
        multi_scroll.pack(side="right", fill="y")

        list_actions = tk.Frame(folder_frame, bg=self.card_bg)
        list_actions.pack(fill="x", pady=(4, 0))
        remove_btn = tk.Button(
            list_actions,
            text="Remover Selecionada",
            command=self._remove_selected_folder,
            font=("Segoe UI", 9, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=12,
            pady=4,
            cursor="hand2",
        )
        remove_btn.pack(side="left")

        self._load_initial_paths()

        stage_frame = tk.Frame(folder_frame, bg=self.card_bg, pady=4)
        stage_frame.pack(fill="x")
        tk.Label(
            stage_frame, text="Pastas de staging:", font=("Segoe UI", 9),
            bg=self.card_bg, fg=self.fg_color,
        ).pack(anchor="w")
        stage_input = tk.Frame(stage_frame, bg=self.card_bg)
        stage_input.pack(fill="x", pady=(3, 0))
        default_stage = "F:\\NebulaStage" if os.path.isdir("F:\\") else os.path.abspath("staging")
        raw_stages = os.environ.get("STAGING_DIRS", os.environ.get("STAGING_DIR", default_stage))
        self.stage_settings_file = os.path.abspath("stage_paths.json")
        try:
            saved_stages = json.loads(open(self.stage_settings_file, encoding="utf-8").read())
            if isinstance(saved_stages, list) and saved_stages:
                raw_stages = ";".join(str(path) for path in saved_stages)
        except (OSError, ValueError):
            pass
        self.stage_listbox = tk.Listbox(
            stage_input, height=2, font=("Consolas", 10),
            bg="#181825", fg=self.fg_color,
            selectbackground=self.accent_color, selectforeground="#11111b",
            exportselection=False,
        )
        self.stage_listbox.pack(side="left", fill="x", expand=True, padx=(0, 10))
        for stage in raw_stages.split(";"):
            if stage.strip():
                self.stage_listbox.insert(tk.END, os.path.normpath(stage.strip()))
        stage_actions = tk.Frame(stage_input, bg=self.card_bg)
        stage_actions.pack(side="right")
        tk.Button(
            stage_actions, text="Adicionar Stage", command=self._browse_stage,
            font=("Segoe UI", 9, "bold"), bg="#45475a", fg="white",
            bd=0, padx=12, pady=4, cursor="hand2",
        ).pack(fill="x")
        tk.Button(
            stage_actions, text="Remover Stage", command=self._remove_selected_stage,
            font=("Segoe UI", 9, "bold"), bg="#45475a", fg="white",
            bd=0, padx=12, pady=4, cursor="hand2",
        ).pack(fill="x", pady=(3, 0))
        # Botões de Controle
        ctrl_frame = tk.Frame(self.root, bg=self.bg_color, pady=5)
        ctrl_frame.pack(fill="x", padx=15)

        self.btn_start = tk.Button(ctrl_frame, text="Iniciar NebulaFTP", command=self.start_services, font=("Segoe UI", 11, "bold"), bg=self.success_color, fg="#11111b", bd=0, padx=20, pady=6, cursor="hand2")
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_stream = tk.Button(ctrl_frame, text="Somente Streaming", command=lambda: self.start_services(stream_only=True), font=("Segoe UI", 11, "bold"), bg=self.accent_color, fg="#11111b", bd=0, padx=16, pady=6, cursor="hand2")
        self.btn_stream.pack(side="left", padx=(0, 10))

        self.btn_stop = tk.Button(ctrl_frame, text="Parar", command=self.stop_services, font=("Segoe UI", 11, "bold"), bg=self.stop_color, fg="#11111b", bd=0, padx=20, pady=6, cursor="hand2", state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 10))

        self.btn_strm = tk.Button(ctrl_frame, text="Gerar Biblioteca STRM", command=self.generate_strm, font=("Segoe UI", 11, "bold"), bg=self.accent_color, fg="#11111b", bd=0, padx=16, pady=6, cursor="hand2")
        self.btn_strm.pack(side="left")

        turbo_frame = tk.Frame(self.root, bg=self.bg_color)
        turbo_frame.pack(fill="x", padx=15)
        self.turbo_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(
            turbo_frame,
            text="Modo Turbo quando o PC estiver ocioso",
            variable=self.turbo_enabled,
            bg=self.bg_color,
            fg=self.fg_color,
            selectcolor="#181825",
            activebackground=self.bg_color,
            activeforeground=self.fg_color,
        ).pack(side="left")
        tk.Label(
            turbo_frame, text="Ativar apos", bg=self.bg_color, fg=self.fg_color,
        ).pack(side="left", padx=(12, 4))
        self.turbo_idle_minutes = tk.IntVar(
            value=max(1, int(os.environ.get("TURBO_IDLE_MINUTES", "10")))
        )
        tk.Spinbox(
            turbo_frame,
            from_=1,
            to=120,
            width=4,
            textvariable=self.turbo_idle_minutes,
            bg="#181825",
            fg=self.fg_color,
            buttonbackground="#45475a",
        ).pack(side="left")
        tk.Label(
            turbo_frame, text="minutos", bg=self.bg_color, fg=self.fg_color,
        ).pack(side="left", padx=(4, 0))

        # Container Principal Dividido Lado a Lado (PanedWindow)
        main_paned = ttk.PanedWindow(self.root, orient="horizontal")
        main_paned.pack(fill="both", expand=True, padx=15, pady=10)

        # Coluna Esquerda: Uploads Ativos em Tempo Real
        progress_frame = tk.LabelFrame(main_paned, text=" Transferencias Ativas em Tempo Real ", font=("Segoe UI", 10, "bold"), bg=self.bg_color, fg=self.accent_color, pady=5, padx=10)
        main_paned.add(progress_frame, weight=1)

        # Canvas com Scrollbar para os Cards de Upload
        self.canvas = tk.Canvas(progress_frame, bg="#181825", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(progress_frame, orient="vertical", command=self.canvas.yview)
        self.scroll_content = tk.Frame(self.canvas, bg="#181825")

        self.scroll_content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scroll_content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Coluna Direita: Logs do Sistema (UTF-8)
        log_frame = tk.LabelFrame(main_paned, text=" Logs do Sistema ", font=("Segoe UI", 10, "bold"), bg=self.bg_color, fg=self.fg_color, pady=5, padx=10)
        main_paned.add(log_frame, weight=1)

        self.log_text = tk.Text(log_frame, font=("Consolas", 9), bg="#11111b", fg="#a6adc8", bd=0, wrap="word")
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")

        self.file_bars = {}
        self.strm_downloads = {}

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.path_var.get())
        if folder:
            self.path_var.set(folder)
            if hasattr(self, "paths_listbox"):
                current = set(self.paths_listbox.get(0, tk.END))
                if folder not in current:
                    self.paths_listbox.insert(tk.END, folder)

    def _browse_stage(self):
        folder = filedialog.askdirectory()
        if folder:
            current = set(self.stage_listbox.get(0, tk.END))
            if folder not in current:
                self.stage_listbox.insert(tk.END, folder)
                self._save_stage_paths()

    def _remove_selected_stage(self):
        for index in reversed(self.stage_listbox.curselection()):
            self.stage_listbox.delete(index)
        self._save_stage_paths()

    def _get_stage_paths(self):
        return [self.stage_listbox.get(index) for index in range(self.stage_listbox.size())]

    def _save_stage_paths(self):
        with open(self.stage_settings_file, "w", encoding="utf-8") as handle:
            json.dump(self._get_stage_paths(), handle, ensure_ascii=False, indent=2)

    def _load_initial_paths(self):
        raw_paths = os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "D:/midias"))
        for chunk in re.split(r"[;,\n]", raw_paths):
            path = chunk.strip().strip('"')
            if path:
                self._add_path(path)

    def _add_path(self, folder):
        folder = os.path.normpath(folder)
        if hasattr(self, "path_var"):
            self.path_var.set(folder)
        if hasattr(self, "paths_listbox"):
            current = set(self.paths_listbox.get(0, tk.END))
            if folder and folder not in current:
                self.paths_listbox.insert(tk.END, folder)

    def _remove_selected_folder(self):
        if not hasattr(self, "paths_listbox"):
            return
        selection = list(self.paths_listbox.curselection())
        if not selection:
            return
        for index in reversed(selection):
            self.paths_listbox.delete(index)
        if self.paths_listbox.size():
            self.path_var.set(self.paths_listbox.get(0))
        else:
            self.path_var.set("")

    def _clear_folders(self):
        if hasattr(self, "paths_listbox"):
            self.paths_listbox.delete(0, tk.END)

    def _get_monitor_paths(self):
        if hasattr(self, "paths_listbox"):
            return [self.paths_listbox.get(i) for i in range(self.paths_listbox.size())]
        return [part.strip().strip('"') for part in re.split(r"[;,\n]", self.path_var.get()) if part.strip()]

    def log(self, message):
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")

    @staticmethod
    def _windows_idle_seconds():
        if os.name != "nt":
            return 0

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000

    @staticmethod
    def _set_process_priority(proc, turbo):
        if os.name != "nt" or not proc or proc.poll() is not None:
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x0200, False, proc.pid)
        if handle:
            try:
                kernel32.SetPriorityClass(handle, 0x00008000 if turbo else 0x00000020)
            finally:
                kernel32.CloseHandle(handle)

    def _close_apps_for_turbo(self):
        names = ("chrome.exe", "Telegram.exe", "WhatsApp.Root.exe", "Taskmgr.exe")
        script = (
            "$names=@(" + ",".join(f"'{name}'" for name in names) + ");"
            "Get-CimInstance Win32_Process | Where-Object {$_.Name -in $names} | "
            "Where-Object {$_.ExecutablePath} | Select-Object Name,ExecutablePath | "
            "Sort-Object Name -Unique | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            found = json.loads(result.stdout or "[]")
        except ValueError:
            found = []
        if isinstance(found, dict):
            found = [found]

        self.turbo_closed_apps = [
            item for item in found
            if isinstance(item, dict) and os.path.isfile(item.get("ExecutablePath", ""))
        ]
        for item in self.turbo_closed_apps:
            subprocess.run(
                ["taskkill", "/IM", item["Name"], "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return [item["Name"] for item in self.turbo_closed_apps]

    def _restore_apps_after_turbo(self):
        restored = []
        for item in self.turbo_closed_apps:
            try:
                subprocess.Popen(
                    [item["ExecutablePath"]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                restored.append(item["Name"])
            except OSError:
                pass
        self.turbo_closed_apps = []
        return restored

    def _check_idle_turbo(self):
        try:
            threshold = max(1, self.turbo_idle_minutes.get()) * 60
        except (tk.TclError, ValueError):
            threshold = 600
        active = (
            self.turbo_enabled.get()
            and self.is_running
            and self._windows_idle_seconds() >= threshold
        )

        if active != self.turbo_active:
            self.turbo_active = active
            for proc in (self.proc_server, self.proc_feed, self.proc_mount, self.proc_cleanup):
                self._set_process_priority(proc, active)
            if self.is_running:
                if active:
                    closed = self._close_apps_for_turbo()
                    detail = ", ".join(closed) if closed else "nenhum aplicativo aberto"
                    message = f"[TURBO] PC ocioso; prioridade aumentada. Fechados: {detail}."
                else:
                    restored = self._restore_apps_after_turbo()
                    detail = ", ".join(restored) if restored else "nenhum aplicativo"
                    message = f"[TURBO] Atividade detectada; prioridade normal. Reabertos: {detail}."
                self.log(message)
        elif active:
            for proc in (self.proc_server, self.proc_feed, self.proc_mount, self.proc_cleanup):
                self._set_process_priority(proc, True)

        self.root.after(5000, self._check_idle_turbo)

    def start_services(self, stream_only=False, automatic=False):
        if self.is_running:
            return
        if not automatic:
            self.auto_restart_attempts = 0
        for port in (2121, 2122):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    messagebox.showerror(
                        "NebulaFTP ja esta executando",
                        f"A porta {port} ja esta em uso. Abra a instancia do Nebula "
                        "que esta na area de notificacoes ou encerre-a antes de iniciar outra.",
                    )
                    return
            except OSError:
                pass

        sources = self._get_monitor_paths()
        if not stream_only and not sources:
            messagebox.showerror("Erro", "Informe ao menos uma pasta para monitorar.")
            return
        missing = [] if stream_only else [source for source in sources if not os.path.exists(source)]
        if missing:
            messagebox.showerror("Erro", "Caminho(s) não encontrado(s):\n" + "\n".join(missing))
            return

        stage_dirs = [os.path.abspath(path) for path in self._get_stage_paths()]
        if not stage_dirs:
            messagebox.showerror("Erro", "Adicione ao menos uma pasta de staging.")
            return
        try:
            for stage_dir in stage_dirs:
                os.makedirs(stage_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Erro", f"Nao foi possivel criar uma pasta de staging:\n{exc}")
            return

        self.is_running = True
        self.stream_only = stream_only
        self.server_ready.clear()
        self.btn_start.config(state="disabled")
        self.btn_stream.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status_lbl.config(
            text="Somente Streaming" if stream_only else "Executando uploads",
            fg=self.success_color,
        )

        self.log("Iniciando servidor somente para streaming." if stream_only else "Iniciando NebulaFTP Server e Monitor nas pastas: " + " | ".join(sources))

        # Inicia Server main.py em subprocesso oculto
        py_exe = get_python_exe()
        server_env = os.environ.copy()
        server_env["STREAM_ONLY"] = "true" if stream_only else "false"
        server_env["STAGING_DIRS"] = ";".join(stage_dirs)
        self.proc_server = subprocess.Popen(
            [py_exe, "-u", "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=server_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )

        # Inicia Feed_ftp.py em subprocesso oculto
        self.proc_feed = None
        if not stream_only:
            feed_cmd = [py_exe, "-u", "tools/feed_ftp.py"]
            for source in sources:
                feed_cmd.extend(["--source", source])
            feed_cmd.extend(["--direct-mongo", "--workers", "2", "--watch", "--max-active", "60", "--poll-seconds", "60", "--delete-source", "--prune-completed-strm"])
            threading.Thread(target=self._start_feed_when_ready, args=(feed_cmd, server_env), daemon=True).start()

        threading.Thread(target=self._read_stream, args=(self.proc_server, "SERVER"), daemon=True).start()
        threading.Thread(target=self._mount_drive_when_ready, daemon=True).start()

        # Inicia Bot de Limpeza de Midias Concluidas (clean_already_sent.py) - apenas em discos locais
        cleanup_cmd = [py_exe, "-u", "tools/clean_already_sent.py", "--sources"] + sources + ["--interval", "30"]
        try:
            self.proc_cleanup = subprocess.Popen(
                cleanup_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=server_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            threading.Thread(target=self._read_stream, args=(self.proc_cleanup, "CLEANUP"), daemon=True).start()
            self.log("[CLEANUP] Bot de limpeza continua ativado.")
        except Exception as exc:
            self.log(f"[CLEANUP] Erro ao iniciar bot de limpeza: {exc}")

    def _start_feed_when_ready(self, feed_cmd, runtime_env):
        while self.is_running and self.proc_server and self.proc_server.poll() is None:
            if self.server_ready.wait(timeout=1):
                self.root.after(0, self.log, "[FEED] Iniciando monitoramento de arquivos (Feeder)...")
                try:
                    self.proc_feed = subprocess.Popen(
                        feed_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=runtime_env,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    threading.Thread(target=self._read_stream, args=(self.proc_feed, "FEED"), daemon=True).start()
                except Exception as exc:
                    self.root.after(0, self.log, f"[FEED] Erro ao iniciar Feeder: {exc}")
                return
        if self.is_running:
            self.root.after(0, self.log, "Feeder nao iniciado porque o servidor falhou.")

    def _mount_drive_when_ready(self):
        rclone = shutil.which("rclone")
        if not rclone:
            self.root.after(0, self.log, "rclone nao encontrado; unidade N: nao montada.")
            return
        for _ in range(40):
            if not self.is_running:
                return
            try:
                with socket.create_connection(("127.0.0.1", 2121), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            self.root.after(0, self.log, "FTP nao abriu em 40 segundos; unidade N: nao montada.")
            return
        if os.path.exists("N:\\"):
            self.root.after(0, self.log, "Unidade N: ja esta montada.")
            return
        self.proc_mount = subprocess.Popen(
            [
                rclone, "mount", "nebula:/", "N:",
                "--vfs-cache-mode", "full", "--dir-cache-time", "30s", "--network-mode", "--links",
                "--volname", "NebulaFTP",
                "--log-file", os.path.abspath("rclone-mount.log"),
                "--log-level", "INFO",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for _ in range(30):
            if self.proc_mount.poll() is not None:
                self.root.after(
                    0,
                    self.log,
                    f"Falha ao montar N: (rclone encerrou com codigo {self.proc_mount.returncode}). "
                    "Consulte rclone-mount.log.",
                )
                return
            if os.path.exists("N:\\"):
                self.root.after(0, self.log, "Unidade N: montada automaticamente.")
                return
            time.sleep(1)
        self.root.after(
            0,
            self.log,
            "Montagem de N: ainda esta inicializando em segundo plano.",
        )

    def generate_strm(self):
        self.log("Iniciando geração da biblioteca de arquivos .strm...")
        try:
            res = subprocess.run([sys.executable, "generate_strm.py"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            out = (res.stdout or "").strip()
            err = (res.stderr or "").strip()
            if res.returncode == 0:
                self.log(out or "Geração de arquivos .strm concluída.")
                messagebox.showinfo("Sucesso", "Biblioteca STRM gerada na pasta 'strm_library'!")
            else:
                self.log(f"Erro ao gerar STRM: {err or 'Erro desconhecido'}")
                messagebox.showerror("Erro", "Falha ao gerar arquivos .strm.")
        except Exception as exc:
            self.log(f"Falha ao executar gerador STRM: {exc}")

    def stop_services(self):
        if not self.is_running:
            return

        self.is_running = False
        for proc in (self.proc_server, self.proc_feed, self.proc_mount, self.proc_cleanup):
            self._set_process_priority(proc, False)
        if self.turbo_closed_apps:
            self._restore_apps_after_turbo()
        self.turbo_active = False
        self.log("Encerrando serviços.")
        rclone = shutil.which("rclone")
        if rclone and os.path.exists("N:\\"):
            subprocess.run(
                [rclone, "unmount", "N:"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        if self.proc_mount and self.proc_mount.poll() is None:
            self.proc_mount.terminate()
        self.proc_mount = None
        if self.proc_server:
            self.proc_server.terminate()
        if self.proc_feed:
            self.proc_feed.terminate()
        if self.proc_cleanup:
            self.proc_cleanup.terminate()
            self.proc_cleanup = None

        self.server_ready.clear()
        self.btn_start.config(state="normal")
        self.btn_stream.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_lbl.config(text="Parado", fg=self.stop_color)
        self.log("Serviços encerrados.")

    def _read_stream(self, proc, prefix):
        for line in iter(proc.stdout.readline, ""):
            if line:
                line_str = line.strip()
                self.root.after(0, self.log, f"[{prefix}] {line_str}")
                if prefix == "SERVER" and "HTTP Stream local:" in line_str:
                    self.server_ready.set()
                    self.auto_restart_attempts = 0
                if prefix == "FEED":
                    self.root.after(0, self._update_strm_download, line_str)
        proc.stdout.close()
        if prefix == "SERVER":
            return_code = proc.poll()
            self.root.after(0, self._handle_server_exit, return_code)

    def _handle_server_exit(self, return_code):
        if not self.is_running:
            return

        self.is_running = False
        self.server_ready.clear()
        for child in (self.proc_feed, self.proc_mount):
            if child and child.poll() is None:
                child.terminate()
        self.btn_start.config(state="normal")
        self.btn_stream.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_lbl.config(text="Servidor interrompido", fg=self.stop_color)
        self.log(f"[SERVER] Processo encerrou inesperadamente (codigo {return_code}).")

        if self.auto_restart_attempts >= 3:
            self.log("[SERVER] Reinicio automatico cancelado apos 3 tentativas.")
            return
        self.auto_restart_attempts += 1
        attempt = self.auto_restart_attempts
        self.log(f"[SERVER] Reiniciando automaticamente em 10 segundos ({attempt}/3).")
        self.root.after(
            10000,
            lambda: self.start_services(self.stream_only, automatic=True),
        )

    def _update_strm_download(self, line):
        started = re.search(r"\[STRM\]\[W\d+\] Iniciando: (.+)$", line)
        progress = re.search(r"\[STRM\] Baixando (.+): ([\d.]+) MB de ([\d.]+) MB \((\d+)%\)$", line)
        finished = re.search(r"\[STRM\]\[W\d+\] (?:Materializado|Reaproveitado|Falha ao materializar):? (.+)$", line)
        if started:
            name = os.path.basename(started.group(1))
            self.strm_downloads.clear()
            self.strm_downloads[f"STRM::{name}"] = {"display_name": name, "downloaded": 0, "total": 1}
        elif progress:
            name = progress.group(1)
            self.strm_downloads[f"STRM::{name}"] = {
                "display_name": name,
                "downloaded": float(progress.group(2)),
                "total": float(progress.group(3)) or 1,
            }
        elif finished:
            self.strm_downloads.clear()

    def _start_status_loop(self):
        def update():
            if self.is_running:
                try:
                    client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=1000)
                    active_docs = list(client.ftp.files.find({"status": {"$in": ["uploading", "queued"]}}, {"name": 1, "status": 1, "size": 1, "uploaded_bytes": 1, "parts": 1, "worker_id": 1, "bot_index": 1}))
                    client.close()
                    active_docs.extend(
                        {
                            "name": key,
                            "display_name": item["display_name"],
                            "status": "materializing",
                            "size": item["total"],
                            "uploaded_bytes": item["downloaded"],
                        }
                        for key, item in self.strm_downloads.items()
                    )

                    current_names = {doc["name"] for doc in active_docs}

                    # Remove barras antigas
                    for name in list(self.file_bars.keys()):
                        if name not in current_names:
                            self.file_bars[name]["frame"].destroy()
                            del self.file_bars[name]

                    # Atualiza ou cria novas barras
                    for doc in active_docs:
                        name = doc.get("name", "Arquivo")
                        status = doc.get("status", "uploading")
                        total = doc.get("size", 1) or 1
                        worker_id = doc.get("worker_id", "?")
                        
                        uploaded = doc.get("uploaded_bytes", 0)
                        parts = doc.get("parts", [])
                        bot_indexes = sorted({
                            part.get("bot_index")
                            for part in parts
                            if isinstance(part.get("bot_index"), int)
                        })
                        if bot_indexes:
                            shown_bots = ", ".join(f"#{idx}" for idx in bot_indexes[:4])
                            remaining_bots = len(bot_indexes) - 4
                            bot_text = f"Bots {shown_bots}" + (f" +{remaining_bots}" if remaining_bots else "")
                        else:
                            bot_text = "Bots rotativos"
                        if uploaded > 0 and total > 0:
                            pct = (uploaded / total) * 100
                        elif parts:
                            done_parts = len(parts)
                            total_parts = max(1, (total + (64 * 1024 * 1024) - 1) // (64 * 1024 * 1024))
                            pct = (done_parts / total_parts) * 100
                        else:
                            pct = 0.0

                        display_name = doc.get("display_name", name)
                        if status == "materializing":
                            info_text = f"Baixando STRM | {display_name[:60]}"
                        elif status == "uploading":
                            info_text = f"Uploading | Worker #{worker_id} | {bot_text} | {display_name[:50]}"
                        else:
                            info_text = f"Na fila | {display_name[:50]}"

                        if name not in self.file_bars:
                            item_frame = tk.Frame(self.scroll_content, bg="#313244", pady=6, padx=10)
                            item_frame.pack(fill="x", expand=True, pady=4, padx=5)

                            lbl_name = tk.Label(item_frame, text=info_text, font=("Segoe UI", 9, "bold"), bg="#313244", fg="#cdd6f4", anchor="w")
                            lbl_name.pack(fill="x")

                            pframe = tk.Frame(item_frame, bg="#313244")
                            pframe.pack(fill="x", pady=2)

                            pbar = ttk.Progressbar(pframe, length=400, mode="determinate")
                            pbar.pack(side="left", fill="x", expand=True, padx=(0, 10))

                            lbl_pct = tk.Label(pframe, text=f"{pct:.1f}%", font=("Consolas", 9, "bold"), bg="#313244", fg=self.accent_color, width=8)
                            lbl_pct.pack(side="right")

                            self.file_bars[name] = {"frame": item_frame, "pbar": pbar, "pct_lbl": lbl_pct, "name_lbl": lbl_name}

                        self.file_bars[name]["name_lbl"].config(text=info_text)
                        self.file_bars[name]["pbar"]["value"] = pct
                        self.file_bars[name]["pct_lbl"].config(text=f"{pct:.1f}%")

                except Exception:
                    pass

            self.root.after(1000, update)

        self.root.after(1000, update)

if __name__ == "__main__":
    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "NebulaFTP ja esta aberto",
            "Use o icone do NebulaFTP na area de notificacoes para abrir a instancia existente.",
        )
        root.destroy()
        raise SystemExit(0)
    root = tk.Tk()
    app = NebulaGUI(root)
    root.mainloop()

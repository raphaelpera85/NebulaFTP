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
    try:
        import tkinter as tk
        from tkinter import messagebox
        _err_root = tk.Tk()
        _err_root.withdraw()
        messagebox.showerror("Erro de Dependências - NebulaFTP", f"Falha ao verificar/instalar dependências:\n\n{exc}")
        _err_root.destroy()
    except Exception:
        pass
    raise SystemExit(f"[deps] {exc}") from None

import pystray
from PIL import Image, ImageDraw
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

APP_DIR = os.path.dirname(os.path.abspath(__file__))

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


def find_rclone_exe() -> str | None:
    try:
        from tools.rclone_installer import find_rclone
        exe = find_rclone()
        if exe:
            return exe
    except Exception:
        pass
    exe = shutil.which("rclone") or shutil.which("rclone.exe")
    if exe:
        return exe
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        winget_pkg = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkg):
            import glob
            matches = glob.glob(os.path.join(winget_pkg, "**", "rclone.exe"), recursive=True)
            if matches:
                return matches[0]
    for candidate in [
        os.path.join(APP_DIR, "tools", "rclone.exe"),
        r"C:\Program Files\rclone\rclone.exe",
        os.path.expanduser(r"~\rclone\rclone.exe"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None


def upload_status_label(doc: dict) -> str:
    """Describe persisted upload state without implying queued work is active."""
    status = doc.get("status")
    if status == "uploading":
        worker_id = doc.get("worker_id")
        worker = f"Worker #{worker_id}" if worker_id is not None else "Worker ativo"
        return f"Uploading | {worker}"

    if doc.get("recovery_required"):
        reason = doc.get("failed_reason")
        if reason == "local_path_missing":
            return "Aguardando arquivo local — recuperação necessária"
        if reason == "duplicate_target_preserved":
            return "Duplicado preservado — revisão necessária"
        return "Recuperação necessária — aguardando ação"

    if status == "staging":
        return "Preparado — aguardando worker de envio"
    return "Na fila — aguardando envio"


def should_display_upload_card(doc: dict) -> bool:
    """Return True only for active uploads or items requiring recovery."""
    return doc.get("status") == "uploading" or bool(doc.get("recovery_required"))


def upload_bot_indexes(parts) -> list[int]:
    """Return valid bot indexes while ignoring malformed persisted part data."""
    if not isinstance(parts, list):
        return []
    return sorted(
        {
            part["bot_index"] + 1
            for part in parts
            if isinstance(part, dict)
            and type(part.get("bot_index")) is int
        }
    )


def parse_downloader_scan_progress(line: str) -> tuple[int, int, int, int] | None:
    """Extract directory/file/candidate/batch counts from scan log lines."""
    match = re.search(
        r"Varredura em andamento: pastas=(\d+) arquivos_verificados=(\d+) "
        r"candidatos=(\d+) lotes_processados=(\d+)",
        line,
    )
    if not match:
        return None
    return tuple(int(value) for value in match.groups())



class NebulaGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("NebulaFTP & STRM Downloader")
        self.root.geometry("960x720")
        self.root.minsize(800, 600)

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
        self.warning_color = "#f9e2af"

        self.root.configure(bg=self.bg_color)

        # Customização de estilos TTK (Tabs, Progressbar, Scrollbar)
        self.style.configure("TNotebook", background=self.bg_color, borderwidth=0)
        self.style.configure(
            "TNotebook.Tab",
            background=self.card_bg,
            foreground=self.fg_color,
            padding=[20, 8],
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
        )
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", self.accent_color)],
            foreground=[("selected", "#11111b")],
        )
        self.style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#181825",
            background=self.accent_color,
            thickness=14,
        )

        self.proc_server = None
        self.proc_mount = None
        self.proc_cleanup = None
        self.proc_downloader = None

        self.server_ready = threading.Event()
        self.is_running = False
        self.is_downloader_running = False
        self.stream_only = False
        self.auto_restart_attempts = 0
        self.turbo_active = False
        self.turbo_closed_apps = []

        self.mongo_uri = os.environ.get("MONGODB", "mongodb://localhost:27017")
        self.stage_settings_file = os.path.abspath("stage_paths.json")
        self.monitor_settings_file = os.path.abspath("monitor_paths.json")
        self.m3u_config_file = os.path.abspath("m3u_strm_config.json")
        self.m3u_catalog_file = os.path.abspath("m3u_strm_catalog.json")
        self.m3u_priority_file = os.path.abspath("m3u_priority.json")

        # M3U sync state
        self._m3u_running = False
        self._m3u_timer_id = None
        self.m3u_catalog = []
        self.m3u_search_results = []
        self.m3u_priorities = {"paths": [], "terms": []}

        self.file_bars = {}
        self.current_download_info = {}

        self._create_widgets()
        self._create_tray()
        self._start_status_loop()
        self._start_stage_disk_monitor()
        self.root.after(5000, self._check_idle_turbo)

    def _create_tray(self):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill="#89b4fa")
        draw.text((20, 13), "N", fill="#11111b", stroke_width=1)
        self.tray_icon = pystray.Icon(
            "NebulaFTP",
            image,
            "NebulaFTP & STRM Downloader",
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
            "Fechar Nebula",
            "Deseja fechar completamente o Nebula e o Downloader?\n\n"
            "Sim: encerrar o programa e todos os serviços.\n"
            "Não: minimizar para a área de notificações.",
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
        self.stop_downloader()
        self.tray_icon.stop()
        self.root.destroy()

    def _create_widgets(self):
        # Header Geral Superior
        header_frame = tk.Frame(self.root, bg=self.card_bg, pady=8, padx=15)
        header_frame.pack(fill="x", padx=10, pady=(10, 5))

        title_lbl = tk.Label(
            header_frame,
            text="NebulaFTP Hub",
            font=("Segoe UI", 15, "bold"),
            bg=self.card_bg,
            fg=self.accent_color,
        )
        title_lbl.pack(side="left")

        self.global_status_lbl = tk.Label(
            header_frame,
            text="Envio: Parado | Download: Parado",
            font=("Segoe UI", 10, "bold"),
            bg=self.card_bg,
            fg=self.fg_color,
        )
        self.global_status_lbl.pack(side="right")

        # Container de Abas (Notebook)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Aba 1: Nebula (Envio de Mídias)
        self.tab_nebula = tk.Frame(self.notebook, bg=self.bg_color)
        self.notebook.add(self.tab_nebula, text="  🚀 Nebula (Envio)  ")

        # Aba 2: Download (STRM Downloader)
        self.tab_download = tk.Frame(self.notebook, bg=self.bg_color)
        self.notebook.add(self.tab_download, text="  📥 Download STRM  ")

        # Aba 3: IPTV M3U → STRM
        self.tab_iptv = tk.Frame(self.notebook, bg=self.bg_color)
        self.notebook.add(self.tab_iptv, text="  📡 IPTV M3U  ")

        # Montar conteúdo de cada aba
        self._build_nebula_tab()
        self._build_download_tab()
        self._build_iptv_tab()

    # =========================================================================
    # ABA 1: NEBULA (ENVIO DE MÍDIAS)
    # =========================================================================
    def _build_nebula_tab(self):
        # Controles do Nebula Envio
        ctrl_frame = tk.Frame(self.tab_nebula, bg=self.card_bg, pady=8, padx=12)
        ctrl_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.btn_start = tk.Button(
            ctrl_frame,
            text="▶ Iniciar Envio (Nebula)",
            command=self.start_services,
            font=("Segoe UI", 10, "bold"),
            bg=self.success_color,
            fg="#11111b",
            bd=0,
            padx=16,
            pady=6,
            cursor="hand2",
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stream = tk.Button(
            ctrl_frame,
            text="🌐 Somente Streaming",
            command=lambda: self.start_services(stream_only=True),
            font=("Segoe UI", 10, "bold"),
            bg=self.accent_color,
            fg="#11111b",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.btn_stream.pack(side="left", padx=(0, 8))

        self.btn_stop = tk.Button(
            ctrl_frame,
            text="⏹ Parar Envio",
            command=self.stop_services,
            font=("Segoe UI", 10, "bold"),
            bg=self.stop_color,
            fg="#11111b",
            bd=0,
            padx=16,
            pady=6,
            cursor="hand2",
            state="disabled",
        )
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_strm = tk.Button(
            ctrl_frame,
            text="📑 Gerar Biblioteca STRM",
            command=self.generate_strm,
            font=("Segoe UI", 10, "bold"),
            bg="#585b70",
            fg="white",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.btn_strm.pack(side="left")

        # Opções Turbo
        turbo_frame = tk.Frame(self.tab_nebula, bg=self.bg_color, pady=4)
        turbo_frame.pack(fill="x", padx=10)

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
            turbo_frame, text="Ativar após", bg=self.bg_color, fg=self.fg_color
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
            turbo_frame, text="minutos", bg=self.bg_color, fg=self.fg_color
        ).pack(side="left", padx=(4, 0))

        # Divisão: Uploads Ativos e Logs do Servidor
        nebula_paned = ttk.PanedWindow(self.tab_nebula, orient="horizontal")
        nebula_paned.pack(fill="both", expand=True, padx=10, pady=5)

        # Coluna Esquerda: Uploads Ativos
        progress_frame = tk.LabelFrame(
            nebula_paned,
            text=" 📤 Uploads Ativos / Fila de Envio ",
            font=("Segoe UI", 10, "bold"),
            bg=self.bg_color,
            fg=self.accent_color,
            pady=5,
            padx=10,
        )
        self.progress_frame = progress_frame
        nebula_paned.add(progress_frame, weight=1)

        self.canvas = tk.Canvas(progress_frame, bg="#181825", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(progress_frame, orient="vertical", command=self.canvas.yview)
        self.scroll_content = tk.Frame(self.canvas, bg="#181825")

        self.scroll_content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scroll_content, anchor="nw")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width),
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Coluna Direita: Logs do Sistema
        log_frame = tk.LabelFrame(
            nebula_paned,
            text=" 📜 Logs do Servidor & Upload ",
            font=("Segoe UI", 10, "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
            pady=5,
            padx=10,
        )
        nebula_paned.add(log_frame, weight=1)

        self.log_text = tk.Text(log_frame, font=("Consolas", 9), bg="#11111b", fg="#a6adc8", bd=0, wrap="word")
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")

    # =========================================================================
    # ABA 2: DOWNLOAD (STRM DOWNLOADER)
    # =========================================================================
    def _build_download_tab(self):
        # Painel Superior: Configuração de Pastas (Monitoramento STRM + Stage)
        config_frame = tk.Frame(self.tab_download, bg=self.card_bg, pady=8, padx=12)
        config_frame.pack(fill="x", padx=10, pady=(10, 5))

        # Seção 1: Pasta de Monitoramento (.strm)
        strm_lbl = tk.Label(
            config_frame,
            text="📁 Pasta Mapeada de Monitoramento (Arquivos .strm):",
            font=("Segoe UI", 10, "bold"),
            bg=self.card_bg,
            fg=self.accent_color,
        )
        strm_lbl.pack(anchor="w")

        strm_input_frame = tk.Frame(config_frame, bg=self.card_bg)
        strm_input_frame.pack(fill="x", pady=(3, 3))

        self.path_var = tk.StringVar(value=os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "D:/midias")))
        self.path_entry = tk.Entry(
            strm_input_frame,
            textvariable=self.path_var,
            font=("Consolas", 10),
            bg="#181825",
            fg=self.fg_color,
            insertbackground="white",
            bd=1,
            relief="solid",
        )
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 8))

        tk.Button(
            strm_input_frame,
            text="Procurar...",
            command=self._browse_folder,
            font=("Segoe UI", 9, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=12,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            strm_input_frame,
            text="Adicionar",
            command=self._add_current_folder,
            font=("Segoe UI", 9, "bold"),
            bg=self.accent_color,
            fg="#11111b",
            bd=0,
            padx=12,
            pady=3,
            cursor="hand2",
        ).pack(side="left")

        # Lista de Pastas STRM Ativas
        list_container = tk.Frame(config_frame, bg=self.card_bg)
        list_container.pack(fill="x", pady=(2, 6))

        list_body = tk.Frame(list_container, bg="#181825", bd=1, relief="solid")
        list_body.pack(side="left", fill="x", expand=True)

        self.paths_listbox = tk.Listbox(
            list_body,
            height=2,
            font=("Consolas", 9),
            bg="#181825",
            fg=self.fg_color,
            selectbackground=self.accent_color,
            selectforeground="#11111b",
            bd=0,
            highlightthickness=0,
            exportselection=False,
        )
        self.paths_listbox.pack(side="left", fill="x", expand=True)
        strm_scroll = ttk.Scrollbar(list_body, orient="vertical", command=self.paths_listbox.yview)
        self.paths_listbox.configure(yscrollcommand=strm_scroll.set)
        strm_scroll.pack(side="right", fill="y")

        tk.Button(
            list_container,
            text="Remover Pasta",
            command=self._remove_selected_folder,
            font=("Segoe UI", 8, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right", padx=(8, 0), anchor="center")

        # Seção 2: Pastas de Staging Mapeadas pelo Nebula
        stage_lbl = tk.Label(
            config_frame,
            text="💾 Pastas Stage Mapeadas pelo Nebula (Destino dos Downloads):",
            font=("Segoe UI", 10, "bold"),
            bg=self.card_bg,
            fg=self.success_color,
        )
        stage_lbl.pack(anchor="w", pady=(4, 0))

        stage_container = tk.Frame(config_frame, bg=self.card_bg)
        stage_container.pack(fill="x", pady=(3, 3))

        stage_body = tk.Frame(stage_container, bg="#181825", bd=1, relief="solid")
        stage_body.pack(side="left", fill="x", expand=True)

        self.stage_listbox = tk.Listbox(
            stage_body,
            height=2,
            font=("Consolas", 9),
            bg="#181825",
            fg=self.fg_color,
            selectbackground=self.accent_color,
            selectforeground="#11111b",
            bd=0,
            highlightthickness=0,
            exportselection=False,
        )
        self.stage_listbox.pack(side="left", fill="x", expand=True)
        stage_scroll = ttk.Scrollbar(stage_body, orient="vertical", command=self.stage_listbox.yview)
        self.stage_listbox.configure(yscrollcommand=stage_scroll.set)
        stage_scroll.pack(side="right", fill="y")

        stage_actions = tk.Frame(stage_container, bg=self.card_bg)
        stage_actions.pack(side="right", padx=(8, 0))

        tk.Button(
            stage_actions,
            text="Adicionar Stage",
            command=self._browse_stage,
            font=("Segoe UI", 8, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=10,
            pady=2,
            cursor="hand2",
        ).pack(fill="x")

        tk.Button(
            stage_actions,
            text="Remover Stage",
            command=self._remove_selected_stage,
            font=("Segoe UI", 8, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=10,
            pady=2,
            cursor="hand2",
        ).pack(fill="x", pady=(2, 0))

        # Indicador de Espaço Livre nos Discos de Stage
        self.stage_disk_lbl = tk.Label(
            config_frame,
            text="Monitorando capacidade dos discos de stage...",
            font=("Segoe UI", 8, "italic"),
            bg=self.card_bg,
            fg="#a6adc8",
            anchor="w",
        )
        self.stage_disk_lbl.pack(fill="x", pady=(2, 0))

        # Carregar caminhos iniciais
        self._load_initial_paths()
        self._load_initial_stages()

        # Barra de Ações e Configurações de Download
        action_frame = tk.Frame(self.tab_download, bg=self.bg_color, pady=6)
        action_frame.pack(fill="x", padx=10)

        self.btn_start_dl = tk.Button(
            action_frame,
            text="▶ Iniciar Downloader STRM",
            command=self.start_downloader,
            font=("Segoe UI", 10, "bold"),
            bg=self.success_color,
            fg="#11111b",
            bd=0,
            padx=16,
            pady=6,
            cursor="hand2",
        )
        self.btn_start_dl.pack(side="left", padx=(0, 8))

        self.btn_stop_dl = tk.Button(
            action_frame,
            text="⏹ Parar Downloader",
            command=self.stop_downloader,
            font=("Segoe UI", 10, "bold"),
            bg=self.stop_color,
            fg="#11111b",
            bd=0,
            padx=16,
            pady=6,
            cursor="hand2",
            state="disabled",
        )
        self.btn_stop_dl.pack(side="left", padx=(0, 8))

        self.btn_prune_dl = tk.Button(
            action_frame,
            text="🧹 Limpar STRMs Concluídos",
            command=self._prune_completed_strms,
            font=("Segoe UI", 9, "bold"),
            bg="#45475a",
            fg="white",
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.btn_prune_dl.pack(side="left", padx=(0, 10))

        # Controle de Partes Simultâneas por Mídia
        tk.Label(
            action_frame,
            text="Partes:",
            font=("Segoe UI", 9, "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
        ).pack(side="left", padx=(4, 2))
        self.dl_parts_var = tk.IntVar(value=int(os.environ.get("STRM_DOWNLOAD_PARTS", "20")))
        tk.Spinbox(
            action_frame,
            from_=1,
            to=32,
            width=3,
            textvariable=self.dl_parts_var,
            bg="#181825",
            fg=self.fg_color,
            buttonbackground="#45475a",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 10))

        # Badge Regra 1 Mídia por Vez
        rule_badge = tk.Label(
            action_frame,
            text="🔒 1 Mídia por Vez | Prioridade por lote: Animações > Filmes > Séries > Doramas > Novelas > Pornô",
            font=("Segoe UI", 9, "bold"),
            bg="#313244",
            fg=self.accent_color,
            padx=10,
            pady=4,
        )
        rule_badge.pack(side="right")

        # Divisão: Status do Download Atual e Logs do Downloader
        dl_paned = ttk.PanedWindow(self.tab_download, orient="horizontal")
        dl_paned.pack(fill="both", expand=True, padx=10, pady=5)

        # Coluna Esquerda: Download Atual em Andamento
        dl_status_frame = tk.LabelFrame(
            dl_paned,
            text=" 📥 Download em Andamento (1 Mídia por Vez) ",
            font=("Segoe UI", 10, "bold"),
            bg=self.bg_color,
            fg=self.accent_color,
            pady=5,
            padx=10,
        )
        dl_paned.add(dl_status_frame, weight=1)

        self.dl_card_frame = tk.Frame(dl_status_frame, bg=self.card_bg, pady=10, padx=10)
        self.dl_card_frame.pack(fill="x", expand=False, pady=5)

        self.dl_name_lbl = tk.Label(
            self.dl_card_frame,
            text="Nenhum download em andamento",
            font=("Segoe UI", 10, "bold"),
            bg=self.card_bg,
            fg=self.fg_color,
            anchor="w",
            wraplength=380,
            justify="left",
        )
        self.dl_name_lbl.pack(fill="x")

        self.dl_meta_lbl = tk.Label(
            self.dl_card_frame,
            text="Aguardando início do Downloader...",
            font=("Segoe UI", 9),
            bg=self.card_bg,
            fg="#a6adc8",
            anchor="w",
        )
        self.dl_meta_lbl.pack(fill="x", pady=(2, 6))

        self.dl_pbar = ttk.Progressbar(self.dl_card_frame, mode="determinate")
        self.dl_pbar.pack(fill="x", pady=4)

        self.dl_pct_lbl = tk.Label(
            self.dl_card_frame,
            text="0.0% (0 MB / 0 MB)",
            font=("Consolas", 9, "bold"),
            bg=self.card_bg,
            fg=self.accent_color,
            anchor="e",
        )
        self.dl_pct_lbl.pack(fill="x")

        # Coluna Direita: Logs do Downloader STRM
        dl_log_frame = tk.LabelFrame(
            dl_paned,
            text=" 📜 Logs do STRM Downloader ",
            font=("Segoe UI", 10, "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
            pady=5,
            padx=10,
        )
        dl_paned.add(dl_log_frame, weight=1)

        self.dl_log_text = tk.Text(dl_log_frame, font=("Consolas", 9), bg="#11111b", fg="#a6adc8", bd=0, wrap="word")
        dl_log_scrollbar = ttk.Scrollbar(dl_log_frame, orient="vertical", command=self.dl_log_text.yview)
        self.dl_log_text.configure(yscrollcommand=dl_log_scrollbar.set)

        self.dl_log_text.pack(side="left", fill="both", expand=True)
        dl_log_scrollbar.pack(side="right", fill="y")

    # =========================================================================
    # ABA 3: IPTV M3U → STRM
    # =========================================================================
    def _build_iptv_tab(self):
        """Constrói a aba de geração automática de STRMs a partir de M3U IPTV."""

        # --- Helpers de estilo reutilizados da GUI principal ---
        def card(parent, title):
            frm = tk.LabelFrame(
                parent,
                text=f" {title} ",
                font=("Segoe UI", 10, "bold"),
                bg=self.card_bg,
                fg=self.fg_color,
                pady=8,
                padx=12,
                relief="flat",
                bd=1,
            )
            frm.pack(fill="x", padx=10, pady=(8, 0))
            return frm

        def row(parent):
            f = tk.Frame(parent, bg=self.card_bg)
            f.pack(fill="x", pady=3)
            return f

        def lbl(parent, text, bold=False):
            font = ("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9)
            return tk.Label(parent, text=text, font=font, bg=self.card_bg, fg=self.fg_color)

        def entry_var(parent, var, width=60):
            return tk.Entry(
                parent,
                textvariable=var,
                font=("Consolas", 9),
                bg="#181825",
                fg=self.fg_color,
                insertbackground=self.fg_color,
                bd=0,
                highlightthickness=1,
                highlightcolor=self.accent_color,
                highlightbackground="#45475a",
                width=width,
            )

        def btn(parent, text, command, color=None, state="normal", width=None):
            kw = dict(
                text=text,
                command=command,
                font=("Segoe UI", 9, "bold"),
                bg=color or "#45475a",
                fg="#11111b" if color else "white",
                bd=0,
                padx=10,
                pady=4,
                cursor="hand2",
                relief="flat",
                state=state,
            )
            if width:
                kw["width"] = width
            return tk.Button(parent, **kw)

        # ---- Card 1: Configuração da URL M3U ----
        cfg = card(self.tab_iptv, "⚙ Configuração M3U")

        r1 = row(cfg)
        lbl(r1, "URL do arquivo M3U:", bold=True).pack(side="left", padx=(0, 8))
        self.m3u_url_var = tk.StringVar()
        url_entry = entry_var(r1, self.m3u_url_var, width=55)
        url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        btn(r1, "🔍 Testar URL", self._m3u_test_url).pack(side="left")

        r2 = row(cfg)
        lbl(r2, "Pasta de destino das mídias:", bold=True).pack(side="left", padx=(0, 8))
        self.m3u_dest_var = tk.StringVar(value=r"D:\midias")
        entry_var(r2, self.m3u_dest_var, width=35).pack(side="left", fill="x", expand=True, padx=(0, 8))
        btn(r2, "📂 Escolher", self._m3u_browse_dest).pack(side="left")

        r3 = row(cfg)
        lbl(r3, "Intervalo de atualização (minutos):", bold=True).pack(side="left", padx=(0, 8))
        self.m3u_interval_var = tk.IntVar(value=60)
        tk.Spinbox(
            r3,
            textvariable=self.m3u_interval_var,
            from_=10,
            to=1440,
            increment=10,
            width=6,
            font=("Consolas", 9),
            bg="#181825",
            fg=self.fg_color,
            buttonbackground="#45475a",
            relief="flat",
        ).pack(side="left", padx=(0, 20))

        self.m3u_recovery_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            r3,
            text="Corrigir automaticamente STRMs em recuperação",
            variable=self.m3u_recovery_var,
            font=("Segoe UI", 9),
            bg=self.card_bg,
            fg=self.fg_color,
            selectcolor="#313244",
            activebackground=self.card_bg,
            activeforeground=self.fg_color,
            bd=0,
        ).pack(side="left")

        # ---- Card 2: Controles ----
        ctrl = card(self.tab_iptv, "▶ Controles")
        ctrl_row = row(ctrl)

        self.btn_m3u_start = btn(
            ctrl_row, "▶ Iniciar Sincronização", self._m3u_start, color=self.success_color
        )
        self.btn_m3u_start.pack(side="left", padx=(0, 8))

        self.btn_m3u_stop = btn(
            ctrl_row, "⏹ Parar", self._m3u_stop, color=self.stop_color, state="disabled"
        )
        self.btn_m3u_stop.pack(side="left", padx=(0, 8))

        self.btn_m3u_now = btn(
            ctrl_row, "🔄 Executar Agora", self._m3u_run_now, color=self.accent_color
        )
        self.btn_m3u_now.pack(side="left", padx=(0, 8))

        btn(ctrl_row, "💾 Salvar Config", self._m3u_save_config).pack(side="left")

        # ---- Card 3: Status ----
        stat_card = card(self.tab_iptv, "📊 Status")

        self.m3u_status_lbl = tk.Label(
            stat_card,
            text="Aguardando...",
            font=("Segoe UI", 9, "bold"),
            bg=self.card_bg,
            fg=self.accent_color,
            anchor="w",
        )
        self.m3u_status_lbl.pack(fill="x", pady=(0, 2))

        self.m3u_last_run_lbl = tk.Label(
            stat_card,
            text="Última execução: —",
            font=("Segoe UI", 8, "italic"),
            bg=self.card_bg,
            fg="#a6adc8",
            anchor="w",
        )
        self.m3u_last_run_lbl.pack(fill="x")

        self.m3u_stats_lbl = tk.Label(
            stat_card,
            text="Criados: — | Ignorados: — | Recuperados: — | Erros: —",
            font=("Consolas", 8),
            bg=self.card_bg,
            fg="#a6adc8",
            anchor="w",
        )
        self.m3u_stats_lbl.pack(fill="x")

        # ---- Busca e prioridade de download/upload ----
        search_card = card(self.tab_iptv, "🔎 Buscar e priorizar mídia")
        search_row = row(search_card)
        lbl(search_row, "Digite título, série, episódio ou categoria:", bold=True).pack(side="left", padx=(0, 8))
        self.m3u_search_var = tk.StringVar()
        self.m3u_search_var.trace_add("write", lambda *_: self._m3u_search())
        entry_var(search_row, self.m3u_search_var, width=42).pack(side="left", fill="x", expand=True, padx=(0, 8))
        btn(search_row, "⭐ Priorizar selecionada", self._m3u_prioritize_selected, color=self.accent_color).pack(side="left", padx=(0, 6))
        btn(search_row, "Limpar", self._m3u_clear_priority).pack(side="left")

        results_frame = tk.Frame(search_card, bg=self.card_bg)
        results_frame.pack(fill="x", pady=(4, 0))
        self.m3u_search_list = tk.Listbox(
            results_frame,
            height=5,
            font=("Consolas", 8),
            bg="#181825",
            fg=self.fg_color,
            selectbackground=self.accent_color,
            selectforeground="#11111b",
            bd=0,
            highlightthickness=0,
            exportselection=False,
        )
        self.m3u_search_list.pack(side="left", fill="x", expand=True)
        search_scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.m3u_search_list.yview)
        self.m3u_search_list.configure(yscrollcommand=search_scroll.set)
        search_scroll.pack(side="right", fill="y")
        self.m3u_priority_lbl = tk.Label(
            search_card,
            text="Prioridade: nenhuma mídia selecionada",
            font=("Segoe UI", 8, "italic"),
            bg=self.card_bg,
            fg="#a6adc8",
            anchor="w",
        )
        self.m3u_priority_lbl.pack(fill="x", pady=(3, 0))

        # ---- Log ----
        log_frame = tk.LabelFrame(
            self.tab_iptv,
            text=" 📜 Log M3U → STRM ",
            font=("Segoe UI", 10, "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
            pady=5,
            padx=10,
        )
        log_frame.pack(fill="both", expand=True, padx=10, pady=(8, 10))

        self.m3u_log_text = tk.Text(
            log_frame,
            font=("Consolas", 9),
            bg="#11111b",
            fg="#a6adc8",
            bd=0,
            wrap="word",
        )
        m3u_log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.m3u_log_text.yview)
        self.m3u_log_text.configure(yscrollcommand=m3u_log_scroll.set)
        self.m3u_log_text.pack(side="left", fill="both", expand=True)
        m3u_log_scroll.pack(side="right", fill="y")

        # Carrega config salva
        self._m3u_load_config()
        self._m3u_load_catalog()
        self._m3u_load_priority()

    def _m3u_load_catalog(self):
        try:
            with open(self.m3u_catalog_file, encoding="utf-8") as handle:
                value = json.load(handle)
            self.m3u_catalog = value if isinstance(value, list) else []
        except (OSError, ValueError):
            self.m3u_catalog = []

    def _m3u_load_priority(self):
        try:
            with open(self.m3u_priority_file, encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                self.m3u_priorities = {
                    "paths": [str(item) for item in value.get("paths", [])],
                    "terms": [str(item) for item in value.get("terms", [])],
                }
        except (OSError, ValueError):
            pass
        self._m3u_update_priority_label()

    def _m3u_save_priority(self):
        with open(self.m3u_priority_file, "w", encoding="utf-8") as handle:
            json.dump(self.m3u_priorities, handle, ensure_ascii=False, indent=2)
        self._m3u_update_priority_label()

    def _m3u_update_priority_label(self):
        count = len(set(self.m3u_priorities.get("paths", [])))
        self.m3u_priority_lbl.config(
            text=(
                f"Prioridade ativa: {count} mídia(s) — o Downloader processará primeiro "
                "e o upload respeitará a mesma prioridade."
                if count else "Prioridade: nenhuma mídia selecionada"
            )
        )

    def _m3u_search(self):
        query = self.m3u_search_var.get().strip().casefold()
        self.m3u_search_list.delete(0, tk.END)
        self.m3u_search_results = []
        if not query:
            return

        items = list(self.m3u_catalog)
        dest = self.m3u_dest_var.get().strip() or r"D:\midias"
        if os.path.isdir(dest):
            known = {str(item.get("relative_path", "")).casefold() for item in items}
            for root, _, files in os.walk(dest):
                for filename in files:
                    if not filename.casefold().endswith(".strm"):
                        continue
                    full = os.path.join(root, filename)
                    relative = os.path.relpath(full, dest)
                    if relative.casefold() not in known:
                        items.append({
                            "name": os.path.splitext(filename)[0],
                            "category": relative.split(os.sep, 1)[0],
                            "media_type": "strm",
                            "relative_path": relative,
                        })

        for item in items:
            haystack = " ".join(
                str(item.get(key, ""))
                for key in ("name", "group", "category", "media_type", "relative_path")
            ).casefold()
            if query not in haystack:
                continue
            self.m3u_search_results.append(item)
            self.m3u_search_list.insert(
                tk.END,
                f"[{item.get('category', '—')}] {item.get('name', '—')} — {item.get('relative_path', '—')}",
            )
            if len(self.m3u_search_results) >= 100:
                break

    def _m3u_prioritize_selected(self):
        selected = list(self.m3u_search_list.curselection())
        if not selected:
            messagebox.showwarning("Nenhuma mídia", "Selecione uma mídia na busca.", parent=self.root)
            return
        paths = set(self.m3u_priorities.get("paths", []))
        terms = set(self.m3u_priorities.get("terms", []))
        for index in selected:
            item = self.m3u_search_results[index]
            relative = str(item.get("relative_path", ""))
            if relative:
                paths.add(relative)
            name = str(item.get("name", "")).strip()
            if name:
                terms.add(name.casefold())
        self.m3u_priorities = {"paths": sorted(paths), "terms": sorted(terms)}
        self._m3u_save_priority()
        self._m3u_log(f"Prioridade adicionada para {len(selected)} mídia(s).")

    def _m3u_clear_priority(self):
        self.m3u_priorities = {"paths": [], "terms": []}
        self._m3u_save_priority()
        self._m3u_log("Prioridades de mídia limpas.")

    def _m3u_log(self, message: str):
        self.m3u_log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.m3u_log_text.see("end")

    def _m3u_load_config(self):
        if not os.path.isfile(self.m3u_config_file):
            return
        try:
            cfg = json.loads(open(self.m3u_config_file, encoding="utf-8").read())
            if cfg.get("m3u_url"):
                self.m3u_url_var.set(cfg["m3u_url"])
            if cfg.get("dest_root"):
                self.m3u_dest_var.set(cfg["dest_root"])
            if cfg.get("interval_minutes"):
                self.m3u_interval_var.set(int(cfg["interval_minutes"]))
            if "fix_recovery" in cfg:
                self.m3u_recovery_var.set(bool(cfg["fix_recovery"]))
        except Exception:
            pass

    def _m3u_save_config(self):
        cfg = {
            "m3u_url": self.m3u_url_var.get().strip(),
            "dest_root": self.m3u_dest_var.get().strip(),
            "interval_minutes": int(self.m3u_interval_var.get()),
            "fix_recovery": bool(self.m3u_recovery_var.get()),
        }
        with open(self.m3u_config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self._m3u_log("Configuração salva.")

    def _m3u_browse_dest(self):
        folder = filedialog.askdirectory(initialdir=self.m3u_dest_var.get() or "D:\\")
        if folder:
            self.m3u_dest_var.set(folder)

    def _m3u_test_url(self):
        from tools.m3u_strm_generator import normalize_m3u_url, redact_url  # noqa: PLC0415

        url = normalize_m3u_url(self.m3u_url_var.get())
        if not url:
            messagebox.showwarning("URL vazia", "Digite a URL do M3U antes de testar.", parent=self.root)
            return
        self._m3u_log(f"Testando URL: {redact_url(url)}")

        def _test():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
                        "Accept": "application/vnd.apple.mpegurl,*/*",
                    },
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    first_bytes = resp.read(512).decode("utf-8", errors="replace")
                if "#EXTM3U" in first_bytes:
                    self.root.after(0, lambda: self._m3u_log("✅ URL válida — cabeçalho #EXTM3U detectado."))
                else:
                    self.root.after(0, lambda: self._m3u_log(f"⚠ URL respondeu, mas não parece M3U. Início: {first_bytes[:100]!r}"))
            except Exception as exc:
                self.root.after(0, lambda: self._m3u_log(f"❌ Erro ao testar URL: {exc}"))

        threading.Thread(target=_test, daemon=True).start()

    def _m3u_start(self):
        url = self.m3u_url_var.get().strip()
        if not url:
            messagebox.showwarning("URL vazia", "Configure a URL do M3U antes de iniciar.", parent=self.root)
            return
        if self._m3u_running:
            return
        self._m3u_running = True
        self.btn_m3u_start.config(state="disabled")
        self.btn_m3u_stop.config(state="normal")
        self.m3u_status_lbl.config(text="⏳ Sincronização ativa", fg=self.success_color)
        self._m3u_log("Sincronização automática iniciada.")
        self._m3u_save_config()
        self._m3u_schedule_next(delay_ms=0)

    def _m3u_stop(self):
        self._m3u_running = False
        if self._m3u_timer_id is not None:
            self.root.after_cancel(self._m3u_timer_id)
            self._m3u_timer_id = None
        self.btn_m3u_start.config(state="normal")
        self.btn_m3u_stop.config(state="disabled")
        self.m3u_status_lbl.config(text="⏹ Parado", fg=self.fg_color)
        self._m3u_log("Sincronização automática parada.")

    def _m3u_run_now(self):
        """Executa uma sincronização imediata (manual), sem afetar o agendamento automático."""
        self._m3u_log("Execução manual solicitada...")
        threading.Thread(target=self._m3u_do_sync, daemon=True).start()

    def _m3u_schedule_next(self, delay_ms: int = None):
        if not self._m3u_running:
            return
        if delay_ms is None:
            delay_ms = int(self.m3u_interval_var.get()) * 60 * 1000
        self._m3u_timer_id = self.root.after(delay_ms, self._m3u_trigger)

    def _m3u_trigger(self):
        if not self._m3u_running:
            return
        self.m3u_status_lbl.config(text="⏳ Sincronizando...", fg=self.accent_color)
        threading.Thread(target=self._m3u_do_sync, daemon=True).start()

    def _m3u_do_sync(self):
        from tools.m3u_strm_generator import normalize_m3u_url, run_m3u_sync  # noqa: PLC0415

        url = normalize_m3u_url(self.m3u_url_var.get())
        dest = self.m3u_dest_var.get().strip() or r"D:\midias"
        fix_rec = bool(self.m3u_recovery_var.get())

        def _cb(msg):
            self.root.after(0, lambda m=msg: self._m3u_log(m))

        try:
            stats = run_m3u_sync(
                m3u_url=url,
                dest_root=dest,
                mongo_uri=self.mongo_uri,
                fix_recovery=fix_rec,
                progress_callback=_cb,
                catalog_path=self.m3u_catalog_file,
            )
            ts = time.strftime("%d/%m/%Y %H:%M:%S")

            def _update(s=stats, t=ts):
                self._m3u_load_catalog()
                self._m3u_search()
                self.m3u_last_run_lbl.config(text=f"Última execução: {t}")
                self.m3u_stats_lbl.config(
                    text=(
                        f"Criados: {s['created']} | "
                        f"Ignorados: {s['skipped']} | "
                        f"Recuperados: {s['recovery_fixed']} | "
                        f"Erros: {s['errors']}"
                    )
                )
                if self._m3u_running:
                    self.m3u_status_lbl.config(text="✅ Aguardando próxima execução", fg=self.success_color)

            self.root.after(0, _update)

        except Exception as exc:
            def _err(e=exc):
                self._m3u_log(f"❌ Falha na sincronização: {e}")
                if self._m3u_running:
                    self.m3u_status_lbl.config(text="❌ Erro — tentando novamente no próximo ciclo", fg=self.stop_color)
            self.root.after(0, _err)

        # Agendar próxima execução automática (somente se ainda rodando)
        if self._m3u_running:
            interval_ms = int(self.m3u_interval_var.get()) * 60 * 1000
            self.root.after(0, lambda: self._m3u_schedule_next(interval_ms))

    # =========================================================================
    # GERENCIAMENTO DE CAMINHOS & PASTAS
    # =========================================================================
    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.path_var.get() or os.getcwd())
        if folder:
            self.path_var.set(folder)

    def _add_current_folder(self):
        folder = self.path_var.get().strip()
        if not folder:
            return
        folder = os.path.normpath(folder)
        if hasattr(self, "paths_listbox"):
            current = set(self.paths_listbox.get(0, tk.END))
            if folder not in current:
                self.paths_listbox.insert(tk.END, folder)
                self._save_monitor_paths()

    def _remove_selected_folder(self):
        if not hasattr(self, "paths_listbox"):
            return
        selection = list(self.paths_listbox.curselection())
        if not selection:
            return
        for index in reversed(selection):
            self.paths_listbox.delete(index)
        self._save_monitor_paths()
        if self.paths_listbox.size():
            self.path_var.set(self.paths_listbox.get(0))
        else:
            self.path_var.set("")

    def _load_initial_paths(self):
        paths = []
        if os.path.isfile(self.monitor_settings_file):
            try:
                saved = json.loads(open(self.monitor_settings_file, encoding="utf-8").read())
                if isinstance(saved, list):
                    paths = [str(p) for p in saved if str(p).strip()]
            except Exception:
                pass
        if not paths:
            raw_paths = os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "D:/midias"))
            for chunk in re.split(r"[;,\n]", raw_paths):
                p = chunk.strip().strip('"')
                if p:
                    paths.append(p)
        for p in paths:
            norm = os.path.normpath(p)
            if hasattr(self, "paths_listbox"):
                if norm not in set(self.paths_listbox.get(0, tk.END)):
                    self.paths_listbox.insert(tk.END, norm)
        if paths:
            self.path_var.set(paths[0])

    def _save_monitor_paths(self):
        paths = self._get_monitor_paths()
        with open(self.monitor_settings_file, "w", encoding="utf-8") as handle:
            json.dump(paths, handle, ensure_ascii=False, indent=2)

    def _get_monitor_paths(self):
        if hasattr(self, "paths_listbox") and self.paths_listbox.size() > 0:
            return [self.paths_listbox.get(i) for i in range(self.paths_listbox.size())]
        return [part.strip().strip('"') for part in re.split(r"[;,\n]", self.path_var.get()) if part.strip()]

    def _browse_stage(self):
        folder = filedialog.askdirectory()
        if folder:
            norm = os.path.normpath(folder)
            current = set(self.stage_listbox.get(0, tk.END))
            if norm not in current:
                self.stage_listbox.insert(tk.END, norm)
                self._save_stage_paths()
                self._update_stage_disk_label()

    def _remove_selected_stage(self):
        for index in reversed(self.stage_listbox.curselection()):
            self.stage_listbox.delete(index)
        self._save_stage_paths()
        self._update_stage_disk_label()

    def _load_initial_stages(self):
        default_stage = "E:\\NebulaStage" if os.path.isdir("E:\\") else ("I:\\NebulaStage" if os.path.isdir("I:\\") else os.path.abspath("staging"))
        raw_stages = os.environ.get("STAGING_DIRS", os.environ.get("STAGING_DIR", default_stage))
        saved_stages = None
        if os.path.isfile(self.stage_settings_file):
            try:
                saved_stages = json.loads(open(self.stage_settings_file, encoding="utf-8").read())
            except Exception:
                pass
        if isinstance(saved_stages, list) and saved_stages:
            stage_list = saved_stages
        else:
            stage_list = [s.strip() for s in raw_stages.split(";") if s.strip()]

        for stage in stage_list:
            norm = os.path.normpath(stage)
            if norm not in set(self.stage_listbox.get(0, tk.END)):
                self.stage_listbox.insert(tk.END, norm)

    def _save_stage_paths(self):
        stages = self._get_stage_paths()
        with open(self.stage_settings_file, "w", encoding="utf-8") as handle:
            json.dump(stages, handle, ensure_ascii=False, indent=2)

    def _get_stage_paths(self):
        return [self.stage_listbox.get(index) for index in range(self.stage_listbox.size())]

    def _start_stage_disk_monitor(self):
        def monitor():
            self._update_stage_disk_label()
            self.root.after(5000, monitor)
        self.root.after(1000, monitor)

    def _update_stage_disk_label(self):
        stages = self._get_stage_paths()
        if not stages:
            self.stage_disk_lbl.config(text="Nenhuma pasta de stage configurada.")
            return
        info_parts = []
        for stage in stages:
            try:
                usage = shutil.disk_usage(stage if os.path.exists(stage) else os.path.splitdrive(stage)[0] + "\\")
                free_gb = usage.free / (1024 ** 3)
                total_gb = usage.total / (1024 ** 3)
                pct_free = (usage.free / usage.total) * 100 if usage.total > 0 else 0
                info_parts.append(f"{stage}: {free_gb:.1f} GB livres ({pct_free:.0f}%)")
            except Exception:
                info_parts.append(f"{stage}: indisponível")
        self.stage_disk_lbl.config(text=" | ".join(info_parts))

    # =========================================================================
    # LOGGING
    # =========================================================================
    def log(self, message):
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")

    def log_dl(self, message):
        self.dl_log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.dl_log_text.see("end")

    # =========================================================================
    # MODO TURBO
    # =========================================================================
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
            and (self.is_running or self.is_downloader_running)
            and self._windows_idle_seconds() >= threshold
        )

        if active != self.turbo_active:
            self.turbo_active = active
            for proc in (self.proc_server, self.proc_mount, self.proc_cleanup, self.proc_downloader):
                self._set_process_priority(proc, active)
            if self.is_running or self.is_downloader_running:
                if active:
                    closed = self._close_apps_for_turbo()
                    detail = ", ".join(closed) if closed else "nenhum aplicativo aberto"
                    message = f"[TURBO] PC ocioso; prioridade aumentada. Fechados: {detail}."
                else:
                    restored = self._restore_apps_after_turbo()
                    detail = ", ".join(restored) if restored else "nenhum aplicativo"
                    message = f"[TURBO] Atividade detectada; prioridade normal. Reabertos: {detail}."
                self.log(message)
                self.log_dl(message)
        elif active:
            for proc in (self.proc_server, self.proc_mount, self.proc_cleanup, self.proc_downloader):
                self._set_process_priority(proc, True)

        self.root.after(5000, self._check_idle_turbo)

    # =========================================================================
    # CONTROLE DE SERVIÇOS NEBULA (ENVIO EXCLUSIVO)
    # =========================================================================
    def start_services(self, stream_only=False, automatic=False):
        if self.is_running:
            return
        if not automatic:
            self.auto_restart_attempts = 0
        ftp_port = int(os.environ.get("PORT", "2122"))
        stream_port = int(os.environ.get("STREAM_PORT", "2124"))
        for port in (ftp_port, stream_port):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    messagebox.showerror(
                        "NebulaFTP já está executando",
                        f"A porta {port} já está em uso. Abra a instância do Nebula "
                        "que está na área de notificações ou encerre-a antes de iniciar outra.",
                    )
                    return
            except OSError:
                pass

        stage_dirs = [os.path.abspath(path) for path in self._get_stage_paths()]
        if not stage_dirs:
            messagebox.showerror("Erro", "Adicione ao menos uma pasta de staging.")
            return
        try:
            for stage_dir in stage_dirs:
                os.makedirs(stage_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Erro", f"Não foi possível criar uma pasta de staging:\n{exc}")
            return

        self.is_running = True
        self.stream_only = stream_only
        self.server_ready.clear()
        self.btn_start.config(state="disabled")
        self.btn_stream.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._update_global_status()

        self.log("Iniciando servidor somente para streaming." if stream_only else "Iniciando NebulaFTP Server (Modo Envio de Mídias).")

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

        threading.Thread(target=self._read_stream, args=(self.proc_server, "SERVER"), daemon=True).start()
        threading.Thread(target=self._mount_drive_when_ready, daemon=True).start()

        # Inicia Bot de Limpeza de Mídias Concluídas (clean_already_sent.py)
        sources = self._get_monitor_paths()
        if sources:
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
                self.log("[CLEANUP] Bot de limpeza contínua ativado.")
            except Exception as exc:
                self.log(f"[CLEANUP] Erro ao iniciar bot de limpeza: {exc}")

    def _mount_drive_when_ready(self):
        try:
            from tools.rclone_installer import ensure_rclone
            rclone = ensure_rclone(
                progress_callback=lambda msg: self.root.after(0, self.log, f"[RCLONE] {msg}"),
                ensure_mount_prereqs=True,
            )
        except Exception as exc:
            self.root.after(0, self.log, f"[RCLONE] Erro ao preparar rclone/WinFsp: {exc}")
            rclone = find_rclone_exe()
            if not rclone:
                self.root.after(0, self.log, "rclone não encontrado; unidade N: não montada.")
                return
        ftp_port = int(os.environ.get("PORT", "2122"))
        for _ in range(180):
            if not self.is_running:
                return
            try:
                with socket.create_connection(("127.0.0.1", ftp_port), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            self.root.after(0, self.log, "FTP não abriu em 3 minutos; unidade N: não montada.")
            return
        if os.path.exists("N:\\"):
            self.root.after(0, self.log, "Unidade N: já está montada.")
            return
        config_path = os.path.join(APP_DIR, "rclone-nebula.conf")
        library_user = os.environ.get("NEBULA_LIBRARY_USER", "raphael").strip("/")
        cmd = [
            rclone, "mount", "nebula:/", "N:",
            "--config", config_path,
            "--exclude", f"/{library_user}/**",
            "--read-only",
            "--vfs-cache-mode", "full",
            "--vfs-cache-max-size", "5G",
            "--vfs-cache-max-age", "15m",
            "--vfs-cache-poll-interval", "30s",
            "--dir-cache-time", "10s",
            "--poll-interval", "0",
            "--links",
            "--no-checksum",
            "--network-mode",
            "--volname", "NebulaFTP",
            "--log-file", os.path.join(APP_DIR, "rclone-mount.log"),
            "--log-level", "INFO",
        ]
        self.proc_mount = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=APP_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for _ in range(30):
            if self.proc_mount.poll() is not None:
                self.root.after(
                    0,
                    self.log,
                    f"Falha ao montar N: (rclone encerrou com código {self.proc_mount.returncode}). "
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
            "Montagem de N: ainda está inicializando em segundo plano.",
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
        for proc in (self.proc_server, self.proc_mount, self.proc_cleanup):
            self._set_process_priority(proc, False)
        if self.turbo_closed_apps and not self.is_downloader_running:
            self._restore_apps_after_turbo()
        self.log("Encerrando serviços Nebula.")
        rclone = find_rclone_exe()
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
            self.proc_server = None
        if self.proc_cleanup:
            self.proc_cleanup.terminate()
            self.proc_cleanup = None

        self.server_ready.clear()
        self.btn_start.config(state="normal")
        self.btn_stream.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._update_global_status()
        self.log("Serviços Nebula encerrados.")

    # =========================================================================
    # CONTROLE DO STRM DOWNLOADER
    # =========================================================================
    def start_downloader(self):
        if self.is_downloader_running:
            return
        sources = self._get_monitor_paths()
        if not sources:
            messagebox.showerror("Erro", "Informe ou selecione ao menos uma pasta com arquivos .strm para monitorar.")
            return

        stage_dirs = self._get_stage_paths()
        if not stage_dirs:
            messagebox.showerror("Erro", "Adicione ao menos uma pasta de staging mapeada pelo Nebula.")
            return

        missing = [source for source in sources if not os.path.exists(source)]
        if missing:
            messagebox.showerror("Erro", "Pasta(s) de monitoramento não encontrada(s):\n" + "\n".join(missing))
            return

        self.is_downloader_running = True
        self.btn_start_dl.config(state="disabled")
        self.btn_stop_dl.config(state="normal")
        self.dl_name_lbl.config(text="Iniciando varredura da biblioteca STRM...")
        self.dl_meta_lbl.config(text="Aguardando o primeiro lote de candidatos.")
        self.dl_pbar["value"] = 0
        self.dl_pct_lbl.config(text="Varredura em andamento...")
        self._update_global_status()

        py_exe = get_python_exe()
        dl_env = os.environ.copy()
        dl_env["STAGING_DIRS"] = ";".join(stage_dirs)
        dl_env["PYTHONIOENCODING"] = "utf-8"
        dl_env["PYTHONUTF8"] = "1"
        dl_env["PYTHONUNBUFFERED"] = "1"

        parts_count = str(max(1, min(32, self.dl_parts_var.get() if hasattr(self, "dl_parts_var") else 20)))
        cmd = [py_exe, "-u", "tools/strm_downloader.py", "--watch", "--parts", parts_count, "--min-free-percent", "10", "--sources"] + sources

        self.log_dl("Iniciando STRM Downloader (1 mídia por vez)...")
        try:
            self.proc_downloader = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dl_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            threading.Thread(target=self._read_stream, args=(self.proc_downloader, "STRM_DL"), daemon=True).start()
        except Exception as exc:
            self.is_downloader_running = False
            self.btn_start_dl.config(state="normal")
            self.btn_stop_dl.config(state="disabled")
            self._update_global_status()
            messagebox.showerror("Erro", f"Falha ao iniciar STRM Downloader:\n{exc}")

    def stop_downloader(self):
        if not self.is_downloader_running:
            return
        self.is_downloader_running = False
        if self.proc_downloader and self.proc_downloader.poll() is None:
            self.proc_downloader.terminate()
            self.proc_downloader = None
        self.btn_start_dl.config(state="normal")
        self.btn_stop_dl.config(state="disabled")
        self._update_global_status()
        self.dl_name_lbl.config(text="Downloader parado.")
        self.dl_meta_lbl.config(text="")
        self.dl_pbar["value"] = 0
        self.dl_pct_lbl.config(text="0.0%")
        self.log_dl("STRM Downloader encerrado.")

    def _prune_completed_strms(self):
        sources = self._get_monitor_paths()
        if not sources:
            messagebox.showwarning("Aviso", "Nenhuma pasta de monitoramento selecionada.")
            return
        py_exe = get_python_exe()
        cmd = [py_exe, "tools/strm_downloader.py", "--prune-completed", "--sources"] + sources
        self.log_dl("Executando limpeza de .strm já concluídos no Telegram/Nebula...")

        def _run():
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                out = (res.stdout or "").strip()
                self.root.after(0, self.log_dl, f"[LIMPEZA] {out or 'Concluída.'}")
                self.root.after(0, messagebox.showinfo, "Limpeza Concluída", "Verificação e limpeza de .strm concluídos!")
            except Exception as exc:
                self.root.after(0, self.log_dl, f"[LIMPEZA] Erro: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _update_global_status(self):
        envio_status = "Somente Streaming" if self.stream_only else ("Executando" if self.is_running else "Parado")
        dl_status = "Executando (1 mídia por vez)" if self.is_downloader_running else "Parado"
        self.global_status_lbl.config(
            text=f"Envio: {envio_status} | Download: {dl_status}",
            fg=self.success_color if (self.is_running or self.is_downloader_running) else self.stop_color,
        )

    # =========================================================================
    # LEITURA DE STREAM DE LOGS DOS PROCESSOS
    # =========================================================================
    def _read_stream(self, proc, prefix):
        for line in iter(proc.stdout.readline, ""):
            if line:
                line_str = line.strip()
                if prefix == "STRM_DL":
                    self.root.after(0, self.log_dl, line_str)
                    self.root.after(0, self._parse_downloader_line, line_str)
                else:
                    self.root.after(0, self.log, f"[{prefix}] {line_str}")
                    if prefix == "SERVER" and "HTTP Stream local:" in line_str:
                        self.server_ready.set()
                        self.auto_restart_attempts = 0
        proc.stdout.close()
        if prefix == "SERVER":
            return_code = proc.poll()
            self.root.after(0, self._handle_server_exit, return_code)
        elif prefix == "STRM_DL":
            self.root.after(0, self._handle_downloader_exit)

    def _handle_downloader_exit(self):
        if self.is_downloader_running:
            self.is_downloader_running = False
            self.btn_start_dl.config(state="normal")
            self.btn_stop_dl.config(state="disabled")
            self._update_global_status()
            self.log_dl("STRM Downloader processo finalizado.")

    def _handle_server_exit(self, return_code):
        if not self.is_running:
            return

        self.is_running = False
        self.server_ready.clear()
        if self.proc_mount and self.proc_mount.poll() is None:
            self.proc_mount.terminate()
        self.proc_mount = None
        # O servidor pode falhar antes de aceitar conexões (por exemplo,
        # WinError 10048). Não deixe o processo auxiliar continuar vivo,
        # pois o reinício automático criaria feeders duplicados.
        if self.proc_cleanup and self.proc_cleanup.poll() is None:
            self.proc_cleanup.terminate()
        self.proc_cleanup = None
        self.btn_start.config(state="normal")
        self.btn_stream.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._update_global_status()
        self.log(f"[SERVER] Processo encerrou inesperadamente (código {return_code}).")

        if self.auto_restart_attempts >= 3:
            self.log("[SERVER] Reinício automático cancelado após 3 tentativas.")
            return
        self.auto_restart_attempts += 1
        attempt = self.auto_restart_attempts
        self.log(f"[SERVER] Reiniciando automaticamente em 10 segundos ({attempt}/3).")
        self.root.after(
            10000,
            lambda: self.start_services(self.stream_only, automatic=True),
        )

    def _parse_downloader_line(self, line):
        scan_progress = parse_downloader_scan_progress(line)
        if scan_progress:
            directories, files, candidates, batches = scan_progress
            self.dl_name_lbl.config(text="Varrendo biblioteca STRM...")
            self.dl_meta_lbl.config(
                text=f"Pastas: {directories:,} | Arquivos verificados: {files:,} | "
                f"Candidatos: {candidates:,} | Lotes processados: {batches:,}"
            )
            self.dl_pbar["value"] = 0
            self.dl_pct_lbl.config(text="Progresso da varredura")
            return

        batch_match = re.search(
            r"Lote (\d+) disponível \((\d+) mídias; (\d+) encontradas até agora\); iniciando processamento",
            line,
            re.IGNORECASE,
        )
        if batch_match:
            batch, size, candidates = (int(value) for value in batch_match.groups())
            self.dl_name_lbl.config(text=f"Validando primeiro lote ({batch})...")
            self.dl_meta_lbl.config(
                text=f"{size} itens neste lote | {candidates:,} encontrados até agora"
            )
            self.dl_pbar["value"] = 0
            self.dl_pct_lbl.config(text="Selecionando primeira mídia elegível...")
            return

        # 1. Detecta início de download ou movimentação de mídia pronta
        m_start = re.search(
            r"Iniciando download:\s*(.+?)(?:\s+\[.+?\])?\s*->\s*Stage:\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if m_start:
            name = m_start.group(1).strip()
            dest = m_start.group(2).strip()
            self.dl_name_lbl.config(text=f"Baixando: {name}")
            self.dl_meta_lbl.config(text=f"Destino em Stage: {dest}")
            self.dl_pbar["value"] = 0
            self.dl_pct_lbl.config(text="0.0% (Iniciando download...)")
            return

        m_ready = re.search(
            r"M[íi]dia pronta detectada:\s*(.+?)(?:\s+\[.+?\])?\s*->\s*Movendo para Stage:\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if m_ready:
            name = m_ready.group(1).strip()
            dest = m_ready.group(2).strip()
            self.dl_name_lbl.config(text=f"Mídia Pronta: {name}")
            self.dl_meta_lbl.config(text=f"Movendo para Stage: {dest}")
            self.dl_pbar["value"] = 50
            self.dl_pct_lbl.config(text="50.0% (Transferindo para Stage...)")
            return

        # 2. Detecta retomada de download (Resume)
        m_resume = re.search(
            r"Resumindo\s+(.+?):\s*([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB\s*\((\d+)%\)\s*j[aá]\s*no\s*disco",
            line,
            re.IGNORECASE,
        )
        if m_resume:
            name = m_resume.group(1).strip()
            done_mb = float(m_resume.group(2))
            total_mb = float(m_resume.group(3))
            pct = float(m_resume.group(4))
            self.dl_name_lbl.config(text=f"Resumindo: {name}")
            self.dl_meta_lbl.config(text=f"Retomando download ({done_mb:.1f} MB já no disco)")
            self.dl_pbar["value"] = pct
            self.dl_pct_lbl.config(text=f"{pct:.1f}% ({done_mb:.1f} MB / {total_mb:.1f} MB já no disco)")
            return

        # 3. Detecta progresso do download
        m_prog = re.search(
            r"Baixando\s+(.+?):\s*([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB\s*\((\d+)%\)(?:\s*-\s*Vel:\s*([\d.]+\s*MB/s))?",
            line,
            re.IGNORECASE,
        )
        if m_prog:
            name = m_prog.group(1).strip()
            done_mb = float(m_prog.group(2))
            total_mb = float(m_prog.group(3))
            pct = float(m_prog.group(4))
            vel_str = f" | Vel: {m_prog.group(5)}" if m_prog.group(5) else ""
            self.dl_name_lbl.config(text=f"Baixando: {name}")
            self.dl_pbar["value"] = pct
            self.dl_pct_lbl.config(text=f"{pct:.1f}% ({done_mb:.1f} MB / {total_mb:.1f} MB{vel_str})")
            return

        # 3. Detecta união de partes multipart
        m_merge = re.search(r"Unindo partes do arquivo:\s*(.+)", line, re.IGNORECASE)
        if m_merge:
            name = m_merge.group(1).strip()
            self.dl_name_lbl.config(text=f"Processando: {name}")
            self.dl_meta_lbl.config(text="Download concluído. Unindo partes do arquivo...")
            self.dl_pbar["value"] = 99
            self.dl_pct_lbl.config(text="99.0% (Montando arquivo final...)")
            return

        # 4. Detecta enfileiramento no MongoDB
        m_queue = re.search(
            r"Enfileirado no Nebula com sucesso:\s*(.+?)\s*->\s*(.+?)\s*\(tamanho=([\d.]+)\s*MB",
            line,
            re.IGNORECASE,
        )
        if m_queue:
            name = m_queue.group(1).strip()
            parent = m_queue.group(2).strip()
            size_mb = m_queue.group(3).strip()
            self.dl_name_lbl.config(text=f"Enfileirado: {name}")
            self.dl_meta_lbl.config(text=f"Registrado na fila: {parent} ({size_mb} MB)")
            self.dl_pbar["value"] = 100
            self.dl_pct_lbl.config(text="100.0% (Enfileirado para envio)")
            return

        # 5. Detecta conclusão da mídia e prontidão para a próxima
        m_done = re.search(
            r"(?:Conclus[aã]o|Conclu[íi]do)\s+(?:do\s+)?processamento de:\s*(.+?)(?:\.\s*Pronto|\.$|$)",
            line,
            re.IGNORECASE,
        )
        if m_done:
            name = m_done.group(1).strip()
            self.dl_name_lbl.config(text=f"Concluído: {name}")
            self.dl_meta_lbl.config(text="Mídia enfileirada no Nebula e .strm deletado. Pronto para próxima mídia.")
            self.dl_pbar["value"] = 100
            self.dl_pct_lbl.config(text="100.0% (Aguardando próxima mídia...)")
            return

        # 6. Detecta pausa de disco cheio / backpressure
        if "Espaço insuficiente no stage" in line or "Backpressure" in line:
            self.dl_meta_lbl.config(text="⏸ Pausado: aguardando liberação de espaço em disco pelo Nebula...")

    # =========================================================================
    # LOOP DE STATUS DOS UPLOADS DO NEBULA
    # =========================================================================
    def _start_status_loop(self):
        def update():
            if self.is_running:
                try:
                    client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=1000)
                    active_docs = list(client.ftp.files.find(
                        {"status": {"$in": ["uploading", "queued", "staging"]}},
                        {
                            "name": 1,
                            "status": 1,
                            "size": 1,
                            "uploaded_bytes": 1,
                            "parts": 1,
                            "worker_id": 1,
                            "bot_index": 1,
                            "mtime": 1,
                            "ctime": 1,
                            "recovery_required": 1,
                            "failed_reason": 1,
                        },
                    ))
                    client.close()

                    active_count = sum(doc.get("status") == "uploading" for doc in active_docs)
                    queued_count = len(active_docs) - active_count
                    recovery_count = sum(bool(doc.get("recovery_required")) for doc in active_docs)
                    self.progress_frame.config(
                        text=(
                            " 📤 Uploads Ativos / Fila de Envio"
                            f"  |  Ativos: {active_count}  |  Fila: {queued_count} arquivo(s)"
                            f"  |  Recuperação: {recovery_count}"
                        )
                    )

                    # Apenas uploads ativos e itens em recuperação viram cards visíveis na UI.
                    # A fila normal é totalizada numericamente no cabeçalho.
                    visible_docs = [
                        doc for doc in active_docs
                        if should_display_upload_card(doc)
                    ]
                    current_keys = {
                        str(doc.get("_id", doc.get("name", "Arquivo")))
                        for doc in visible_docs
                    }

                    # Remove barras antigas
                    for key in list(self.file_bars.keys()):
                        if key not in current_keys:
                            self.file_bars[key]["frame"].destroy()
                            del self.file_bars[key]

                    # Atualiza ou cria novas barras apenas para visíveis
                    for doc in visible_docs:
                        name = doc.get("name", "Arquivo")
                        status = doc.get("status", "queued")
                        key = str(doc.get("_id", name))
                        total = doc.get("size", 1) or 1
                        uploaded = doc.get("uploaded_bytes", 0)
                        raw_parts = doc.get("parts")
                        parts = raw_parts if isinstance(raw_parts, list) else []
                        bot_indexes = upload_bot_indexes(parts) if status == "uploading" else []
                        if bot_indexes:
                            shown_bots = ", ".join(f"#{idx}" for idx in bot_indexes[:4])
                            remaining_bots = len(bot_indexes) - 4
                            bot_text = f"Bots usados {shown_bots}" + (f" +{remaining_bots}" if remaining_bots else "")
                        else:
                            bot_text = "Bots rotativos" if status == "uploading" else ""
                        if uploaded > 0 and total > 0:
                            pct = (uploaded / total) * 100
                        elif parts:
                            done_parts = len(parts)
                            total_parts = max(1, (total + (16 * 1024 * 1024) - 1) // (16 * 1024 * 1024))
                            pct = (done_parts / total_parts) * 100
                        else:
                            pct = 0.0

                        display_name = doc.get("display_name", name)
                        status_text = upload_status_label(doc)
                        details = " | ".join(part for part in (status_text, bot_text, display_name) if part)
                        info_text = details

                        if key not in self.file_bars:
                            item_frame = tk.Frame(self.scroll_content, bg="#313244", pady=6, padx=10)
                            item_frame.pack(fill="x", expand=True, pady=4, padx=5)

                            lbl_name = tk.Label(item_frame, text=info_text, font=("Segoe UI", 9, "bold"), bg="#313244", fg="#cdd6f4", anchor="w")
                            lbl_name.pack(fill="x")

                            pframe = tk.Frame(item_frame, bg="#313244")
                            pframe.pack(fill="x", pady=2)

                            pbar = ttk.Progressbar(pframe, length=100, mode="determinate")
                            pbar.pack(side="left", fill="x", expand=True, padx=(0, 10))

                            lbl_pct = tk.Label(pframe, text=f"{pct:.1f}%", font=("Consolas", 9, "bold"), bg="#313244", fg=self.accent_color, width=11, anchor="e")
                            lbl_pct.pack(side="right")

                            self.file_bars[key] = {"frame": item_frame, "pbar": pbar, "pct_lbl": lbl_pct, "name_lbl": lbl_name}

                        self.file_bars[key]["name_lbl"].config(text=info_text)
                        self.file_bars[key]["pbar"]["value"] = pct
                        if uploaded or parts or status == "uploading":
                            progress_text = f"{pct:.1f}%"
                        else:
                            progress_text = "Aguardando"
                        self.file_bars[key]["pct_lbl"].config(text=progress_text)

                except Exception:
                    pass

            self.root.after(1000, update)

        self.root.after(1000, update)


if __name__ == "__main__":
    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "NebulaFTP já está aberto",
            "Use o ícone do NebulaFTP na área de notificações para abrir a instância existente.",
        )
        root.destroy()
        raise SystemExit(0)
    root = tk.Tk()
    app = NebulaGUI(root)
    root.mainloop()


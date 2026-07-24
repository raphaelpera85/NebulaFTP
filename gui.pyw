import os
import sys
import time
import subprocess
import threading
import queue
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

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
        self.is_running = False

        self.mongo_uri = os.environ.get("MONGODB", "mongodb://localhost:27017")

        self._create_widgets()
        self._start_status_loop()

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

        self.path_var = tk.StringVar(value=os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "E:\\")))
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
        # Botões de Controle
        ctrl_frame = tk.Frame(self.root, bg=self.bg_color, pady=5)
        ctrl_frame.pack(fill="x", padx=15)

        self.btn_start = tk.Button(ctrl_frame, text="Iniciar NebulaFTP", command=self.start_services, font=("Segoe UI", 11, "bold"), bg=self.success_color, fg="#11111b", bd=0, padx=20, pady=6, cursor="hand2")
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_stop = tk.Button(ctrl_frame, text="Parar", command=self.stop_services, font=("Segoe UI", 11, "bold"), bg=self.stop_color, fg="#11111b", bd=0, padx=20, pady=6, cursor="hand2", state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 10))

        self.btn_strm = tk.Button(ctrl_frame, text="Gerar Biblioteca STRM", command=self.generate_strm, font=("Segoe UI", 11, "bold"), bg=self.accent_color, fg="#11111b", bd=0, padx=16, pady=6, cursor="hand2")
        self.btn_strm.pack(side="left")

        # Container Principal Dividido Lado a Lado (PanedWindow)
        main_paned = ttk.PanedWindow(self.root, orient="horizontal")
        main_paned.pack(fill="both", expand=True, padx=15, pady=10)

        # Coluna Esquerda: Uploads Ativos em Tempo Real
        progress_frame = tk.LabelFrame(main_paned, text=" Uploads Ativos em Tempo Real ", font=("Segoe UI", 10, "bold"), bg=self.bg_color, fg=self.accent_color, pady=5, padx=10)
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

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.path_var.get())
        if folder:
            self.path_var.set(folder)
            if hasattr(self, "paths_listbox"):
                current = set(self.paths_listbox.get(0, tk.END))
                if folder not in current:
                    self.paths_listbox.insert(tk.END, folder)

    def _load_initial_paths(self):
        raw_paths = os.environ.get("MONITOR_PATHS", os.environ.get("MONITOR_PATH", "E:\\"))
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

    def start_services(self):
        if self.is_running:
            return

        sources = self._get_monitor_paths()
        if not sources:
            messagebox.showerror("Erro", "Informe ao menos uma pasta para monitorar.")
            return
        missing = [source for source in sources if not os.path.exists(source)]
        if missing:
            messagebox.showerror("Erro", "Caminho(s) não encontrado(s):\n" + "\n".join(missing))
            return

        self.is_running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status_lbl.config(text="Executando", fg=self.success_color)

        self.log("Iniciando NebulaFTP Server e Monitor nas pastas: " + " | ".join(sources))

        # Inicia Server main.py em subprocesso oculto
        self.proc_server = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )

        # Inicia Feed_ftp.py em subprocesso oculto
        feed_cmd = [sys.executable, "-u", "tools/feed_ftp.py"]
        for source in sources:
            feed_cmd.extend(["--source", source])
        feed_cmd.extend(["--direct-mongo", "--workers", "2", "--watch", "--max-active", "60", "--poll-seconds", "60", "--delete-source"])
        self.proc_feed = subprocess.Popen(
            feed_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )

        threading.Thread(target=self._read_stream, args=(self.proc_server, "SERVER"), daemon=True).start()
        threading.Thread(target=self._read_stream, args=(self.proc_feed, "FEED"), daemon=True).start()

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

        self.log("Encerrando serviços.")
        if self.proc_server:
            self.proc_server.terminate()
        if self.proc_feed:
            self.proc_feed.terminate()

        self.is_running = False
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_lbl.config(text="Parado", fg=self.stop_color)
        self.log("Serviços encerrados.")

    def _read_stream(self, proc, prefix):
        for line in iter(proc.stdout.readline, ""):
            if line:
                line_str = line.strip()
                self.root.after(0, self.log, f"[{prefix}] {line_str}")
        proc.stdout.close()

    def _start_status_loop(self):
        def update():
            if self.is_running:
                try:
                    client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=1000)
                    active_docs = list(client.ftp.files.find({"status": {"$in": ["uploading", "queued"]}}, {"name": 1, "status": 1, "size": 1, "uploaded_bytes": 1, "parts": 1, "worker_id": 1, "bot_index": 1}))
                    client.close()

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
                        bot_idx = doc.get("bot_index", "?")
                        
                        uploaded = doc.get("uploaded_bytes", 0)
                        parts = doc.get("parts", [])
                        if uploaded > 0 and total > 0:
                            pct = (uploaded / total) * 100
                        elif parts:
                            done_parts = len(parts)
                            total_parts = max(1, (total + (64 * 1024 * 1024) - 1) // (64 * 1024 * 1024))
                            pct = (done_parts / total_parts) * 100
                        else:
                            pct = 0.0

                        info_text = f"Uploading | Worker #{worker_id} | Bot #{bot_idx} | {name[:50]}" if status == "uploading" else f"Na fila | {name[:50]}"

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
    root = tk.Tk()
    app = NebulaGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.stop_services(), root.destroy()))
    root.mainloop()

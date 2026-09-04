"""Rclone and WinFsp automatic discovery and installer for Nebula.

Garante que o executável do rclone e os drivers de montagem (WinFsp no Windows)
estejam presentes e operacionais para montagem de discos virtuais (N:, Z:, etc.).
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

RCLONE_ZIP_URL = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
WINFSP_MSI_URL = "https://github.com/winfsp/winfsp/releases/download/v2.0/winfsp-2.0.23075.msi"


def _log(msg: str, callback: Callable[[str], None] | None = None) -> None:
    if callback:
        try:
            callback(msg)
            return
        except Exception:
            pass
    print(f"[RCLONE] {msg}", flush=True)


def is_winfsp_installed() -> bool:
    """Verifica se o WinFsp (driver de sistema de arquivos virtual no Windows) está instalado."""
    if os.name != "nt":
        return True

    # 1. Checa DLLs padrões no sistema de 64-bit / 32-bit
    standard_paths = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "WinFsp", "bin", "winfsp-x64.dll"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WinFsp", "bin", "winfsp-x64.dll"),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "winfsp.sys"),
    ]
    for path in standard_paths:
        if os.path.isfile(path):
            return True

    # 2. Checa chaves no Registro do Windows
    try:
        import winreg
        for key_path in [r"SOFTWARE\WinFsp", r"SOFTWARE\WOW6432Node\WinFsp"]:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                    install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
                    if install_dir and os.path.exists(install_dir):
                        return True
            except OSError:
                continue
    except Exception:
        pass

    return False


def ensure_winfsp(progress_callback: Callable[[str], None] | None = None) -> bool:
    """Instala o WinFsp silenciosamente se ainda não estiver presente no Windows."""
    if os.name != "nt" or is_winfsp_installed():
        return True

    _log("WinFsp não detectado no Windows. Iniciando instalação do WinFsp...", progress_callback)

    # Tentativa 1: winget
    winget = shutil.which("winget")
    if winget:
        _log("Instalando WinFsp via winget...", progress_callback)
        try:
            proc = subprocess.run(
                [winget, "install", "--id", "WinFsp.WinFsp", "--silent", "--accept-package-agreements", "--accept-source-agreements"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode == 0 and is_winfsp_installed():
                _log("WinFsp instalado com sucesso via winget!", progress_callback)
                return True
        except Exception as exc:
            _log(f"Falha na tentativa winget para WinFsp: {exc}", progress_callback)

    # Tentativa 2: Download direto do instalador oficial MSI
    _log("Baixando instalador MSI do WinFsp...", progress_callback)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            msi_path = os.path.join(tmp_dir, "winfsp_installer.msi")
            req = urllib.request.Request(WINFSP_MSI_URL, headers={"User-Agent": "NebulaFTP-Installer/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(msi_path, "wb") as out_file:
                shutil.copyfileobj(resp, out_file)

            _log("Executando instalação silenciosa do WinFsp via msiexec...", progress_callback)
            proc = subprocess.run(
                ["msiexec.exe", "/i", msi_path, "/quiet", "/qn", "/norestart"],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if proc.returncode in (0, 3010) or is_winfsp_installed():
                _log("WinFsp instalado com sucesso via MSI!", progress_callback)
                return True
            _log(f"msiexec retornou código {proc.returncode}. Output: {proc.stdout} {proc.stderr}", progress_callback)
    except Exception as exc:
        _log(f"Erro ao baixar/instalar WinFsp MSI: {exc}", progress_callback)

    return is_winfsp_installed()


def _is_working_rclone(exe_path: str) -> bool:
    """Testa se o binário fornecido é executável e responde a `rclone --version`."""
    if not exe_path or not os.path.isfile(exe_path):
        return False
    try:
        proc = subprocess.run(
            [exe_path, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        return proc.returncode == 0 and "rclone" in proc.stdout.lower()
    except Exception:
        return False


def find_rclone() -> str | None:
    """Localiza o executável do rclone no ambiente."""
    # 1. Checa PATH do sistema
    exe = shutil.which("rclone") or shutil.which("rclone.exe")
    if exe and _is_working_rclone(exe):
        return os.path.abspath(exe)

    # 2. Checa pastas locais do projeto
    module_dir = Path(__file__).resolve().parent
    project_root = module_dir.parent
    workspace_root = project_root.parent

    local_candidates = [
        module_dir / "rclone.exe",
        module_dir / "rclone",
        project_root / "tools" / "rclone.exe",
        workspace_root / "tools" / "rclone.exe",
        project_root / "bin" / "rclone.exe",
    ]
    for cand in local_candidates:
        if cand.is_file() and _is_working_rclone(str(cand)):
            return str(cand.resolve())

    # 3. Checa pacotes do WinGet no AppData do usuário
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        winget_pkg = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkg):
            matches = glob.glob(os.path.join(winget_pkg, "**", "rclone.exe"), recursive=True)
            for match in matches:
                if _is_working_rclone(match):
                    return os.path.abspath(match)

        # Checa %LOCALAPPDATA%\rclone\rclone.exe
        appdata_rclone = os.path.join(local_appdata, "rclone", "rclone.exe")
        if os.path.isfile(appdata_rclone) and _is_working_rclone(appdata_rclone):
            return os.path.abspath(appdata_rclone)

    # 4. Checa locais comuns de instalação no sistema
    system_candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "rclone", "rclone.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "rclone", "rclone.exe"),
        os.path.expanduser(r"~\rclone\rclone.exe"),
        os.path.expanduser(r"~\AppData\Local\rclone\rclone.exe"),
    ]
    for cand in system_candidates:
        if os.path.isfile(cand) and _is_working_rclone(cand):
            return os.path.abspath(cand)

    return None


def download_and_extract_rclone(
    target_dir: str | Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> str | None:
    """Baixa o zip oficial portátil do rclone e extrai rclone.exe para a pasta tools."""
    if target_dir is None:
        target_dir = Path(__file__).resolve().parent

    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_exe = target_dir / "rclone.exe"

    _log(f"Baixando rclone oficial de {RCLONE_ZIP_URL}...", progress_callback)

    try:
        req = urllib.request.Request(
            RCLONE_ZIP_URL,
            headers={"User-Agent": "NebulaFTP-RcloneDownloader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=90) as response:
            zip_bytes = response.read()

        _log("Download concluído. Extraindo executável rclone.exe...", progress_callback)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            rclone_member = None
            for name in zf.namelist():
                if name.lower().endswith("rclone.exe") or name.lower().endswith("/rclone.exe"):
                    rclone_member = name
                    break
            if not rclone_member:
                _log("Erro: rclone.exe não encontrado dentro do pacote zip oficial.", progress_callback)
                return None

            with zf.open(rclone_member) as source, open(target_exe, "wb") as target:
                shutil.copyfileobj(source, target)

        # Ajusta permissões se necessário
        try:
            os.chmod(target_exe, 0o755)
        except Exception:
            pass

        if _is_working_rclone(str(target_exe)):
            _log(f"rclone configurado com sucesso em: {target_exe}", progress_callback)
            return str(target_exe)

        _log(f"Executável extraído em {target_exe} não respondeu corretamente.", progress_callback)
        return None

    except Exception as exc:
        _log(f"Falha ao baixar/extrair rclone: {exc}", progress_callback)
        return None


def ensure_rclone(
    progress_callback: Callable[[str], None] | None = None,
    ensure_mount_prereqs: bool = True,
) -> str:
    """Garante que o rclone e os pré-requisitos de montagem estejam prontos.

    Retorna o caminho absoluto do executável rclone.exe.
    Levanta RuntimeError caso a instalação automática falhe completamente.
    """
    # 1. Verifica se já existe rclone funcional
    rclone_path = find_rclone()

    # 2. Se não existir, tenta baixar e extrair diretamente para a pasta tools
    if not rclone_path:
        _log("rclone não foi encontrado no sistema. Instalando automaticamente...", progress_callback)
        rclone_path = download_and_extract_rclone(progress_callback=progress_callback)

    # 3. Se o download direto falhar, tenta via winget
    if not rclone_path:
        winget = shutil.which("winget")
        if winget:
            _log("Tentando instalar rclone via winget...", progress_callback)
            try:
                subprocess.run(
                    [winget, "install", "--id", "Rclone.Rclone", "--silent", "--accept-package-agreements", "--accept-source-agreements"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                rclone_path = find_rclone()
            except Exception as exc:
                _log(f"Falha no winget para rclone: {exc}", progress_callback)

    # 4. Verificação final do rclone
    if not rclone_path or not _is_working_rclone(rclone_path):
        raise RuntimeError(
            "Não foi possível localizar nem instalar o rclone automaticamente. "
            "Por favor, faça o download do rclone em https://rclone.org/downloads/ "
            "e coloque o arquivo rclone.exe na pasta 'tools' do projeto."
        )

    # 5. Verifica/instala WinFsp se solicitado (necessário para rclone mount no Windows)
    if ensure_mount_prereqs and os.name == "nt":
        if not is_winfsp_installed():
            ensure_winfsp(progress_callback=progress_callback)
            if not is_winfsp_installed():
                _log(
                    "AVISO: O driver WinFsp não pôde ser instalado automaticamente. "
                    "A montagem do disco virtual N: pode falhar até que o WinFsp seja instalado "
                    "(disponível em https://winfsp.dev/release/).",
                    progress_callback,
                )

    return rclone_path


def main() -> int:
    """CLI para verificação/instalação manual."""
    print("=== Nebula Rclone & WinFsp Auto-Installer ===")
    try:
        rclone = ensure_rclone(progress_callback=print, ensure_mount_prereqs=True)
        winfsp_status = "OK (Instalado)" if is_winfsp_installed() else "Ausente/Aviso"
        print(f"\n[SUCESSO] Rclone pronto: {rclone}")
        print(f"[SUCESSO] WinFsp status: {winfsp_status}")
        return 0
    except Exception as exc:
        print(f"\n[ERRO] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

import argparse
import contextlib
import os
import shutil
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo

load_dotenv()


def force_remove_tree(path: str) -> bool:
    """Attempt to forcibly remove a directory tree, handling read-only files gracefully."""
    def _onerror(func, p, _):
        with contextlib.suppress(Exception):
            os.chmod(p, 0o777)
            func(p)

    try:
        shutil.rmtree(path, onerror=_onerror)
        return not os.path.exists(path)
    except Exception:
        return False


def normalize_string(s: str) -> str:
    """Normalize string for safe comparison (ignore accents, symbols, case)."""
    if not s:
        return ""
    normalized = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("utf-8")
    return "".join(c for c in normalized.lower() if c.isalnum())


def get_completed_telegram_items(db) -> set[str]:
    """Fetch normalized names of all files and folders completed in Telegram."""
    completed = set()
    for doc in db.files.find({"type": "file", "status": "completed"}):
        parent = doc.get("parent", "")
        name = doc.get("name", "")

        # Add parent folder name if present
        if parent:
            folder_name = parent.rstrip("/").split("/")[-1]
            if folder_name and folder_name not in ("Filmes", "Series"):
                completed.add(normalize_string(folder_name))

        # Add file stem
        if name:
            stem = os.path.splitext(name)[0]
            if stem:
                completed.add(normalize_string(stem))

    return completed


MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".sub", ".ass", ".ssa", ".vtt", ".strm",
}


def safe_print(msg: str, file=sys.stdout):
    try:
        print(msg, file=file)
    except UnicodeEncodeError:
        encoding = getattr(file, "encoding", None) or "utf-8"
        encoded = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(encoded, file=file)


def clean_sources(sources: list[str], completed_items: set[str], dry_run: bool = False) -> int:
    """Scan sources and remove files/folders already present in Telegram or left without media files."""
    removed_count = 0

    for source in sources:
        # SAFETY CHECK: Never clean virtual drive N: or network drives mapped to Nebula FTP
        norm_src = os.path.normpath(source).upper()
        if norm_src.startswith("N:") or norm_src.startswith("N:\\"):
            safe_print(f"[SEGURANÇA] Ignorando drive virtual N: ({source}) para proteger o banco de dados.")
            continue

        if not os.path.exists(source):
            continue

        abs_source = os.path.abspath(source)

        # Walk bottom-up so child folders are processed before parents
        for root, dirs, files in os.walk(source, topdown=False):
            abs_root = os.path.abspath(root)
            if abs_root == abs_source:
                continue

            folder_name = os.path.basename(root)

            # Do not rmtree top category containers like Filmes/Series if directly under source
            is_top_category = folder_name.lower() in ("filmes", "series") and os.path.dirname(abs_root) == abs_source

            if is_top_category:
                if not dry_run and os.path.exists(root) and len(os.listdir(root)) == 0:
                    try:
                        os.rmdir(root)
                        safe_print(f"[REMOVER VAZIA] Categoria ficou vazia: {root}")
                    except Exception:
                        pass
                continue

            # Check individual files inside the folder
            for f in files:
                stem = os.path.splitext(f)[0]
                norm_stem = normalize_string(stem)

                if norm_stem and norm_stem in completed_items:
                    file_path = os.path.join(root, f)
                    safe_print(f"[REMOVER ARQUIVO] {'(DRY-RUN) ' if dry_run else ''}Já existe no Telegram: {file_path}")
                    if not dry_run:
                        try:
                            os.remove(file_path)
                            removed_count += 1
                        except Exception as err:
                            safe_print(f"[ERRO] Falha ao apagar arquivo {file_path}: {err}", file=sys.stderr)
                    else:
                        removed_count += 1

            # Check if any media files remain inside this folder or its subdirectories
            if os.path.exists(root):
                has_media = any(
                    os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS
                    for r, _, fs in os.walk(root)
                    for f in fs
                )

                if not has_media:
                    safe_print(f"[REMOVER PASTA SEM MIDIA] {'(DRY-RUN) ' if dry_run else ''}Sem arquivos de mídia restantes: {root}")
                    if not dry_run:
                        if force_remove_tree(root):
                            removed_count += 1
                        else:
                            safe_print(f"[AVISO] Não foi possível remover completamente: {root}", file=sys.stderr)
                    else:
                        removed_count += 1

    return removed_count


def run_cycle(mongo_uri: str, db_name: str, sources: list[str], dry_run: bool = False):
    """Run a single cleanup cycle."""
    try:
        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        completed_items = get_completed_telegram_items(db)

        if not completed_items:
            print("[INFO] Nenhum item concluído encontrado no MongoDB.")
            return

        removed = clean_sources(sources, completed_items, dry_run=dry_run)
        if removed > 0:
            print(f"[COMPLETO] Total de {removed} itens limpos.")
    except Exception as e:
        print(f"[ERRO CRÍTICO] Falha durante ciclo de limpeza: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Bot de limpeza contínua de mídias/strm já enviadas ao Telegram.")
    parser.add_argument("--sources", nargs="+", default=["D:/midias"], help="Diretórios locais a monitorar (apenas discos locais).")
    parser.add_argument("--interval", type=int, default=30, help="Intervalo em segundos entre verificações no modo continuo (padrão: 30s).")
    parser.add_argument("--once", action="store_true", help="Executar apenas uma vez e encerrar.")
    parser.add_argument("--dry-run", action="store_true", help="Apenas simular as remoções sem apagar nada.")

    args = parser.parse_args()
    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DATABASE", "ftp")

    print(f"=== Bot de Limpeza de Mídias Concluídas ===")
    print(f"MongoDB URI: {mongo_uri}")
    print(f"Database: {db_name}")
    print(f"Fontes monitoradas: {args.sources}")
    print(f"Modo: {'Execução Única' if args.once else f'Contínuo (a cada {args.interval}s)'}")
    print(f"Dry-run: {args.dry_run}")
    print("===========================================\n")

    if args.once:
        run_cycle(mongo_uri, db_name, args.sources, dry_run=args.dry_run)
    else:
        while True:
            run_cycle(mongo_uri, db_name, args.sources, dry_run=args.dry_run)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()

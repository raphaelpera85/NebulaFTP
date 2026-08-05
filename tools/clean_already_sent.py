import argparse
import os
import shutil
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo

load_dotenv()


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


def clean_sources(sources: list[str], completed_items: set[str], dry_run: bool = False) -> int:
    """Scan sources and remove files/folders already present in Telegram."""
    removed_count = 0

    for source in sources:
        # SAFETY CHECK: Never clean virtual drive N: or network drives mapped to Nebula FTP
        norm_src = os.path.normpath(source).upper()
        if norm_src.startswith("N:") or norm_src.startswith("N:\\"):
            print(f"[SEGURANÇA] Ignorando drive virtual N: ({source}) para proteger o banco de dados.")
            continue

        if not os.path.exists(source):
            continue

        # Walk bottom-up so child folders are processed before parents
        for root, dirs, files in os.walk(source, topdown=False):
            if root == source:
                continue

            folder_name = os.path.basename(root)
            norm_folder = normalize_string(folder_name)

            # Check if this folder itself corresponds to a completed Telegram item
            if norm_folder and norm_folder in completed_items:
                print(f"[REMOVER PASTA] {'(DRY-RUN) ' if dry_run else ''}Já existe no Telegram: {root}")
                if not dry_run:
                    try:
                        shutil.rmtree(root)
                        removed_count += 1
                    except Exception as err:
                        print(f"[ERRO] Falha ao apagar pasta {root}: {err}", file=sys.stderr)
                else:
                    removed_count += 1
                continue

            # Check individual files inside the folder
            for f in files:
                stem = os.path.splitext(f)[0]
                norm_stem = normalize_string(stem)

                if norm_stem and norm_stem in completed_items:
                    file_path = os.path.join(root, f)
                    print(f"[REMOVER ARQUIVO] {'(DRY-RUN) ' if dry_run else ''}Já existe no Telegram: {file_path}")
                    if not dry_run:
                        try:
                            os.remove(file_path)
                            removed_count += 1
                        except Exception as err:
                            print(f"[ERRO] Falha ao apagar arquivo {file_path}: {err}", file=sys.stderr)
                    else:
                        removed_count += 1

            # Remove empty directory if all files inside were deleted
            if not dry_run and os.path.exists(root):
                try:
                    if len(os.listdir(root)) == 0:
                        os.rmdir(root)
                        print(f"[REMOVER VAZIA] Pasta ficou vazia após limpeza: {root}")
                except Exception:
                    pass

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

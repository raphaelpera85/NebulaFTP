import os
import sys
from pathlib import Path, PurePosixPath
from pymongo import MongoClient
from dotenv import load_dotenv

if os.path.exists(".env"):
    load_dotenv()

mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
host = os.getenv("STREAM_HOST", "127.0.0.1")
port = os.getenv("STREAM_PORT", "2122")

# Raiz do projeto onde será criada a estrutura de strm
root_dir = Path(__file__).resolve().parent / "strm_library"

def generate_strm_files():
    client = MongoClient(mongo_uri)
    db = client.ftp

    completed_files = list(db.files.find({'type': 'file', 'status': 'completed'}))
    print(f"Encontrados {len(completed_files)} arquivo(s) concluído(s) no MongoDB.")

    created_count = 0

    for doc in completed_files:
        file_id = str(doc["_id"])
        name = doc.get("name", "")
        parent = doc.get("parent", "")

        # Monta link HTTP Stream
        stream_url = f"http://{host}:{port}/stream?id={file_id}"

        # Normaliza caminho removendo o prefixo do usuário (/raphael)
        p_path = PurePosixPath(parent)
        parts = list(p_path.parts)
        if parts and parts[0] == "/":
            parts.pop(0)
        if parts and parts[0] == "raphael":
            parts.pop(0)

        # Caminho relativo das pastas
        rel_folder = Path(*parts) if parts else Path()
        target_dir = root_dir / rel_folder

        # Garante que a estrutura de pastas existe
        target_dir.mkdir(parents=True, exist_ok=True)

        # Troca extensão original da mídia para .strm
        stem_name = Path(name).stem
        strm_filename = f"{stem_name}.strm"
        strm_file_path = target_dir / strm_filename

        # Escreve o arquivo .strm com a URL de streaming
        strm_file_path.write_text(stream_url, encoding="utf-8")
        created_count += 1

    client.close()

    print("=== GERAÇÃO STRM CONCLUÍDA ===")
    print(f"Estrutura criada em: {root_dir}")
    print(f"Total de arquivos .strm gerados: {created_count}")

if __name__ == "__main__":
    generate_strm_files()

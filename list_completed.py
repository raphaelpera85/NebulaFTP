import os
import sys
import subprocess
from pathlib import PurePosixPath
from pymongo import MongoClient
from dotenv import load_dotenv

# Garante a instalação do openpyxl
try:
    import openpyxl
except ImportError:
    print("openpyxl não instalado. Instalando...")
    subprocess.run([sys.executable, "-m", "pip", "install", "openpyxl"], check=True)
    import openpyxl

if os.path.exists(".env"):
    load_dotenv()

mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
client = MongoClient(mongo_uri)
db = client[os.getenv("MONGO_DATABASE", "ftp")]

completed_files = list(db.files.find({'type': 'file', 'status': 'completed'}))
library_user = os.getenv("NEBULA_LIBRARY_USER", "raphael")

series_data = []
filmes_data = []

for doc in completed_files:
    parent = doc.get("parent", "")
    name = doc.get("name", "")
    
    # Normaliza e remove o prefixo do usuario (ex: /raphael)
    p_path = PurePosixPath(parent)
    parts = list(p_path.parts)
    
    # Remove o primeiro elemento se for '/' e o segundo se for o nome do usuário (ex: raphael)
    if parts and parts[0] == "/":
        parts.pop(0)
    if parts and parts[0] == library_user:
        parts.pop(0)
        
    # Identifica se é Série ou Filme
    if parts and parts[0].lower() == "series":
        pasta = parts[0]
        subpasta = parts[1] if len(parts) > 1 else ""
        temporada = parts[2] if len(parts) > 2 else ""
        series_data.append((pasta, subpasta, temporada, name))
    elif parts and parts[0].lower() == "filmes":
        pasta = parts[0]
        subpasta = parts[1] if len(parts) > 1 else ""
        filmes_data.append((pasta, subpasta, name))
    else:
        # Se cair em outra pasta genérica, coloca em filmes por padrão
        pasta = parts[0] if parts else ""
        subpasta = "/".join(parts[1:]) if len(parts) > 1 else ""
        filmes_data.append((pasta, subpasta, name))

# Ordena os dados
series_data.sort(key=lambda x: (x[1], x[2], x[3]))
filmes_data.sort(key=lambda x: (x[1], x[2]))

# Cria a planilha Excel
wb = openpyxl.Workbook()

# Guia de Séries
ws_series = wb.active
ws_series.title = "Séries"
ws_series.append(["Pasta", "Subpasta", "Temporada", "Arquivo"])
for row in series_data:
    ws_series.append(row)

# Guia de Filmes
ws_filmes = wb.create_sheet(title="Filmes")
ws_filmes.append(["Pasta", "Subpasta", "Arquivo"])
for row in filmes_data:
    ws_filmes.append(row)

# Salva o arquivo
excel_filename = "media_completed.xlsx"
wb.save(excel_filename)

print(f"=== SUCESSO ===")
print(f"Planilha '{excel_filename}' criada com sucesso!")
print(f"Total de Séries catalogadas: {len(series_data)}")
print(f"Total de Filmes catalogados: {len(filmes_data)}")

client.close()

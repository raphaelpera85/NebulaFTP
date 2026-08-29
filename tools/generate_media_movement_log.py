import json
import os
import sys
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

def main():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    
    # Check inventory and docs
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    files_col = db.files
    docs = {str(d["_id"]): d for d in files_col.find({"type": "file"})}

    movement_records = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        orig_parent = item.get("mongo_parent") or doc["parent"]
        orig_name = item.get("mongo_name") or doc["name"]
        curr_parent = doc["parent"]
        curr_name = doc["name"]

        orig_full = f"{orig_parent}/{orig_name}"
        curr_full = f"{curr_parent}/{curr_name}"

        status = "RE-ALINHADO" if orig_full != curr_full else "MANTIDO / VERIFICADO"

        movement_records.append({
            "mongo_id": mid,
            "origem_caminho": orig_parent,
            "origem_nome": orig_name,
            "destino_caminho": curr_parent,
            "destino_nome": curr_name,
            "status": status
        })

    # Save to JSON
    json_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/media_movement_log.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(movement_records, f, ensure_ascii=False, indent=2)

    # Save to Markdown Report
    md_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/RELATORIO_MOVIMENTACAO_MIDIAS.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Relatório de Movimentação de Mídias (Origem -> Destino)\n\n")
        f.write(f"Total de mídias mapeadas: **{len(movement_records)}**\n\n")
        f.write("| ID Mídia | Nome / Pasta de Origem | Nome / Pasta de Destino | Status |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")

        for r in movement_records[:200]:  # First 200 items in table
            orig = f"{r['origem_caminho']}/{r['origem_nome']}"
            dest = f"{r['destino_caminho']}/{r['destino_nome']}"
            f.write(f"| `{r['mongo_id'][-8:]}` | `{orig}` | `{dest}` | **{r['status']}** |\n")

        if len(movement_records) > 200:
            f.write(f"\n*... e mais {len(movement_records)-200} registros detalhados gravados no arquivo JSON.*\n")

    print(f"Log de movimentação gerado com sucesso!")
    print(f"  - JSON: {json_path}")
    print(f"  - Markdown: {md_path}")

if __name__ == "__main__":
    main()

"""Aplica de forma reversivel correcoes confirmadas por metadados internos.

Dry-run por padrao. Em --apply:
1. valida que cada documento ainda corresponde ao inventario;
2. copia todos os documentos afetados para uma colecao de backup;
3. libera destinos por uma area temporaria;
4. move rotulos conflitantes e duplicatas para /raphael/Auditoria;
5. aplica e verifica todos os destinos finais.

Nenhum documento e excluido.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pymongo import MongoClient


USER_ROOT = "/raphael"
AUDIT_ROOT = f"{USER_ROOT}/Auditoria"


def unique_name(name: str, mongo_id: str) -> str:
    path = Path(name)
    return f"{path.stem}__{mongo_id[-8:]}{path.suffix}"


def ensure_directory(files, directory_path: str, now: int) -> None:
    if not (directory_path == USER_ROOT or directory_path.startswith(USER_ROOT + "/")):
        raise ValueError(f"diretorio fora da raiz do usuario: {directory_path}")
    relative = [part for part in directory_path[len(USER_ROOT):].strip("/").split("/") if part]
    parent = USER_ROOT
    for name in relative:
        files.update_one(
            {"type": "dir", "parent": parent, "name": name},
            {
                "$setOnInsert": {
                    "type": "dir",
                    "parent": parent,
                    "name": name,
                    "ctime": now,
                    "mtime": now,
                    "size": 0,
                }
            },
            upsert=True,
        )
        parent = f"{parent}/{name}"


def relative_audit_parent(prefix: str, parent: str) -> str:
    relative = parent[len(USER_ROOT):].strip("/") if parent.startswith(USER_ROOT) else "fora_da_raiz"
    return f"{AUDIT_ROOT}/{prefix}/{relative}".rstrip("/")


def candidate_actions(plan: dict[str, Any], kinds: set[str]) -> list[dict[str, Any]]:
    actions = []
    for row in plan["candidates"]:
        if row["kind"] not in kinds:
            continue
        if row["action"] == "move_to_content_path":
            final_parent, final_name = row["desired_parent"], row["desired_name"]
            reason = "content_metadata"
        else:
            final_parent = relative_audit_parent("Duplicatas", row["desired_parent"])
            final_name = unique_name(row["current_name"], row["mongo_id"])
            reason = "duplicate_metadata_match"
        actions.append(
            {
                "mongo_id": row["mongo_id"],
                "obfuscated_id": row.get("obfuscated_id"),
                "expected_parent": row["current_parent"],
                "expected_name": row["current_name"],
                "expected_size": row.get("size"),
                "final_parent": final_parent,
                "final_name": final_name,
                "reason": reason,
                "kind": row["kind"],
                "evidence": row.get("evidence"),
            }
        )
    return actions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--kinds", nargs="+", default=["movie", "series_episode"])
    parser.add_argument("--manifest-dir", type=Path, default=Path(__file__).resolve().parents[2] / "media_audit")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ignore-drift", action="store_true", help="Skip drift check for already-corrected items")
    args = parser.parse_args()

    kinds = set(args.kinds)
    allowed = {"movie", "series_episode", "bleach_episode"}
    if not kinds or not kinds <= allowed:
        raise ValueError(f"kinds invalidos: {sorted(kinds - allowed)}")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    actions = candidate_actions(plan, kinds)
    env = dotenv_values(args.env)
    client = MongoClient(env.get("MONGODB", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
    db = client[env.get("MONGO_DATABASE", "ftp")]
    files = db.files

    ids = [action["mongo_id"] for action in actions]
    if len(ids) != len(set(ids)):
        raise RuntimeError("plano contem documento repetido")

    from bson import ObjectId

    action_by_id = {ObjectId(action["mongo_id"]): action for action in actions}
    live_docs = list(files.find({"_id": {"$in": list(action_by_id)}}))
    live_by_id = {doc["_id"]: doc for doc in live_docs}
    drift = []
    for object_id, action in action_by_id.items():
        doc = live_by_id.get(object_id)
        if not doc:
            drift.append(f"ausente {object_id}")
            continue
        if (
            doc.get("parent") != action["expected_parent"]
            or doc.get("name") != action["expected_name"]
            or doc.get("size") != action["expected_size"]
            or doc.get("obfuscated_id") != action["obfuscated_id"]
        ):
            drift.append(f"alterado {object_id} {doc.get('parent')}/{doc.get('name')}")
    if drift and not args.ignore_drift:
        raise RuntimeError("estado mudou desde o inventario:\n" + "\n".join(drift[:30]))
    elif drift and args.ignore_drift:
        print(f"AVISO: {len(drift)} itens com drift detectado (ignorado por --ignore-drift)")
        for d in drift[:10]:
            print(f"  {d}")

    move_targets = [
        action for action in actions if action["reason"] == "content_metadata"
    ]
    blockers = []
    affected_ids = set(action_by_id)
    for action in move_targets:
        existing = files.find_one(
            {"type": "file", "parent": action["final_parent"], "name": action["final_name"]}
        )
        if existing and existing["_id"] not in affected_ids:
            blockers.append(existing)
            affected_ids.add(existing["_id"])

    final_actions = list(actions)
    for doc in blockers:
        final_actions.append(
            {
                "mongo_id": str(doc["_id"]),
                "obfuscated_id": doc.get("obfuscated_id"),
                "expected_parent": doc.get("parent"),
                "expected_name": doc.get("name"),
                "expected_size": doc.get("size"),
                "final_parent": relative_audit_parent("Rotulos conflitantes", doc.get("parent", "")),
                "final_name": unique_name(doc.get("name", "arquivo.bin"), str(doc["_id"])),
                "reason": "blocking_unverified_label",
                "kind": "blocker",
                "evidence": "destino ocupado por rotulo nao verificado",
            }
        )

    destinations = [(a["final_parent"].casefold(), a["final_name"].casefold()) for a in final_actions]
    if len(destinations) != len(set(destinations)):
        counts: dict[tuple[str, str], int] = {}
        for destination in destinations:
            counts[destination] = counts.get(destination, 0) + 1
        repeated = [f"{parent}/{name}" for (parent, name), count in counts.items() if count > 1]
        raise RuntimeError("destinos finais repetidos:\n" + "\n".join(repeated[:30]))

    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "kinds": sorted(kinds),
        "content_moves": sum(a["reason"] == "content_metadata" for a in final_actions),
        "duplicates_quarantined": sum(a["reason"] == "duplicate_metadata_match" for a in final_actions),
        "blocking_labels_quarantined": sum(a["reason"] == "blocking_unverified_label" for a in final_actions),
        "affected_documents": len(final_actions),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for action in final_actions[:20]:
        print(
            f"{action['expected_parent']}/{action['expected_name']} -> "
            f"{action['final_parent']}/{action['final_name']} [{action['reason']}]"
        )
    if not args.apply:
        print("dry-run concluido; MongoDB nao foi alterado")
        return 0

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"files_backup_content_metadata_{timestamp}"
    backup = db[backup_name]
    if backup.estimated_document_count() != 0:
        raise RuntimeError(f"colecao de backup nao esta vazia: {backup_name}")
    backup_docs = list(files.find({"_id": {"$in": [ObjectId(a["mongo_id"]) for a in final_actions]}}))
    if len(backup_docs) != len(final_actions):
        raise RuntimeError(f"backup incompleto antes da escrita: {len(backup_docs)}/{len(final_actions)}")
    backup.insert_many(backup_docs)
    backup.create_index([("parent", 1), ("name", 1)])

    now = int(time.time())
    staging_parent = f"{AUDIT_ROOT}/.staging_{timestamp}"
    ensure_directory(files, staging_parent, now)
    originals = {doc["_id"]: doc for doc in backup_docs}
    try:
        for action in final_actions:
            object_id = ObjectId(action["mongo_id"])
            result = files.update_one(
                {"_id": object_id},
                {"$set": {"parent": staging_parent, "name": f"{object_id}.stage", "mtime": now}},
            )
            if result.matched_count != 1:
                raise RuntimeError(f"falha ao preparar {object_id}")
        for action in final_actions:
            object_id = ObjectId(action["mongo_id"])
            ensure_directory(files, action["final_parent"], now)
            result = files.update_one(
                {"_id": object_id},
                {"$set": {"parent": action["final_parent"], "name": action["final_name"], "mtime": now}},
            )
            if result.matched_count != 1:
                raise RuntimeError(f"falha ao finalizar {object_id}")
    except Exception:
        for object_id, original in originals.items():
            files.update_one(
                {"_id": object_id},
                {"$set": {"parent": original.get("parent"), "name": original.get("name"), "mtime": original.get("mtime")}},
            )
        raise

    failures = []
    for action in final_actions:
        object_id = ObjectId(action["mongo_id"])
        if files.count_documents(
            {"_id": object_id, "parent": action["final_parent"], "name": action["final_name"]}
        ) != 1:
            failures.append(str(object_id))
    if failures:
        raise RuntimeError("verificacao final falhou: " + ", ".join(failures[:30]))

    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest_dir / f"applied_content_repairs_{timestamp}.json"
    manifest = {
        "summary": summary,
        "backup_collection": backup_name,
        "applied_at": now,
        "actions": final_actions,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"backup_collection={backup_name} backup_documents={backup.count_documents({})}")
    print(f"manifest={manifest_path}")
    print("aplicado e verificado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
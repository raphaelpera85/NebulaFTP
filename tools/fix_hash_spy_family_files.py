import pymongo
import time
import os
from bson import ObjectId

def main():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files
    now = int(time.time())

    user_root = "/raphael"
    
    def ensure_dir(parent_path):
        rel = [p for p in parent_path[len(user_root):].strip("/").split("/") if p]
        curr = user_root
        for p in rel:
            files_col.update_one(
                {"type": "dir", "name": p, "parent": curr},
                {"$setOnInsert": {"type": "dir", "name": p, "parent": curr, "ctime": now, "mtime": now, "size": 0}},
                upsert=True
            )
            curr = f"{curr}/{p}"

    # 1. Fix #Comvocê - Volume 2 (2021) -> Spy x Family S01E03
    doc_spy3 = files_col.find_one({"_id": ObjectId("6a76659654e0892ddf9d6aac")})
    if doc_spy3:
        p_spy = f"{user_root}/Series/Spy × Family/Season 01"
        ensure_dir(p_spy)
        files_col.update_one(
            {"_id": doc_spy3["_id"]},
            {"$set": {"parent": p_spy, "name": "Spy x Family - S01E03.mp4", "mtime": now}}
        )
        print("Moved #Comvocê - Volume 2 (2021) to Series/Spy × Family/Season 01/Spy x Family - S01E03.mp4")

    # 2. Fix #Horror (2015) -> Spy x Family S01E04
    doc_spy4 = files_col.find_one({"_id": ObjectId("6a76659654e0892ddf9d6aae")})
    if doc_spy4:
        p_spy = f"{user_root}/Series/Spy × Family/Season 01"
        ensure_dir(p_spy)
        files_col.update_one(
            {"_id": doc_spy4["_id"]},
            {"$set": {"parent": p_spy, "name": "Spy x Family - S01E04.mp4", "mtime": now}}
        )
        print("Moved #Horror (2015) to Series/Spy × Family/Season 01/Spy x Family - S01E04.mp4")

    # 3. Fix #ComVocê - Volume - 1 (2021) -> Porno/Shikkoku no Shaga
    doc_porn = files_col.find_one({"_id": ObjectId("6a76659654e0892ddf9d6aaa")})
    if doc_porn:
        p_porn = f"{user_root}/Porno/Shikkoku no Shaga"
        ensure_dir(p_porn)
        files_col.update_one(
            {"_id": doc_porn["_id"]},
            {"$set": {"parent": p_porn, "name": "Shikkoku no Shaga - Episode 01.mp4", "mtime": now}}
        )
        print("Moved #ComVocê - Volume - 1 (2021) to Porno/Shikkoku no Shaga/Shikkoku no Shaga - Episode 01.mp4")

    # Delete leftover empty # directories in Filmes
    hash_dirs = list(files_col.find({"type": "dir", "parent": f"{user_root}/Filmes", "name": {"$regex": "^#"}}) )
    for d in hash_dirs:
        dir_path = f"{d['parent']}/{d['name']}"
        if files_col.count_documents({"parent": dir_path}) == 0:
            files_col.delete_one({"_id": d["_id"]})
            print(f"Deleted empty directory {dir_path}")

if __name__ == "__main__":
    main()

import pymongo
import re
import json
import os
import sys
from bson import ObjectId

sys.path.insert(0, os.path.dirname(__file__))
from validar_midias_ia import probe_live_metadata, process_single_file

client = pymongo.MongoClient("mongodb://localhost:27017")
db = client.ftp

porno_docs = list(db.files.find({"type": "file", "parent": re.compile(r"^/raphael/Porno")}))
print(f"Total docs under Porno: {len(porno_docs)}")

for d in porno_docs[:5]:
    print("\n--------------------------------------------------")
    print("MONGO ID:  ", str(d["_id"]))
    print("PARENT:    ", repr(d["parent"]))
    print("NAME:      ", repr(d["name"]))

    dur, tags = probe_live_metadata(d)
    print("PROBED DUR:", dur / 60.0, "min")
    print("PROBED TAGS:", tags)

    res = process_single_file(d, {}, "http://127.0.0.1:1234/v1/chat/completions", "huihui-qwen3.5-9b-abliterated")
    print("PROCESS RESULT:", res)

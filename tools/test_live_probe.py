import subprocess
import json
import pymongo
import re

FFPROBE = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"
client = pymongo.MongoClient("mongodb://localhost:27017")
db = client.ftp

doc = db.files.find_one({"type": "file", "parent": re.compile(r"^/raphael/Porno"), "name": re.compile(r"My Family Pies")})
if doc:
    rel = f"{doc['parent']}/{doc['name']}".replace("/raphael/", "N:\\").replace("/", "\\")
    print("Testing live probe on path:", rel)
    res = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", rel], capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        dur = float(data.get("format", {}).get("duration", 0)) / 60.0
        tags = data.get("format", {}).get("tags", {})
        print(f"PROBED DURATION: {dur:.1f} minutes")
        print("STREAM TAGS:", tags)
    except Exception as e:
        print("Probe error:", e)

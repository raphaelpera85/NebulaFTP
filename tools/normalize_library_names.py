import argparse
import configparser
import os
import re
from base64 import urlsafe_b64decode
from ftplib import FTP
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pymongo import MongoClient


ROOT = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}/Filmes'
RCLONE_KEY = bytes.fromhex(
    "9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638"
)
SPECIAL = {
    "0.H0m3m.d4s.Tr3v4s.2016.BDRip.XviD.Dual": "O Homem nas Trevas (2016)",
    "01 - A Maldição do Pérola Negra": "Piratas do Caribe - A Maldição do Pérola Negra (2003)",
    "Argentina.1985.2022.SPANISH.1080p.WEBRip.x265-VXT": "Argentina, 1985 (2022)",
    "A Família Addams": "A Família Addams (1991)",
    "A Mosca": "A Mosca (1986)",
    "A lagoa azul": "A Lagoa Azul (1980)",
    "Amizade Colorida": "Amizade Colorida (2011)",
    "Babe - O porquinho atrapalhado": "Babe - O Porquinho Atrapalhado (1995)",
    "Bicho de Sete Cabecas.Mp4.720p": "Bicho de Sete Cabeças (2001)",
    "Dual 2022 1080p BluRay DUAL 5.1": "Dual (2022)",
    "Minha Mae e uma Peca 3": "Minha Mãe é uma Peça 3 (2019)",
    "Minha Mãe é uma Peça": "Minha Mãe é uma Peça (2013)",
    "O Homem do Futuro": "O Homem do Futuro (2011)",
    "Sucker Punch Mundo Surreal": "Sucker Punch - Mundo Surreal (2011)",
    "Titanic 4k Matered Dublado - YTSBR.COM": "Titanic (1997)",
    "Ó Paí, Ó": "Ó Paí, Ó (2007)",
}
NOISE_PREFIX = re.compile(r"(?i)^(?:COMANDO\.TO\s*-\s*)")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
PAREN_YEAR = re.compile(r"\(((?:19|20)\d{2})\)")
TRAILING_EDITION = re.compile(
    r"(?ix)"
    r"\s*(?:"
    r"\[[^\]]+\]|"
    r"\(?\b(?:480p|720p|1080p|2160p|4k|full\s*hd|bluray|blu-ray|bdrip|brrip|"
    r"webrip|web-dl|web|dvdrip|hdrip|remux|xvid|x264|x265|h264|h265|hevc|"
    r"av1|aac|ac3|eac3|ddp\d*(?:\.\d)?|dual|dublado|legendado|nacional|"
    r"portuguese|spanish|danish|french|proper|remastered|imax|docu|"
    r"\d+(?:\.\d+)?ch|10bit|fullscreen)\b.*"
    r")$"
)


def clean_title(raw: str, year_start: int) -> str:
    title = NOISE_PREFIX.sub("", raw[:year_start]).strip(" ._-")
    title = re.sub(r"^\d{1,2}\.(?=[A-Za-zÀ-ÿ])", "", title)
    if "." in title and title.count(" ") < title.count("."):
        title = re.sub(r"(?<=\w)\.(?=\w)", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-[]")
    return title


def normalized_movie_name(name: str, child_names: list[str]) -> str | None:
    if name in SPECIAL:
        return SPECIAL[name]
    if re.fullmatch(r".+ \((?:19|20)\d{2}\)", name):
        return name
    sources = [name, *child_names]
    for source in sources:
        paren = list(PAREN_YEAR.finditer(source))
        years = list(YEAR.finditer(source))
        match = paren[-1] if paren else (years[-1] if years else None)
        if not match:
            continue
        year = match.group(1)
        title = clean_title(name if YEAR.search(name) else source, match.start())
        title = TRAILING_EDITION.sub("", title).strip(" ._-")
        if title:
            return f"{title} ({year})"
    return None


def ftp_settings() -> tuple[str, int, str, str]:
    path = Path.home() / "AppData/Roaming/rclone/rclone.conf"
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    section = cfg["nebula"]
    encrypted = urlsafe_b64decode(section["pass"] + "===")
    iv, payload = encrypted[:16], encrypted[16:]
    password = Cipher(algorithms.AES(RCLONE_KEY), modes.CTR(iv)).decryptor().update(payload)
    return (
        section.get("host", "127.0.0.1"),
        section.getint("port", 2121),
        section["user"],
        password.decode("utf-8"),
    )


def build_plan(files) -> tuple[list[tuple[str, str, list[str]]], list[tuple[str, str]]]:
    dirs = list(files.find({"parent": ROOT, "type": "dir"}, {"name": 1}))
    existing = {doc["name"] for doc in dirs}
    plan, skipped = [], []
    for doc in sorted(dirs, key=lambda item: item["name"].casefold()):
        old = doc["name"]
        children = [
            child["name"]
            for child in files.find({"parent": f"{ROOT}/{old}"}, {"name": 1})
        ]
        new = normalized_movie_name(old, children)
        if not new or new == old:
            continue
        if new in existing and new != old:
            skipped.append((old, f"colisão com {new}"))
            continue
        plan.append((old, new, children))
        existing.remove(old)
        existing.add(new)
    return plan, skipped


def apply_plan(plan: list[tuple[str, str, list[str]]], files) -> None:
    host, port, user, password = ftp_settings()
    with FTP() as ftp:
        ftp.connect(host, port, timeout=30)
        ftp.login(user, password)
        for old, new, children in plan:
            old_path = PurePosixPath("/Filmes", old)
            new_path = PurePosixPath("/Filmes", new)
            ftp.mkd(new_path.as_posix())
            media_children = [
                child for child in children
                if Path(child).suffix.lower() in {".avi", ".m4v", ".mkv", ".mp4", ".mov", ".ts", ".wmv"}
            ]
            for child in children:
                destination = child
                if len(media_children) == 1 and child == media_children[0]:
                    destination = f"{new}{Path(child).suffix.lower()}"
                ftp.rename(
                    (old_path / child).as_posix(),
                    (new_path / destination).as_posix(),
                )
            moved = files.count_documents({"parent": f"{ROOT}/{new}"})
            if moved != len(children):
                raise RuntimeError(
                    f"renome incompleto: {old} -> {new}; "
                    f"esperado={len(children)} movido={moved}"
                )
            ftp.rmd(old_path.as_posix())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))[
        os.getenv("MONGO_DATABASE", "ftp")
    ].files
    plan, skipped = build_plan(files)
    print(f"renames={len(plan)} skipped={len(skipped)} mode={'apply' if args.apply else 'dry-run'}")
    for old, new, _ in plan:
        print(f"{old} -> {new}")
    for old, reason in skipped:
        print(f"SKIP {old}: {reason}")
    if args.apply:
        apply_plan(plan, files)


if __name__ == "__main__":
    assert normalized_movie_name(
        "0.H0m3m.d4s.Tr3v4s.2016.BDRip.XviD.Dual", []
    ) == "O Homem nas Trevas (2016)"
    assert normalized_movie_name(
        "A.Mulher.Rei.2022.WEB-DL.1080p.DUAL.5.1", []
    ) == "A Mulher Rei (2022)"
    main()

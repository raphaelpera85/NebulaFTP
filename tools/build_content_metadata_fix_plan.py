"""Monta um plano de reparo a partir de metadados internos extraidos por ffprobe.

Este script nao altera o banco. Ele usa somente evidencias fortes:
- release title contendo serie + SxxEyy;
- titulo de filme contendo ano;
- IDs IMDb/TMDB;
- episodios de Bleach associados a uma lista cronologica externa em cache.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path, PureWindowsPath
from typing import Any


EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".wmv"}
GENERIC_TITLES = re.compile(
    r"(?i)^(?:comando(?:\.la)?|lapumia(?:filmes\.com)?|by .+|acesse .+|"
    r"www\.[^ ]+|bludv(?:\.to)?|the pirate filmes)$"
)
PREFIX_RE = re.compile(r"(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?)\s*-\s*")
TECH_TOKEN_RE = re.compile(
    r"(?i)(?:\b(?:2160p|1080p|720p|480p|4k|bluray|brrip|webrip|web[- .]?dl|hdtv|"
    r"amzn|nf|dsnp|proper|remastered|x26[45]|h\.?26[45]|hevc|aac|eac3|ddp?5|"
    r"dual|dublado|portuguese|spanish|extended|hdr|10bit|av1)\b|\[[^]]+\])"
)


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]


def smart_title(value: str) -> str:
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    if not value:
        return value
    letters = [ch for ch in value if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.65:
        return value.title()
    if value == value.lower():
        return value.title()
    return value


def actual_extension(item: dict[str, Any]) -> str:
    fmt = (item.get("format_name") or "").casefold()
    if "matroska" in fmt:
        return ".mkv"
    if any(name in fmt for name in ("mov", "mp4", "m4a", "3gp")):
        return ".mp4"
    if "avi" in fmt:
        return ".avi"
    suffix = Path(item.get("mongo_name", "")).suffix.casefold()
    return suffix if suffix in VIDEO_EXTENSIONS else ".mkv"


def lower_tags(item: dict[str, Any]) -> dict[str, str]:
    return {str(key).casefold(): str(value) for key, value in (item.get("format_tags") or {}).items()}


def clean_release_prefix(value: str) -> str:
    value = PREFIX_RE.sub("", value.strip())
    value = re.sub(r"(?i)^[^|]*(?:rip|\.com)[^|]*\|\s*", "", value)
    value = re.sub(r"(?i)^encoded by [^-]+-\s*", "", value)
    return value.strip()


def series_candidate(item: dict[str, Any], title: str) -> dict[str, Any] | None:
    match = EPISODE_RE.search(title)
    if not match:
        return None
    show_raw = clean_release_prefix(title[: match.start()])
    show_raw = re.sub(r"[._ -]+$", "", show_raw)
    show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
    show = safe_component(smart_title(show_raw))
    if len(normalized(show)) < 2:
        return None
    season, episode = int(match.group(1)), int(match.group(2))
    extension = actual_extension(item)
    filename = safe_component(f"{show} - S{season:02d}E{episode:02d}") + extension
    return {
        "kind": "series_episode",
        "show": show,
        "season": season,
        "episode": episode,
        "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
        "desired_name": filename,
        "confidence": 0.99,
        "evidence": f"embedded_title={title}",
    }


def movie_candidate(item: dict[str, Any], title: str, tags: dict[str, str]) -> dict[str, Any] | None:
    value = clean_release_prefix(title)
    match = YEAR_RE.search(value)
    if not match:
        return None
    before = value[: match.start()]
    before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
    before = re.sub(r"[.(\s]+$", "", before)
    movie = safe_component(smart_title(before))
    if len(normalized(movie)) < 2 or GENERIC_TITLES.match(movie):
        return None
    year = int(match.group(1))
    canonical = safe_component(f"{movie} ({year})")
    ids = {key: tags[key] for key in ("imdb_id", "tmdb_id") if tags.get(key)}
    confidence = 1.0 if ids else 0.97
    return {
        "kind": "movie",
        "title": movie,
        "year": year,
        "desired_parent": f"/raphael/Filmes/{canonical}",
        "desired_name": canonical + actual_extension(item),
        "confidence": confidence,
        "evidence": f"embedded_title={title}" + (f" ids={ids}" if ids else ""),
        "external_ids": ids,
    }


def bleach_targets(feed_state: Path) -> list[dict[str, Any]]:
    rows = json.loads(feed_state.read_text(encoding="utf-8-sig"))
    parsed = []
    for raw in rows:
        path = PureWindowsPath(raw)
        text = str(path)
        match = re.search(r"(?i)\\Series\\Bleach\\Season (\d{2})\\.*?S(\d{2})E(\d{1,3})", text)
        if not match or int(match.group(1)) in {0, 17}:
            continue
        parsed.append(
            {
                "season": int(match.group(2)),
                "episode": int(match.group(3)),
                "name": path.name,
            }
        )
    parsed.sort(key=lambda row: (row["season"], row["episode"]))
    return parsed


def bleach_catalog(tvmaze_path: Path, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = json.loads(tvmaze_path.read_text(encoding="utf-8"))
    episodes = payload.get("_embedded", {}).get("episodes", [])
    episodes = [episode for episode in episodes if episode.get("type") == "regular"]
    episodes.sort(key=lambda episode: (episode.get("airdate") or "", episode.get("id") or 0))
    if len(targets) > len(episodes):
        raise RuntimeError(f"Catalogo Bleach divergente: tvmaze={len(episodes)} targets={len(targets)}")
    # A biblioteca preservada termina tres episodios antes do fim da serie
    # classica e depois passa ao Thousand-Year Blood War (Season 17).
    episodes = episodes[: len(targets)]
    result = []
    for index, (episode, target) in enumerate(zip(episodes, targets, strict=True), 1):
        result.append({"absolute": index, "title": episode.get("name", ""), "target": target})
    return result


def title_similarity(left: str, right: str) -> float:
    a, b = normalized(left), normalized(right)
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    containment = min(len(a_tokens), len(b_tokens)) and len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))
    return max(sequence, 0.55 * token_score + 0.45 * containment)


def bleach_candidate(
    item: dict[str, Any], title: str, catalog: list[dict[str, Any]]
) -> dict[str, Any] | None:
    explicit = re.search(r"(?i)epis[oó]dio\s+(\d{1,3}).*\[Bleach\]", title)
    score = 1.0
    if explicit:
        absolute = int(explicit.group(1))
        matches = [row for row in catalog if row["absolute"] == absolute]
        if not matches:
            return None
        best = matches[0]
        second_score = 0.0
    else:
        if "---" not in title:
            return None
        query = title.split("---", 1)[1].strip()
        ranked = sorted(
            ((title_similarity(query, row["title"]), row) for row in catalog),
            key=lambda pair: pair[0],
            reverse=True,
        )
        score, best = ranked[0]
        second_score = ranked[1][0]
        if score < 0.70 or score - second_score < 0.035:
            return None
    target = best["target"]
    return {
        "kind": "bleach_episode",
        "show": "Bleach",
        "season": target["season"],
        "episode": target["episode"],
        "desired_parent": f"/raphael/Series/Bleach/Season {target['season']:02d}",
        "desired_name": target["name"],
        "confidence": round(min(0.995, score), 4),
        "evidence": f"embedded_title={title}; matched_episode_title={best['title']}; absolute={best['absolute']}",
        "match_score": round(score, 4),
        "match_margin": round(score - second_score, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--feed-state", type=Path, required=True)
    parser.add_argument("--tvmaze-bleach", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.inventory.suffix.casefold() == ".jsonl":
        inventory = []
        for line in args.inventory.read_text(encoding="utf-8").splitlines():
            try:
                inventory.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    else:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    catalog = bleach_catalog(args.tvmaze_bleach, bleach_targets(args.feed_state))
    candidates = []
    rejected = Counter()
    for item in inventory:
        if item.get("probe_error"):
            rejected["probe_error"] += 1
            continue
        tags = lower_tags(item)
        title = tags.get("title", "").strip()
        if not title or GENERIC_TITLES.match(title):
            rejected["no_informative_title"] += 1
            continue
        candidate = series_candidate(item, title)
        if candidate is None:
            candidate = bleach_candidate(item, title, catalog)
        if candidate is None:
            candidate = movie_candidate(item, title, tags)
        if candidate is None:
            rejected["unparsed_title"] += 1
            continue
        candidate.update(
            {
                "mongo_id": item["mongo_id"],
                "obfuscated_id": item.get("obfuscated_id"),
                "current_parent": item["mongo_parent"],
                "current_name": item["mongo_name"],
                "size": item.get("mongo_size"),
                "duration_seconds": item.get("duration_seconds"),
                "first_tg_message": item.get("first_tg_message"),
            }
        )
        candidates.append(candidate)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate["desired_parent"].casefold(), candidate["desired_name"].casefold())].append(candidate)
    for rows in grouped.values():
        rows.sort(
            key=lambda row: (
                bool(row.get("size") and row["size"] > 67108864),
                row.get("size") or 0,
                row.get("confidence") or 0,
            ),
            reverse=True,
        )
        rows[0]["selected_copy"] = True
        rows[0]["action"] = "move_to_content_path"
        for duplicate in rows[1:]:
            duplicate["selected_copy"] = False
            duplicate["action"] = "quarantine_duplicate"
            duplicate["duplicate_of_mongo_id"] = rows[0]["mongo_id"]

    candidates.sort(key=lambda row: (row["action"] != "move_to_content_path", row["desired_parent"], row["desired_name"]))
    summary = {
        "inventory_count": len(inventory),
        "candidate_count": len(candidates),
        "selected_content_moves": sum(row["action"] == "move_to_content_path" for row in candidates),
        "duplicate_quarantines": sum(row["action"] == "quarantine_duplicate" for row in candidates),
        "by_kind": dict(Counter(row["kind"] for row in candidates)),
        "rejected": dict(rejected),
    }
    output = {"summary": summary, "candidates": candidates}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

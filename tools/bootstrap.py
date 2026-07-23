from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


def requirement_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def package_name(requirement: str) -> str:
    match = REQ_RE.match(requirement)
    if not match:
        raise ValueError(f"Requisito invalido: {requirement}")
    return match.group(1)


def missing_requirements(requirements: list[str]) -> list[str]:
    missing: list[str] = []
    for requirement in requirements:
        try:
            importlib.metadata.distribution(package_name(requirement))
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement)
    return missing


def install(requirements: list[str]) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *requirements])


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    req_file = root / "requirements.txt"
    if not req_file.exists():
        print(f"requirements.txt nao encontrado: {req_file}")
        return 2
    missing = missing_requirements(requirement_lines(req_file))
    if not missing:
        print("Dependencias Python OK.")
        return 0
    print("Instalando dependencias Python ausentes: " + ", ".join(package_name(item) for item in missing))
    install(missing)
    print("Dependencias Python instaladas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

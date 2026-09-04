import json
import urllib.request
import sys

sys.stdout.reconfigure(encoding='utf-8')

def classify_media_with_local_ai(probe_title: str, duration_min: float, curr_name: str) -> dict:
    url = "http://127.0.0.1:1234/v1/chat/completions"
    
    prompt = f"""Analise as informações extraídas do stream do vídeo e identifique o nome correto do filme ou série.

Informações do vídeo:
- Título/Tag do Stream: "{probe_title}"
- Nome Atual do Arquivo: "{curr_name}"
- Duração: {duration_min:.1f} minutos

Instruções:
1. Determine a categoria: "Filmes", "Series" ou "Porno".
2. Se for filme, forneça o título limpo e o ano (YYYY). Exemplo de canonical_name: "Gran Turismo - De Jogador a Corredor (2023).mkv"
3. Se for série/anime, forneça o nome da série, temporada (SXX) e episódio (EXX). Exemplo de canonical_name: "Sons of Anarchy - S06E01.mp4"
4. Retorne APENAS um objeto JSON no formato:
{{
  "category": "Filmes" | "Series" | "Porno",
  "show_or_movie": "Nome Limpo",
  "season": 1,
  "episode": 1,
  "year": 2023,
  "canonical_name": "Nome Limpo (2023).mkv",
  "desired_parent": "/raphael/Filmes/Nome Limpo (2023)"
}}
"""

    payload = {
        "model": "huihui-qwen3.5-9b-abliterated",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 300
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return json.loads(content[content.find("{"):content.rfind("}")+1])
    except Exception as e:
        print(f"Error querying local AI: {e}")
        return {}

def main():
    test_cases = [
        ("Sons of Anarchy S06 / By-LuanHarper / The Pirate Filmes", 63.8, "Big Titty StepSis.mp4"),
        ("Gran.Turismo.2023.1080p.WEB-DL.DUAL", 134.5, "My Sister's BFF.mp4"),
        ("Dracula.The.Last.Voyage.of.the.Demeter.2023.720p", 118.2, "My Family Pies.mp4")
    ]

    for title, dur, curr in test_cases:
        print(f"\n--- QUERYING LOCAL AI FOR: '{title}' ---")
        res = classify_media_with_local_ai(title, dur, curr)
        print(json.dumps(res, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

import json
import urllib.request
import sys

sys.stdout.reconfigure(encoding='utf-8')

def main():
    url = "http://127.0.0.1:1234/v1/chat/completions"
    
    prompt = """Analise estas informações extraídas do stream do vídeo e me diga o nome correto do filme ou série:

Tag do Stream: "Sons of Anarchy S06 / By-LuanHarper / The Pirate Filmes"
Duração: 63.8 minutos

Responda em formato JSON limpo."""

    payload = {
        "model": "huihui-qwen3.5-9b-abliterated",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            print("RAW RESPONSE FROM LM STUDIO:")
            print(raw)
            data = json.loads(raw)
            print("\nEXTRACTED CONTENT:")
            print(data["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"Error querying local AI: {e}")

if __name__ == "__main__":
    main()

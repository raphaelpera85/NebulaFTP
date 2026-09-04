import json
import urllib.request
import sys

sys.stdout.reconfigure(encoding='utf-8')

def main():
    url = "http://127.0.0.1:1234/v1/chat/completions"
    
    prompt = "Qual é o título limpo e o ano deste filme/série: 'Gran.Turismo.2023.1080p'? Responda apenas com o nome e o ano no formato: Nome do Filme (Ano)"

    payload = {
        "model": "huihui-qwen3.5-9b-abliterated",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 500
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            msg = data["choices"][0]["message"]
            print("REASONING CONTENT:")
            print(msg.get("reasoning_content"))
            print("\nFINAL CONTENT:")
            print(msg.get("content"))
    except Exception as e:
        print(f"Error querying local AI: {e}")

if __name__ == "__main__":
    main()

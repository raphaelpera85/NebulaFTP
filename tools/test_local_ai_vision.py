import base64
import json
import urllib.request
from pathlib import Path

def image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")

def main():
    frame_path = Path("d:/Users/rapha/Documents/Projetos/nebula/media_audit/porno_frames/6a76659854e0892ddf9d7243.jpg")
    if not frame_path.exists():
        print(f"Frame file {frame_path} not found.")
        return

    b64 = image_to_base64(frame_path)
    url = "http://127.0.0.1:1234/v1/chat/completions"

    payload = {
        "model": "huihui-qwen3.5-9b-abliterated",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Identifique o filme, série de TV ou desenho animado presente nesta imagem. Responda estritamente em formato JSON com a chave 'title', 'year', 'season', 'episode', 'category' (Filmes, Series, Porno)."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        }
                    }
                ]
            }
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("LOCAL AI RESPONSE:")
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error querying local AI: {e}")

if __name__ == "__main__":
    main()

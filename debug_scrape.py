import asyncio
import json
import re
import httpx

URL = "https://www.tiktok.com/@jek.notes/photo/7683966574105038088"

async def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        resp = await client.get(URL)
        html = resp.text

    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        print("Script tag NOT FOUND")
        return

    data = json.loads(match.group(1))
    scope = data.get("__DEFAULT_SCOPE__", {})
    print("Top-level keys in __DEFAULT_SCOPE__:")
    for key in scope.keys():
        print(" -", key)

    # Dump full structure to file for inspection
    with open("debug_output.json", "w") as f:
        json.dump(data, f, indent=2)
    print("\nFull JSON saved to debug_output.json")

asyncio.run(main())

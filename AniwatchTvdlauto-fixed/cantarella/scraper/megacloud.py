#@cantarellabots
from cantarella.core.proxy import get_random_proxy, get_proxy_dict
import base64
import re
import json
from curl_cffi import requests
from typing import Callable, Iterable, Any

def hash_str(key: str) -> int:
    key_value = 0
    for char in key:
        key_value = (key_value * 31 + ord(char)) & 0xFFFFFFFF
    return key_value

class Megacloud:
    base_url = "https://megacloud.tv"
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "origin": base_url,
        "referer": base_url + "/",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
    }

    def __init__(self, embed_url: str) -> None:
        self.embed_url = embed_url

    def _extract_client_key(self, html: str) -> str:
        """Try multiple patterns to extract the client key from embed page JS."""
        # Pattern 1: 48-char standalone key
        m = re.search(r'["\']([a-zA-Z0-9]{48})["\']', html)
        if m:
            return m.group(1)
        # Pattern 2: x/y/z split key  e.g.  x: "AAAA", y: "BBBB", z: "CCCC"
        m = re.search(r'x:\s*["\']([a-zA-Z0-9]{16})["\'],\s*y:\s*["\']([a-zA-Z0-9]{16})["\'],\s*z:\s*["\']([a-zA-Z0-9]{16})["\']', html)
        if m:
            return m.group(1) + m.group(2) + m.group(3)
        # Pattern 3: _k variable assignment
        m = re.search(r'_k\s*=\s*["\']([a-zA-Z0-9]{16,64})["\']', html)
        if m:
            return m.group(1)
        # Pattern 4: key in script tag as JSON
        m = re.search(r'"key"\s*:\s*"([a-zA-Z0-9]{16,64})"', html)
        if m:
            return m.group(1)
        return ""

    def _lcg(self, n: int) -> int:
        return (n * 1103515245 + 12345) & 0x7FFFFFFF

    def _shuffle_sources(self, sources: list, key: str) -> list:
        if not key:
            return sources
        array_count = len(sources) // len(key)
        if array_count == 0:
            return sources
        arrays = [[""] * len(key) for _ in range(array_count)]
        key_dict = {i: char for i, char in enumerate(key)}
        key_sorted = {i: char for i, char in sorted(key_dict.items(), key=lambda p: p[1])}
        p = 0
        for idx in key_sorted.keys():
            for arr_idx in range(array_count):
                if p < len(sources):
                    arrays[arr_idx][idx] = sources[p]
                    p += 1
        res = []
        for arr in arrays:
            res.extend(arr)
        return res

    def _process_sources(self, encrypted_data: str, key: str) -> str:
        sources = list(encrypted_data)
        current_hash = hash_str(key)
        new_sources = []
        for char in sources:
            current_hash = self._lcg(current_hash)
            val1 = ord(char) - 32
            val2 = current_hash % 95
            v = (val1 - val2) % 95 + 32
            new_sources.append(chr(v))
        shuffled = self._shuffle_sources(new_sources, key)
        return "".join(shuffled)

    def _try_get_sources(self, session, base_url: str, sid: str, client_key: str, referer: str) -> dict | None:
        """Try to fetch and decrypt sources from getSources endpoint."""
        get_src_url = f"{base_url}/embed-2/v3/e-1/getSources"
        headers = self.headers.copy()
        headers["referer"] = referer
        headers["x-requested-with"] = "XMLHttpRequest"

        try:
            resp_obj = session.get(
                get_src_url,
                headers=headers,
                params={"id": sid, "_k": client_key},
                impersonate="chrome",
                timeout=15
            )
            if resp_obj.status_code != 200:
                return None
            resp = resp_obj.json()
        except Exception as e:
            print(f"Megacloud getSources error: {e}")
            return None

        # Decrypt if sources is still a string
        if isinstance(resp.get("sources"), str) and client_key:
            try:
                decrypted = self._process_sources(resp["sources"], client_key)
                resp["sources"] = json.loads(decrypted)
            except Exception as e:
                print(f"Megacloud decryption error: {e}")
                # Try base64 fallback
                try:
                    decoded = base64.b64decode(resp["sources"]).decode()
                    resp["sources"] = json.loads(decoded)
                except Exception:
                    resp["sources"] = []

        if not isinstance(resp.get("sources"), list):
            resp["sources"] = []
        if "tracks" not in resp:
            resp["tracks"] = []
        return resp

    def extract(self) -> dict:
        # Extract SID from embed URL
        sid_match = re.search(r"e-1/([a-zA-Z0-9]+)", self.embed_url)
        if not sid_match:
            print(f"Megacloud: could not extract SID from {self.embed_url}")
            return {"sources": [], "tracks": []}
        sid = sid_match.group(1)

        try:
            session = requests.Session()
            proxy_dict = get_proxy_dict(get_random_proxy())
            if proxy_dict:
                session.proxies.update(proxy_dict)

            # Always prefer megacloud.tv
            curr_embed_url = re.sub(r"megacloud\.\w+", "megacloud.tv", self.embed_url)
            curr_embed_url = curr_embed_url.replace(".blog", ".tv")
            base_url = "https://megacloud.tv"

            headers = self.headers.copy()
            headers["referer"] = "https://hianime.to/"

            # Fetch embed page to get client key
            resp_html = session.get(
                curr_embed_url,
                headers=headers,
                impersonate="chrome",
                timeout=15
            ).text

            client_key = self._extract_client_key(resp_html)
            print(f"Megacloud client_key={'[found]' if client_key else '[NOT FOUND]'} len={len(client_key)}")

            # Try with extracted key first
            result = self._try_get_sources(session, base_url, sid, client_key, curr_embed_url)

            # If no sources and we have a key, try with empty key (some episodes are unencrypted)
            if (not result or not result.get("sources")) and client_key:
                print("Megacloud: retrying with empty key (unencrypted fallback)")
                result = self._try_get_sources(session, base_url, sid, "", curr_embed_url)

            if result and result.get("sources"):
                return result

            print(f"Megacloud: no sources found for SID={sid}")
            return {"sources": [], "tracks": []}

        except Exception as e:
            print(f"Megacloud extract error: {e}")
            return {"sources": [], "tracks": []}

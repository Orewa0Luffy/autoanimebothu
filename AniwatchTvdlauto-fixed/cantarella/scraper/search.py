#@cantarellabots
from cantarella.core.proxy import get_random_proxy, get_proxy_dict
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as c_requests
BASE_URL = "https://hianime.to"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

def search_anime(query, source="aniwatch"):
    if source == "animetsu":
        from cantarella.scraper.animetsu import AnimetsuScraper
        return AnimetsuScraper().search_anime(query)

    search_url = f"{BASE_URL}/search?keyword={query.replace(' ', '+')}"
    try:
        session = c_requests.Session()
        proxy_dict = get_proxy_dict(get_random_proxy())
        if proxy_dict:
            session.proxies.update(proxy_dict)
        resp = session.get(search_url, headers=HEADERS, impersonate="chrome")
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []

        # cantarella search results are usually in .film_list-wrap .flw-item
        for item in soup.select('.film_list-wrap .flw-item'):
            title_elem = item.select_one('.film-name a')
            if not title_elem:
                continue

            title = title_elem.get('title') or title_elem.text.strip()
            href = title_elem.get('href')

            # extract anime_id
            # href format: /watch/anime-name-id
            # or just /anime-name-id
            anime_id = href.split('/')[-1].split('?')[0]

            # Get release year / type if available
            type_elem = item.select_one('.fdi-item')
            anime_type = type_elem.text.strip() if type_elem else "Unknown"

            results.append({
                'title': title,
                'id': anime_id,
                'type': anime_type,
                'url': f"{BASE_URL}{href}" if href.startswith('/') else f"{BASE_URL}/{href}"
            })

            # Limit to top 10 results to avoid huge keyboards
            if len(results) >= 10:
                break

        return results
    except Exception as e:
        print(f"Search error: {e}")
        return []

if __name__ == '__main__':
    print(search_anime('naruto'))

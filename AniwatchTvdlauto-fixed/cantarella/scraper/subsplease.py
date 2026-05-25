#@cantarellabots
# SubsPlease scraper
# Source: https://subsplease.org
# Method: Public RSS feed (magnet/torrent) + undocumented JSON API
# Download: aria2c (magnet/torrent) instead of N_m3u8DL-RE
#
# SubsPlease releases pre-muxed 720p/1080p MKV files with English subs.
# Quality is excellent — same as what you'd find on Nyaa from SubsPlease.

import re
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

from curl_cffi import requests as c_requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

RSS_BASE = "https://subsplease.org/rss"
API_BASE = "https://subsplease.org/api"


class SubsPleaseScraper:
    """
    Scrapes SubsPlease RSS for new episode magnet links and downloads
    them via aria2c. Mirrors the same interface as AnimetsuScraper so
    ongoing.py can call it identically.
    """

    def __init__(self, download_path="anime_downloads", progress_queue=None):
        self.download_path = Path(download_path)
        self.download_path.mkdir(exist_ok=True)
        self.progress_queue = progress_queue
        self.session = c_requests.Session()
        self._aria2c = self._find_aria2c()

    # ─────────────────────────────────────────────
    # Tool discovery
    # ─────────────────────────────────────────────

    def _find_aria2c(self) -> str | None:
        """Return path to aria2c binary, or None if not found."""
        p = shutil.which("aria2c")
        if p:
            print(f"[SubsPlease] Found aria2c: {p}")
            return p
        print("[SubsPlease] WARNING: aria2c not found. Install it with: apt-get install aria2")
        return None

    # ─────────────────────────────────────────────
    # RSS helpers
    # ─────────────────────────────────────────────

    def _fetch_rss(self, resolution: str = "1080") -> list[dict]:
        """
        Fetch the SubsPlease RSS feed for the given resolution.
        Returns a list of dicts: {title, magnet, torrent_url, pub_date}
        """
        url = f"{RSS_BASE}/?t&r={resolution}"
        try:
            resp = self.session.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                print(f"[SubsPlease] RSS fetch failed: HTTP {resp.status_code}")
                return []

            root = ET.fromstring(resp.text)
            ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
            items = []
            for item in root.findall(".//item"):
                title_el = item.find("title")
                link_el  = item.find("link")
                pub_el   = item.find("pubDate")
                enclosure = item.find("enclosure")

                title     = title_el.text.strip() if title_el is not None else ""
                magnet    = link_el.text.strip() if link_el is not None else ""
                torrent   = enclosure.get("url", "") if enclosure is not None else ""
                pub_date  = pub_el.text.strip() if pub_el is not None else ""

                if title:
                    items.append({
                        "title":       title,
                        "magnet":      magnet,
                        "torrent_url": torrent,
                        "pub_date":    pub_date,
                    })
            return items
        except Exception as e:
            print(f"[SubsPlease] RSS parse error: {e}")
            return []

    def _parse_rss_title(self, title: str) -> dict:
        """
        Parse a SubsPlease release title like:
          [SubsPlease] Bleach - Thousand-Year Blood War - 21 (1080p) [ABCD1234].mkv
        Returns: {anime, ep_num, quality, hash}
        """
        # Remove [SubsPlease] prefix
        title = re.sub(r"^\[SubsPlease\]\s*", "", title)

        # Extract hash  e.g. [ABCD1234]
        hash_match = re.search(r"\[([0-9A-Fa-f]{8})\]", title)
        file_hash  = hash_match.group(1) if hash_match else ""

        # Extract quality  e.g. (1080p)
        qual_match = re.search(r"\((\d+p)\)", title)
        quality    = qual_match.group(1) if qual_match else "1080p"

        # Remove quality and hash from title to isolate "Anime Name - EP"
        clean = re.sub(r"\s*\(\d+p\)\s*", "", title)
        clean = re.sub(r"\s*\[[0-9A-Fa-f]{8}\].*$", "", clean).strip()
        clean = re.sub(r"\.mkv$", "", clean, flags=re.I).strip()

        # Last token after " - " is the episode number
        ep_match = re.search(r"^(.+?)\s*-\s*(\d+(?:\.\d+)?)\s*$", clean)
        if ep_match:
            anime_name = ep_match.group(1).strip()
            ep_num     = ep_match.group(2).strip()
        else:
            anime_name = clean
            ep_num     = "1"

        return {
            "anime":   anime_name,
            "ep_num":  ep_num,
            "quality": quality,
            "hash":    file_hash,
        }

    # ─────────────────────────────────────────────
    # Schedule / recent episodes (for ongoing.py)
    # ─────────────────────────────────────────────

    def get_schedule(self) -> list[dict]:
        """
        Fetch today's schedule from SubsPlease API.
        Returns list of {id, title, time, ep}
        """
        try:
            url  = f"{API_BASE}/?f=schedule&h=true&tz=UTC"
            resp = self.session.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                return []
            data  = resp.json()
            today = datetime.now(timezone.utc).strftime("%A").lower()  # e.g. "monday"
            shows = data.get("schedule", {}).get(today, [])
            results = []
            for s in shows:
                results.append({
                    "id":    s.get("page", ""),
                    "title": s.get("title", ""),
                    "time":  s.get("time", ""),
                    "ep":    s.get("episode", None),
                })
            return results
        except Exception as e:
            print(f"[SubsPlease] Schedule fetch error: {e}")
            return []

    def fetch_recently_updated(self, resolutions=("1080", "720")) -> list[dict]:
        """
        Return recently released episodes from the RSS feed,
        in a format compatible with what ongoing.py expects from Animetsu.
        Each item: {id, title, ep_num, aired_at, url}
        """
        results_by_key = {}
        for res in resolutions:
            items = self._fetch_rss(res)
            for item in items:
                parsed = self._parse_rss_title(item["title"])
                # Unique key = anime + ep_num (skip dupes across resolutions)
                key = f"{parsed['anime']}_{parsed['ep_num']}"
                if key not in results_by_key:
                    # Parse pub_date → ms timestamp
                    aired_at = None
                    try:
                        from email.utils import parsedate_to_datetime
                        dt       = parsedate_to_datetime(item["pub_date"])
                        aired_at = int(dt.timestamp() * 1000)
                    except Exception:
                        pass

                    results_by_key[key] = {
                        "id":       key,          # used as unique anime id
                        "title":    parsed["anime"],
                        "ep_num":   parsed["ep_num"],
                        "aired_at": aired_at,
                        "url":      item["magnet"] or item["torrent_url"],
                        "magnet":   item["magnet"],
                        "torrent":  item["torrent_url"],
                        "quality":  parsed["quality"],
                        "_rss_title": item["title"],
                    }

        return list(results_by_key.values())

    def list_episodes(self, anime_id: str) -> list[dict]:
        """
        For SubsPlease, anime_id is a page slug (e.g. 'bleach-thousand-year-blood-war').
        Returns recent episode entries from the RSS that match.
        Compatible with ongoing.py's list_episodes call.
        """
        # Try SubsPlease show-specific RSS: ?f=show&tz=UTC&sid=<slug>
        # Fallback: scan the 1080p RSS for matching titles
        results = []
        try:
            url  = f"{API_BASE}/?f=show&tz=UTC&sid={anime_id}"
            resp = self.session.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # Response: {"tz": ..., "episode": {"1": {...}, "2": {...}, ...}}
                eps = data.get("episode", {})
                for ep_num, ep_data in eps.items():
                    # ep_data has keys like "1080p", "720p", "sd" each with magnet/torrent
                    # Prefer 1080p
                    for res in ("1080p", "720p", "sd"):
                        if res in ep_data:
                            link = ep_data[res]
                            results.append({
                                "title":     f"Episode {ep_num}",
                                "url":       link.get("magnet", link.get("torrent", "")),
                                "ep_number": str(ep_num),
                                "ep_id":     f"{anime_id}_{ep_num}",
                            })
                            break
                if results:
                    return sorted(results, key=lambda x: float(x["ep_number"]))
        except Exception as e:
            print(f"[SubsPlease] list_episodes API error: {e}")

        # Fallback: scan the top-level RSS
        rss = self._fetch_rss("1080") + self._fetch_rss("720")
        for item in rss:
            parsed = self._parse_rss_title(item["title"])
            # Match by slug: compare anime name slugified to anime_id
            slug = re.sub(r"[^a-z0-9]+", "-", parsed["anime"].lower()).strip("-")
            if slug == anime_id or parsed["anime"].lower() in anime_id.lower() or anime_id.lower() in parsed["anime"].lower():
                results.append({
                    "title":     f"Episode {parsed['ep_num']}",
                    "url":       item["magnet"] or item["torrent_url"],
                    "ep_number": parsed["ep_num"],
                    "ep_id":     f"{anime_id}_{parsed['ep_num']}",
                })

        return sorted(results, key=lambda x: float(x.get("ep_number", 0)))

    # ─────────────────────────────────────────────
    # Download via aria2c
    # ─────────────────────────────────────────────

    def download_episode(
        self,
        url: str,
        quality: str = "auto",
        name_override: str = None,
        season_override: str = None,
        ep_num_override: str = None,
        rss_title: str = None,
    ) -> bool:
        """
        Download a SubsPlease episode from a magnet or torrent URL using aria2c.
        Matches the same signature as AnimetsuScraper.download_episode().
        """
        if not self._aria2c:
            if self.progress_queue:
                self.progress_queue.put({
                    "error": "aria2c not installed. Run: apt-get install aria2"
                })
            return False

        # Determine filename from rss_title or overrides
        if rss_title:
            parsed    = self._parse_rss_title(rss_title)
            anime_name = name_override or parsed["anime"]
            ep_num     = ep_num_override or parsed["ep_num"]
            qual_str   = parsed["quality"].replace("p", "")
        else:
            anime_name = name_override or "Unknown"
            ep_num     = ep_num_override or "1"
            qual_str   = "1080"

        season = season_override or "1"

        def sanitize(s):
            return re.sub(r'[\\/*?:"<>|]', "", s)

        try:
            from config import FORMAT
        except ImportError:
            FORMAT = "[S{season}-E{episode}] {title} [{quality}] [{audio}]"

        base_filename = sanitize(FORMAT.format(
            season=season,
            episode=ep_num,
            title=anime_name,
            quality=f"{qual_str}p",
            audio="JP+EN"   # SubsPlease is always dual sub
        ))

        task_dir  = self.download_path / f"sp_{sanitize(anime_name)}_{ep_num}"
        task_dir.mkdir(exist_ok=True)
        final_file = self.download_path / f"{base_filename}.mkv"

        if self.progress_queue:
            self.progress_queue.put({
                "status": f"📥 **Downloading (SubsPlease): {anime_name} [{qual_str}p]**\nPlease wait..."
            })

        # Build aria2c command
        cmd = [
            self._aria2c,
            url,
            "--dir", str(task_dir),
            "--seed-time=0",           # don't seed after download
            "--max-connection-per-server=5",
            "--split=5",
            "--min-split-size=1M",
            "--file-allocation=none",  # faster start on VPS
            "--console-log-level=warn",
            "--summary-interval=5",
            "--on-download-complete=true",
        ]

        # For magnet: bt options
        if url.startswith("magnet:"):
            cmd += [
                "--bt-enable-lpd=true",
                "--bt-max-peers=50",
                "--dht-entry-point=router.bittorrent.com:6881",
                "--dht-entry-point6=router.bittorrent.com:6881",
                "--enable-dht=true",
                "--enable-dht6=false",
            ]

        try:
            print(f"[SubsPlease] Starting aria2c for: {anime_name} E{ep_num}")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue

                # aria2c progress lines look like:
                # [#abc123 100MiB/700MiB(14%) CN:5 DL:5.5MiB ETA:1m42s]
                pct_match   = re.search(r"\((\d+)%\)", line)
                speed_match = re.search(r"DL:([\d.]+[KMGT]iB)", line)
                eta_match   = re.search(r"ETA:([\w]+)", line)
                size_match  = re.search(r"([\d.]+[KMGT]iB)/([\d.]+[KMGT]iB)", line)

                if pct_match and self.progress_queue:
                    self.progress_queue.put({
                        "percent":    f"{pct_match.group(1)}%",
                        "speed":      speed_match.group(1) + "/s" if speed_match else "-- MB/s",
                        "downloaded": size_match.group(1) if size_match else "?",
                        "total":      size_match.group(2) if size_match else "?",
                        "type":       "sub",
                        "title":      f"{anime_name} E{ep_num}",
                    })
                else:
                    print(f"[aria2c] {line}")

            process.wait()
            if process.returncode != 0:
                if self.progress_queue:
                    self.progress_queue.put({"error": f"aria2c exited with code {process.returncode}"})
                return False

        except Exception as e:
            print(f"[SubsPlease] aria2c error: {e}")
            if self.progress_queue:
                self.progress_queue.put({"error": f"aria2c error: {e}"})
            return False

        # Find the downloaded .mkv file in task_dir
        downloaded = list(task_dir.rglob("*.mkv"))
        if not downloaded:
            # Try .mp4 as fallback
            downloaded = list(task_dir.rglob("*.mp4"))
        if not downloaded:
            if self.progress_queue:
                self.progress_queue.put({"error": "Downloaded file not found in task dir."})
            return False

        # Move to final location
        downloaded[0].rename(final_file)
        shutil.rmtree(task_dir, ignore_errors=True)

        if self.progress_queue:
            self.progress_queue.put({
                "finished":  True,
                "filename":  str(final_file),
                "title":     base_filename,
            })
        return True

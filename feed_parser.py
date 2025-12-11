# feed_parser.py
import logging
import asyncio
import random
import feedparser
import httpx
import cloudscraper
import requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from typing import List, Tuple, Dict

import config
from utils import make_async_client

log = logging.getLogger(__name__)

# --- Cloudscraper Helper ---
def fetch_with_cloudscraper(url: str) -> bytes | None:
    """
    Synchronous function to fetch a URL using cloudscraper.
    This should be run in a thread to avoid blocking asyncio.
    """
    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(url, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        log.info(f"Successfully fetched with cloudscraper: {url}")
        return r.content
    except Exception as e:
        log.warning(f"Cloudscraper failed for {url}: {e}")
        return None

# Concurrency & Rate Limiting Helpers
_host_semaphores: Dict[str, asyncio.Semaphore] = {}

def _sem_for(url: str) -> asyncio.Semaphore:
    host = urlparse(url).netloc.lower()
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(config.PER_HOST_CONCURRENCY)
    return _host_semaphores[host]

async def _jitter():
    await asyncio.sleep(random.uniform(config.JITTER_MIN_MS/1000.0, config.JITTER_MAX_MS/1000.0))

def build_headers(url: str) -> Dict[str, str]:
    host = urlparse(url).netloc.lower().replace("www.", "")
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.8,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    }
    # Allow for domain-specific overrides
    domain_headers = config.DOMAIN_CONFIG.get(host, {}).get("headers")
    if domain_headers: 
        headers.update(domain_headers)
    return headers

def get_sources(filename: str) -> List[str]:
    """Reads a list of URLs from a text file."""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except FileNotFoundError:
        log.warning(f"Source file not found: {filename}")
        return []

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str, str, str]]:
    """Fetches and parses a single RSS feed, using cloudscraper for specific domains."""
    posts = []
    content = None
    try:
        async with _sem_for(url):
            await _jitter()
            
            host = urlparse(url).netloc.lower()
            
            if config.SECRETFLYING_HOST in host:
                log.info(f"Using cloudscraper for {url}")
                content = await asyncio.to_thread(fetch_with_cloudscraper, url)
            else:
                r = await client.get(url, headers=build_headers(url))
                if r.status_code == 200:
                    content = r.content
                else:
                    log.warning(f"HTTPX fetch for {url} failed with status code: {r.status_code}")

        if content:
            feed = feedparser.parse(content)
            for entry in feed.entries:
                guid = entry.get("guid", entry.get("link"))
                if entry.get("title") and entry.get("link") and guid:
                    posts.append((entry.title, entry.link, guid, url))
            log.info(f"Fetched {len(posts)} posts from RSS: {url}")
            return posts[:config.MAX_PER_DOMAIN]

    except Exception as e:
        log.warning(f"Error processing RSS feed {url}: {e}", exc_info=True)
        
    return posts

async def scrape_description(client: httpx.AsyncClient, url: str) -> str | None:
    """Scrapes a short description from a given URL."""
    try:
        async with _sem_for(url):
            r = await client.get(url, headers=build_headers(url))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        
        selectors = ['article p', '.entry-content p', '.post-content p', 'main p']
        for sel in selectors:
            p_tag = soup.select_one(sel)
            if p_tag:
                text = p_tag.get_text(separator=' ', strip=True)
                if len(text) > 40:
                    if len(text) > 500:
                        last_space = text.rfind(' ', 0, 500)
                        return text[:last_space] + '...' if last_space != -1 else text[:500] + '...'
                    else:
                        return text
    except Exception as e:
        if config.DEBUG_FEEDS:
            log.info(f"DEBUG: Could not scrape description for {url}: {e}")
    return None

async def process_all_sources() -> List[Dict[str, any]]:
    """
    Fetches all RSS feeds, identifies new posts, and enriches them with descriptions.
    Returns a list of detailed candidates for AI analysis.
    """
    from gcs_state import load_state # Defer import to avoid circular dependency issues at startup

    state, _ = load_state()
    rss_sources = get_sources('rss_sources.txt')
    if not rss_sources:
        log.warning("No sources found in rss_sources.txt. The file is empty or missing.")
        return []

    log.info(f"Loaded {len(rss_sources)} RSS feed(s) to process.")
    all_posts = []
    async with make_async_client() as client:
        tasks = [fetch_feed(client, url) for url in rss_sources]
        results = await asyncio.gather(*tasks)
        for post_list in results:
            if post_list:
                all_posts.extend(post_list)
    
    log.info(f"Total posts collected from all RSS feeds: {len(all_posts)}")
    
    # Filter out already seen posts
    seen_guids = set(state.get("sent_links", {}).keys())
    new_posts = []
    for title, link, guid, source_url in all_posts:
        if guid not in seen_guids:
            new_posts.append((title, link, guid, source_url))

    if config.MAX_POSTS_PER_RUN > 0:
        new_posts = new_posts[:config.MAX_POSTS_PER_RUN]

    if not new_posts:
        log.info("No new posts to process after checking against sent links database.")
        return []
        
    log.info(f"Found {len(new_posts)} new candidates to process. Scraping descriptions...")

    # Enrich new posts with descriptions
    detailed_candidates = []
    async with make_async_client() as client:
        for i, (title, link, dedup_key, source_url) in enumerate(new_posts):
            host = urlparse(link).netloc.lower().replace("www.", "")
            description = None
            if host != config.SECRETFLYING_HOST:
                description = await scrape_description(client, link)
            
            detailed_candidates.append({
                "id": i,
                "title": title,
                "link": link,
                "dedup_key": dedup_key,
                "source_url": source_url,
                "description": description,
                "host": host,
                "source_name": host
            })
            
    return detailed_candidates

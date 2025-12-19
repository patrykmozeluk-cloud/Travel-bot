# feed_parser.py
import logging
import asyncio
import random
import time
import feedparser
import httpx
from curl_cffi import requests as cffi_requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from typing import List, Tuple, Dict

import config
from utils import make_async_client

log = logging.getLogger(__name__)

# --- Nuclear Option for Google News ---
def fetch_with_cffi(url: str):
    """
    Uses curl_cffi to impersonate a real browser's TLS fingerprint.
    Crucial for Google News RSS which blocks standard Python http clients (httpx/requests)
    running from Cloud IPs.
    """
    try:
        # Jitter to look more human
        time.sleep(random.uniform(0.5, 1.5))

        log.info(f"🚀 Launching curl_cffi (Chrome impersonation) for: {url}")
        
        response = cffi_requests.get(
            url, 
            impersonate="chrome120", 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
            },
            timeout=30
        )
        
        if response.status_code == 200:
            log.info(f"✅ SUCCESS! {url} breached via curl_cffi.")
            return response.content
        else:
            log.error(f"❌ curl_cffi failed for {url} with status: {response.status_code}")
            return None

    except Exception as e:
        log.error(f"❌ Error in curl_cffi: {e}", exc_info=True)
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
        "User-Agent": random.choice(config.CHROME_USER_AGENTS),
    }
    # Allow for domain-specific overrides
    domain_headers = config.DOMAIN_CONFIG.get(host, {}).get("headers")
    
    # Specific fix for Google News RSS to avoid 503
    if "news.google.com" in host or "google.com" in host:
         headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
         headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8"
         # Google News often prefers cookies or specific accept-language to serve content correctly
         headers["Accept-Language"] = "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"

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
    """Fetches and parses a single RSS feed, using curl_cffi for specific domains."""
    posts = []
    content = None
    try:
        async with _sem_for(url):
            await _jitter()
            
            host = urlparse(url).netloc.lower()
            
            # Special routing for Google News (requires TLS impersonation)
            if "news.google.com" in host or "google.com" in host:
                content = await asyncio.to_thread(fetch_with_cffi, url)
            else:
                # Standard fetch for friendly RSS feeds
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
            await _jitter() # Add jitter to scraping requests
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
    This version uses conditional proxying: some domains go through a proxy, others go direct.
    """
    from gcs_state import load_state # Defer import to avoid circular dependency issues at startup

    state, _ = load_state()
    rss_sources = get_sources('rss_sources.txt')
    if not rss_sources:
        log.warning("No sources found in rss_sources.txt. The file is empty or missing.")
        return []

    log.info(f"Loaded {len(rss_sources)} RSS feed(s) to process.")
    
    all_posts = []
    direct_client_config = {
        "timeout": config.HTTP_TIMEOUT,
        "follow_redirects": True,
        "http2": True # Can be enabled for direct connections
    }

    # Create two clients: one with proxy, one without.
    async with make_async_client() as proxied_client, httpx.AsyncClient(**direct_client_config) as direct_client:
        
        # --- 1. Fetch all RSS feeds ---
        tasks = []
        for url in rss_sources:
            host = urlparse(url).netloc.lower()
            use_proxy = any(proxy_host in host for proxy_host in config.PROXY_REQUIRED_HOSTS)
            client_to_use = proxied_client if use_proxy else direct_client
            if use_proxy:
                log.info(f"Routing feed fetch for {host} via proxy.")
            tasks.append(fetch_feed(client_to_use, url))
        
        results = await asyncio.gather(*tasks)
        for post_list in results:
            if post_list:
                all_posts.extend(post_list)

        log.info(f"Total posts collected from all RSS feeds: {len(all_posts)}")

        # --- 2. Filter out already seen posts ---
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

        # --- 3. Enrich new posts with descriptions ---
        detailed_candidates = []
        for i, (title, link, dedup_key, source_url) in enumerate(new_posts):
            host = urlparse(link).netloc.lower().replace("www.", "")
            
            # Decide which client to use for scraping the article link
            use_proxy = any(proxy_host in host for proxy_host in config.PROXY_REQUIRED_HOSTS)
            client_to_use = proxied_client if use_proxy else direct_client
            if use_proxy:
                log.info(f"Routing description scrape for {host} via proxy.")
            description = await scrape_description(client_to_use, link)
            
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

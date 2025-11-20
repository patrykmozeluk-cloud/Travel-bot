# Final Hybrid Bot Code v6.0 (Refactored & Simplified)
import os
import logging
import asyncio
import httpx
import feedparser
import orjson
import json # For safe JSON parsing of AI responses
import time
import random
import html
from flask import Flask, request, jsonify
from google.cloud import storage 
import google.generativeai as genai # For Gemini AI integration
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ---------- APP / GCS ----------
app = Flask(__name__)
storage_client = storage.Client()
# Usunięto vision_client

# ---------- ENV ----------
def env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)

TG_TOKEN = env("TG_TOKEN")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID") # Renamed from TG_CHAT_ID
TELEGRAM_CHAT_GROUP_ID = env("TELEGRAM_CHAT_GROUP_ID") # New for AI-selected posts
TELEGRAM_CHANNEL_USERNAME = env("TELEGRAM_CHANNEL_USERNAME") # New for linking to channel messages
GEMINI_API_KEY = env("GEMINI_API_KEY") # New for Gemini AI
BUCKET_NAME = env("BUCKET_NAME")
SENT_LINKS_FILE = env("SENT_LINKS_FILE", "sent_links.json")
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "15.0"))
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1", "true", "True", "yes", "YES"}
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))

DELETE_AFTER_HOURS = int(env("DELETE_AFTER_HOURS", "48"))
DEDUP_TTL_HOURS = int(env("DEDUP_TTL_HOURS", "336"))

MAX_PER_DOMAIN = int(env("MAX_PER_DOMAIN", "8"))
PER_HOST_CONCURRENCY = int(env("PER_HOST_CONCURRENCY", "2"))
JITTER_MIN_MS = int(env("JITTER_MIN_MS", "120"))
JITTER_MAX_MS = int(env("JITTER_MAX_MS", "400"))

SECRETFLYING_HOST = "secretflying.com"

# ---------- GEMINI AI CONFIGURATION ----------
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(
        'gemini-2.5-flash',
        generation_config={"response_mime_type": "application/json"}
    )
    log.info("Gemini AI model configured.")
else:
    gemini_model = None
    log.warning("GEMINI_API_KEY not set. AI analysis will be disabled.")

# NOWA SEKACJA: EMOTIKONY (Bez zmian)
EMOJI_KEYWORDS = {
    '🇬🇧': ['londyn', 'london', 'anglia', 'uk', 'brytanii'],
    '🇪🇸': ['hiszpanii', 'spain', 'barcelona', 'madryt', 'madrid', 'majorka', 'mallorca'],
    '🇮🇹': ['włochy', 'italy', 'rzym', 'rome', 'mediolan', 'milan'],
    '🇫🇷': ['francja', 'france', 'paryż', 'paris'],
    '🇩🇪': ['niemcy', 'germany', 'berlin'],
    '🇵🇹': ['portugalia', 'portugal', 'lizbona', 'lisbon'],
    '🇺🇸': ['usa', 'stany', 'york', 'chicago', 'miami'],
    '🇦🇪': ['dubaj', 'dubai', 'emiraty', 'emirates'],
    '🇯🇵': ['japonia', 'japan', 'tokio', 'tokyo'],
    '🇹🇭': ['tajlandia', 'thailand', 'bangkok'],
    '🏖️': ['plaża', 'beach', 'wakacje', 'holiday', 'morze', 'sea', 'wyspy', 'islands'],
    '✈️': ['loty', 'flights', 'lot', 'flight'],
    '🏨': ['hotel', 'nocleg'],
    '💰': ['okazja', 'deal', 'tanio', 'cheap', 'promocja'],
}

def dbg(msg: str):
    if DEBUG_FEEDS: log.info(f"DEBUG {msg}")

# ---------- GCS STATE MANAGEMENT (Bez zmian) ----------
_bucket = storage_client.bucket(BUCKET_NAME) if BUCKET_NAME else None
_blob = _bucket.blob(SENT_LINKS_FILE) if _bucket else None

DROP_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "fbclid","gclid","igshid","mc_cid","mc_eid","ref","ref_src","src"
}

def canonicalize_url(url: str) -> str:
    try:
        u = unquote(url.strip())
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower().replace("www.", "")
        path = p.path or "/"
        if path != "/" and path.endswith("/"): path = path[:-1]
        q = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS])
        return urlunparse((scheme, netloc, path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url.strip()

def _default_state() -> Dict[str, Any]:
    return {"sent_links": {}, "delete_queue": []}

def _ensure_state_shapes(state: Dict[str, Any]):
    if "sent_links" not in state: state["sent_links"] = {}
    if "delete_queue" not in state: state["delete_queue"] = []

def load_state() -> Tuple[Dict[str, Any], int | None]:
    if not _blob: return (_default_state(), None)
    try:
        if not _blob.exists(): return _default_state(), None
        _blob.reload()
        state_data = orjson.loads(_blob.download_as_bytes())
        _ensure_state_shapes(state_data)
        return state_data, _blob.generation
    except Exception as e:
        log.warning(f"load_state fallback: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], gen: int | None):
    if not _blob: return
    payload = orjson.dumps(state)
    for _ in range(10):
        try:
            _blob.upload_from_string(payload, if_generation_match=gen or 0, content_type="application/json")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                log.warning("State save conflict, retrying...")
                time.sleep(random.uniform(0.3, 0.8)); _, gen = load_state()
                continue
            raise
    raise RuntimeError("Atomic state save failed.")

# ---------- DOMAIN-SPECIFIC CONFIG & HTTP CLIENT (Bez zmian) ----------
DOMAIN_CONFIG: Dict[str, Dict[str, Any]] = {
    "travel-dealz.com": { 
        "selectors": ['article.article-item h2 a', 'article.article h2 a'],
        "headers": { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" } 
    },
    "secretflying.com": { "selectors": ['article.post-item .post-title a', 'article h2 a'], "rss": ["https://www.secretflying.com/feed/"] },
    "wakacyjnipiraci.pl": { "selectors": ['article.post-list__item a.post-list__link'], "rss": ["https://www.wakacyjnipiraci.pl/feed"], "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" } },
    "holidaypirates.com": { "selectors": ['article.post-list__item a.post-list__link'], "rss": ["https://www.holidaypirates.com/feed"], "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" } },
    "theflightdeal.com": { "selectors": ['article h2 a', '.entry-title a'], "rss": ["https://www.theflightdeal.com/feed/"] },
    "travelfree.info": { "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7" } },
    "fly4free.pl": { "rss": ["https://www.fly4free.pl/feed/"] },
    "loter.pl": { "selectors": ['article h2 a', 'article h3 a'] }
}
GENERIC_FALLBACK_SELECTORS = ['article h2 a', 'article h3 a', 'h2 a', 'h3 a']
BASE_HEADERS = {"Accept-Encoding": "gzip, deflate", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}

def build_headers(url: str) -> Dict[str, str]:
    host = urlparse(url).netloc.lower().replace("www.", "")
    headers = BASE_HEADERS.copy()
    domain_headers = DOMAIN_CONFIG.get(host, {}).get("headers")
    if domain_headers: headers.update(domain_headers)
    return headers

def make_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, http2=True)

def get_sources(filename: str) -> List[str]:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except FileNotFoundError:
        log.warning(f"Source file not found: {filename}")
        return []

# ---------- CONCURRENCY HELPERS (Bez zmian) ----------
_host_semaphores: Dict[str, asyncio.Semaphore] = {}
def _sem_for(url: str) -> asyncio.Semaphore:
    host = urlparse(url).netloc.lower()
    if host not in _host_semaphores: _host_semaphores[host] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    return _host_semaphores[host]

async def _jitter():
    await asyncio.sleep(random.uniform(JITTER_MIN_MS/1000.0, JITTER_MAX_MS/1000.0))

# ---------- NOWE FUNKCJE POMOCNICZE (INTELIGENCJA) ----------
# Funkcja shorten_link usunięta zgodnie z prośbą użytkownika.

# ########## ZBĘDNE FUNKCJE USUNIĘTE ##########
# Usunięto: is_image_safe
# Usunięto: _run_face_detection_sync
# Usunięto: find_safe_image_url
# ###########################################

async def scrape_description(client: httpx.AsyncClient, url: str) -> str | None:
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
                    if len(text) > 200:
                        # Find the last space within the first 200 characters
                        last_space = text.rfind(' ', 0, 200)
                        if last_space != -1:
                            # Truncate at the last space
                            return text[:last_space] + '...'
                        else:
                            # No space found, just truncate at 200 (fallback)
                            return text[:200] + '...'
                    else:
                        return text
    except Exception as e:
        dbg(f"Could not scrape description for {url}: {e}")
    return None

def add_emojis(text: str) -> str:
    found_emojis = []
    text_lower = text.lower()
    for emoji, keywords in EMOJI_KEYWORDS.items():
        if any(keyword in text_lower for keyword in keywords):
            found_emojis.append(emoji)
    return ' '.join(found_emojis) + ' ' + text if found_emojis else text

def add_emojis(text: str) -> str:
    found_emojis = []
    text_lower = text.lower()
    for emoji, keywords in EMOJI_KEYWORDS.items():
        if any(keyword in text_lower for keyword in keywords):
            found_emojis.append(emoji)
    return ' '.join(found_emojis) + ' ' + text if found_emojis else text

async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not gemini_model:
        log.error("Gemini AI model not initialized. Skipping AI analysis.")
        return []

    system_prompt = f"""
Jesteś Globalnym Ekspertem Rynku Lotniczego i Turystycznego. Twoim zadaniem jest analiza listy ofert RSS i zwrócenie listy wyników w formacie JSON.

Przetwórz KAŻDĄ ofertę z poniższej listy. Dla każdej oferty wykonaj następujące kroki:

KROK 1: KONTEKST ŹRÓDŁA (Dostosuj perspektywę):
    • 'The Flight Deal' (i inne z USA jak 'theflightdeal.com'): Rynek USA. Waluta USD. Loty wewnątrz USA lub z USA są atrakcyjne. Nie obniżaj oceny za wylot z Ameryki.
    • 'Fly4Free' (i inne PL/EU jak 'fly4free.pl', 'wakacyjnipiraci.pl', 'travel-dealz.com'): Rynek Europejski (szczególnie Polska). Waluta PLN/EUR. Priorytet: Polska + Huby (Berlin, Praga, Wiedeń, Londyn, Sztokholm - tani dolot).
    • 'Travel Dealz' (lub wzmianka o 'Business Class' w tytule/opisie): Rynek Premium. Oczekuj wysokich cen (np. 5000 PLN). Jeśli to Biznes Klasa - oceniaj jako okazję, nie jako drożyznę.
    • Dla wszystkich innych źródeł: Ocena globalna.

KROK 2: OCENA (1-10):
    • 9-10: Mega Hit, Error Fare, Biznes w cenie Economy, Ważny News (strajki, wizy, zmiany w przepisach).
    • 7-8: Dobra, solidna oferta.
    • 1-6: Przeciętna cena, reklama, spam. (ODRZUĆ, is_good: false).

KROK 3: GENEROWANIE TREŚCI (Dwa Warianty):
    • 'channel_msg': Styl dziennikarski, informacyjny, konkretne daty, ceny, miejsca docelowe, emoji. Max 200 znaków.
    • 'chat_msg': Styl luźny, "vibe coding", pytanie angażujące społeczność (np. "Kto leci?", "Kto się skusi?"), emoji. Max 150 znaków.
    
    W obu wiadomościach (channel_msg i chat_msg) unikaj umieszczania linku do oferty. Link zostanie dodany automatycznie przez bota.

KROK 4: SELEKCJA NA CZAT:
    • Ustaw 'post_to_chat': true TYLKO dla ocen 9-10 (Hity) lub Ważnych Newsów (np. o strajkach, zmianach wizowych). Nie chcemy spamu na czacie.

Twoja odpowiedź MUSI być pojedynczym obiektem JSON, zawierającym klucz "results", który jest listą obiektów. Każdy obiekt w liście musi odpowiadać jednej ofercie z wejścia i zawierać jej oryginalne "id".

Format odpowiedzi:
{{
  "results": [
    {{ "id": 0, "score": int, "is_good": bool, "post_to_chat": bool, "channel_msg": str, "chat_msg": str }},
    {{ "id": 1, "score": int, "is_good": bool, "post_to_chat": bool, "channel_msg": str, "chat_msg": str }}
  ]
}}
"""
    
    batch_prompt_parts = []
    for candidate in candidates:
        batch_prompt_parts.append(
            f"OFERTA ID: {candidate['id']}\n"
            f"Źródło: {candidate['source_name']}\n"
            f"Tytuł: {candidate['title']}\n"
            f"Opis: {candidate['description'] or 'Brak opisu.'}"
        )
    
    user_message = "Przeanalizuj poniższe oferty:\n\n---\n".join(batch_prompt_parts)

    try:
        log.info(f"Sending a batch of {len(candidates)} candidates to Gemini AI.")
        response = await gemini_model.generate_content_async([system_prompt, user_message])
        
        if not response.text:
            log.warning("Gemini API returned empty response for batch.")
            return []

        try:
            ai_results_wrapper = json.loads(response.text)
            ai_results = ai_results_wrapper.get("results", [])
            
            if not isinstance(ai_results, list):
                log.error(f"Gemini API returned 'results' that is not a list: {ai_results}")
                return []
            
            log.info(f"AI processed batch and returned {len(ai_results)} results.")
            return ai_results

        except (json.JSONDecodeError, KeyError):
            log.error(f"Gemini API returned invalid JSON or missing 'results' key for batch: {response.text[:200]}")
            return []

    except Exception as e:
        log.error(f"Error calling Gemini API for batch: {e}")
        return []

# ---------- PRZEBUDOWANA LOGIKA WYSYŁANIA ----------
async def send_telegram_message_async(message_content: str, link: str, host: str, chat_id: str) -> int | None:
    async with make_async_client() as client:
        try:
            safe_text = html.escape(message_content, quote=False)

            # UPROSZCZENIE: Usunięto logikę warunkową dla PIRATES_HOSTS.
            # Teraz każda wiadomość jest wysyłana w ten sam, spójny sposób.
            text = f"{safe_text}\n\n<a href='{link}'>👉 Zobacz ofertę</a>"
            payload, method = {"text": text, "disable_web_page_preview": False}, "sendMessage"

            payload.update({"chat_id": chat_id, "parse_mode": "HTML"})
            url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Message sent: {message_content[:60]}… (method={method})")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false: {body}")
        except Exception as e:
            log.error(f"Telegram send error for {link}: {e}")
    return None

# ---------- ORYGINALNE FUNKCJE (Bez zmian) ----------
def remember_for_deletion(state: Dict[str, Any], chat_id: str, message_id: int, source_url: str):
    log.info(f"DEBUG: remember_for_deletion called. Value of DELETE_AFTER_HOURS: {DELETE_AFTER_HOURS}")
    delete_at = (datetime.now(timezone.utc) + timedelta(hours=DELETE_AFTER_HOURS)).replace(minute=0, second=0, microsecond=0)
    state["delete_queue"].append({ "chat_id": str(chat_id), "message_id": int(message_id), "delete_at": delete_at.isoformat(), "source_url": source_url })

async def sweep_delete_queue(state: Dict[str, Any]) -> int:
    """Przetwarza kolejkę do usunięcia, modyfikując podany obiekt stanu w sposób odporny na błędy."""
    if not state.get("delete_queue"):
        return 0

    now = datetime.now(timezone.utc)
    
    keep_for_later, process_now = [], []
    for item in state["delete_queue"]:
        try:
            if datetime.fromisoformat(item["delete_at"]) > now:
                keep_for_later.append(item)
            else:
                process_now.append(item)
        except (ValueError, TypeError):
            log.warning(f"Skipping malformed item in delete_queue: {item}")
            continue

    if not process_now:
        return 0

    # Nowe, bardziej szczegółowe liczniki
    actually_deleted_count = 0
    cleaned_from_queue_count = 0
    
    final_queue = keep_for_later.copy()

    async with make_async_client() as client:
        tasks = []
        for item in process_now:
            log.info(f'Attempting to delete message ID: {item["message_id"]} from source: {item.get("source_url", "Unknown")}')
            url = f"https://api.telegram.org/bot{TG_TOKEN}/deleteMessage"
            tasks.append(client.post(url, json={"chat_id": item["chat_id"], "message_id": item["message_id"]}))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(results):
            item = process_now[i]
            item_id = item["message_id"]

            if isinstance(res, Exception):
                final_queue.append(item)
                log.error(f"Network/HTTP error for message {item_id}. Will retry. Error: {res}")
                continue

            if res.status_code == 200:
                actually_deleted_count += 1
                log.info(f"SUCCESS: Message {item_id} deleted successfully from Telegram (200 OK).")
                continue

            if res.status_code in [400, 403]:
                try:
                    response_data = res.json()
                    description = response_data.get("description", "").lower()
                except Exception:
                    description = res.text.lower()

                if "message to delete not found" in description:
                    # Scenariusz 1: Wiadomość faktycznie nie istnieje (sukces)
                    cleaned_from_queue_count += 1
                    log.info(f"Wiadomość {item_id} już nie istniała. Uznaję za posprzątane i usuwam z kolejki.")
                
                elif "message is too old to be deleted" in description or "message can't be deleted" in description:
                    # Scenariusz 2: Wiadomość jest ZA STARA (problem, ale sprzątamy)
                    cleaned_from_queue_count += 1
                    log.warning(f"Nie można usunąć wiadomości {item_id}, była za stara (limit 48h). Mimo to usuwam z kolejki.")

                else:
                    # Inny, nieoczekiwany błąd API - ponów próbę
                    final_queue.append(item)
                    log.error(f"Nie udało się usunąć wiadomości {item_id} z powodu błędu API: {res.status_code} {description}. Zostawiam do ponownej próby.")
                continue
            
            final_queue.append(item)
            log.error(f"Server-side error for message {item_id}. Will retry. Status: {res.status_code}, Response: {res.text}")

    total_processed = actually_deleted_count + cleaned_from_queue_count
    items_to_retry = len(process_now) - total_processed

    if total_processed > 0:
        state["delete_queue"] = final_queue
        # Nowe, czytelne podsumowanie
        log.info(f"--- Sweep Job Summary ---")
        log.info(f"Successfully deleted from Telegram: {actually_deleted_count}")
        log.info(f"Cleaned from queue (old/not found): {cleaned_from_queue_count}")
        log.info(f"Kept for future retry: {items_to_retry}")
        log.info(f"Final queue size: {len(final_queue)}")
        log.info(f"-----------------------")

    return total_processed

def prune_sent_links(state: Dict[str, Any]):
    if DEDUP_TTL_HOURS <= 0: return
    prune_before = datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HOURS)
    original_count = len(state["sent_links"])
    try:
        pruned_links = {link: ts for link, ts in state["sent_links"].items() if datetime.fromisoformat(ts) >= prune_before}
        if len(pruned_links) < original_count:
            log.info(f"Pruned {original_count - len(pruned_links)} old links from state.")
            state["sent_links"] = pruned_links
    except (ValueError, TypeError):
        log.warning("Could not prune links due to malformed timestamp.")

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str, str, str]]:
    posts = []
    try:
        async with _sem_for(url):
            await _jitter()
            r = await client.get(url, headers=build_headers(url))
        if r.status_code == 200:
            feed = feedparser.parse(r.content)
            for entry in feed.entries:
                # Use GUID for uniqueness, fallback to link. GUID is often more stable.
                guid = entry.get("guid", entry.get("link"))
                if entry.get("title") and entry.get("link") and guid:
                    posts.append((entry.title, entry.link, guid, url))
            log.info(f"Fetched {len(posts)} posts from RSS: {url}")
            return posts[:MAX_PER_DOMAIN]
    except Exception as e: log.warning(f"Error fetching RSS {url}: {e}", exc_info=True)
    return posts

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    host = urlparse(url).netloc.lower().replace("www.", "")
    selectors = DOMAIN_CONFIG.get(host, {}).get("selectors", []) + GENERIC_FALLBACK_SELECTORS
    try:
        async with _sem_for(url):
            await _jitter()
            r = await client.get(url, headers=build_headers(url))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        posts = []
        for sel in selectors:
            for tag in soup.select(sel):
                href, title = tag.get('href', '').strip(), tag.get_text(strip=True)
                if href.startswith("http") and title: posts.append((title, href))
            if posts: return posts[:MAX_PER_DOMAIN]
    except Exception as e: dbg(f"Scrape failed for {url}: {e}")
    return []

# ---------- GŁÓWNA LOGIKA (Używamy ostatniej, prostej wersji) ----------
async def process_sources_async() -> str:
    log.info("Starting a simple RSS-only processing run...")

    if not TG_TOKEN or not TELEGRAM_CHANNEL_ID: return "Missing critical environment variables."
    state, generation = load_state()

    # Zintegrowane czyszczenie (sweep) na początku wykonania
    log.info("Running the integrated sweep job at the start of the main run...")
    try:
        deleted_count = await sweep_delete_queue(state) # Przekazanie stanu
        log.info(f"In-process sweep finished. {deleted_count} messages processed in queue.")
    except Exception as e:
        log.error(f"In-process sweep failed: {e}")
    rss_sources = get_sources('rss_sources.txt')
    if not rss_sources: return "No sources found in rss_sources.txt. The file is empty or missing."
    log.info(f"Loaded {len(rss_sources)} RSS feed(s) to process.")
    all_posts = []
    async with make_async_client() as client:
        tasks = []
        for url in rss_sources:
            tasks.append(fetch_feed(client, url))
        results = await asyncio.gather(*tasks)
        for post_list in results:
            if post_list: all_posts.extend(post_list)
    log.info(f"Total posts collected from all RSS feeds: {len(all_posts)}")
    
    candidates = []
    seen_guids = set(state.get("sent_links", {}).keys())
    log.info(f"Checking {len(all_posts)} posts against {len(seen_guids)} previously sent links (using GUIDs).")

    for title, link, guid, source_url in all_posts:
        dedup_key = guid
        if dedup_key not in seen_guids:
            candidates.append((title, link, dedup_key, source_url))
            # No need to add to seen_guids here, as the concurrent tasks won't share this live list

    if MAX_POSTS_PER_RUN > 0: candidates = candidates[:MAX_POSTS_PER_RUN]
    
    if not candidates:
        log.info("No new posts to send. (All posts were duplicates or no posts were found).")
        # Save state to prune old links even if no new posts are sent
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after pruning: {e}")
        return "Run complete. No new posts."

    log.info(f"Found {len(candidates)} new candidates to process in batches.")
    
    # Prepare candidates with descriptions and IDs for the AI
    detailed_candidates = []
    async with make_async_client() as client:
        for i, (title, link, dedup_key, source_url) in enumerate(candidates):
            host = urlparse(link).netloc.lower().replace("www.", "")
            description = None
            if host != SECRETFLYING_HOST:
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

    # --- Batch Processing ---
    BATCH_SIZE = 15
    candidate_chunks = [detailed_candidates[i:i + BATCH_SIZE] for i in range(0, len(detailed_candidates), BATCH_SIZE)]
    
    all_ai_results = []
    for chunk in candidate_chunks:
        results = await analyze_batch(chunk)
        all_ai_results.extend(results)
        if len(candidate_chunks) > 1:
            log.info(f"Processed a chunk, waiting 1s before next batch to be safe.")
            await asyncio.sleep(1)

    if not all_ai_results:
        log.warning("AI analysis returned no results for any batch.")
        # Save state to prune old links even if no posts are sent
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after empty AI result: {e}")
        return "Run complete. AI analysis yielded no results."
    
    # Create a mapping from ID to original candidate data
    candidates_by_id = {c['id']: c for c in detailed_candidates}

    sent_count = 0
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    
    for ai_result in all_ai_results:
        result_id = ai_result.get("id")
        if result_id is None:
            continue

        original_candidate = candidates_by_id.get(result_id)
        if not original_candidate:
            log.warning(f"AI returned a result with ID {result_id} that does not match any original candidate.")
            continue
            
        # Ensure is_good is correctly set based on score (AI might sometimes err)
        is_good_flag = ai_result.get("is_good", False)
        if ai_result.get("score", 0) < 7:
            is_good_flag = False

        if not is_good_flag:
            log.info(f"AI deemed '{original_candidate['title'][:50]}...' not good (score: {ai_result.get('score', 'N/A')}). Skipping.")
            continue

        # --- Send to Channel ---
        channel_message_id = await send_telegram_message_async(
            message_content=ai_result.get("channel_msg") or original_candidate['title'],
            link=original_candidate['link'],
            host=original_candidate['host'],
            chat_id=TELEGRAM_CHANNEL_ID
        )
        
        if channel_message_id:
            sent_count += 1
            state["sent_links"][original_candidate['dedup_key']] = now_utc_iso
            if DELETE_AFTER_HOURS > 0:
                remember_for_deletion(state, TELEGRAM_CHANNEL_ID, channel_message_id, original_candidate['source_url'])
            
            log.info(f"Channel message for '{original_candidate['title'][:50]}...' sent and recorded.")

            # --- Conditional Send to Chat Group ---
            post_to_chat_flag = ai_result.get("post_to_chat", False)
            if ai_result.get("score", 0) < 9: # Enforce rule: only 9+ for chat
                 post_to_chat_flag = False

            if post_to_chat_flag:
                chat_text = ai_result.get("chat_msg") or f"Nowa super oferta: {original_candidate['title'][:50]}..."
                
                # 30% chance to add "Więcej na kanale" link
                if random.random() < 0.3 and TELEGRAM_CHANNEL_USERNAME:
                    channel_link = f"https://t.me/{TELEGRAM_CHANNEL_USERNAME.replace('@', '')}/{channel_message_id}"
                    chat_text += f"\n\n👉 Więcej na kanale: {channel_link}"
                
                log.info(f"Attempting to send chat message for '{original_candidate['title'][:50]}...'.")
                await send_telegram_message_async(
                    message_content=chat_text,
                    link=original_candidate['link'],
                    host=original_candidate['host'],
                    chat_id=TELEGRAM_CHAT_GROUP_ID
                )
        else:
            log.warning(f"Failed to send channel message for '{original_candidate['title'][:50]}...'. Not processing for chat either.")
        
        # Add a small, random delay between each Telegram message to avoid hitting Telegram's own limits
        await asyncio.sleep(random.uniform(0.2, 0.5))

    if sent_count > 0:
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info(f"Successfully saved state for {sent_count} new items.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED: {e}")
            return "Critical: State save failed."
            
    return f"Run complete. Found {len(all_posts)} posts, sent {sent_count} new messages."


# ---------- FLASK ROUTES (Bez zmian) ----------
@app.route("/")
def index():
    return "Travel-Bot v6.0 Refactored is running.", 200

@app.route("/run", methods=['POST'])
def run_now():
    try:
        result = asyncio.run(process_sources_async())
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        log.exception("Error in /run endpoint")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/sweep", methods=['POST'])
def handle_sweep():
    auth_header = request.headers.get("X-Bot-Secret-Token")
    if not TELEGRAM_SECRET or auth_header != TELEGRAM_SECRET:
        return "Unauthorized", 401
    
    # FIX: Wczytaj stan przed wywołaniem sweep i zapisz go po
    state, generation = load_state()
    deleted_count = asyncio.run(sweep_delete_queue(state))
    try:
        # Zapis atomowy jest ważny, jeśli sweep zmodyfikował stan
        save_state_atomic(state, generation) 
        log.info("Stan zapisany po ręcznym zadaniu sweep.")
    except Exception as e:
        log.error(f"Nie udało się zapisać stanu po ręcznym sweep: {e}")

    log.info(f"Ręczne zadanie sweep zakończone. Przetworzono {deleted_count} wiadomości.")
    return jsonify({"status": "ok", "processed_count": deleted_count}), 200

if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
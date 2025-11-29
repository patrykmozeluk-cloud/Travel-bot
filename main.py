# Final Hybrid Bot Code v6.0 (Refactored & Simplified)
import os
import logging
import asyncio
import httpx
import feedparser
import json
import time
import random
import html
from flask import Flask, request, jsonify
from google.cloud import storage 
import google.generativeai as genai
from telegraph import Telegraph
from google.generativeai.types import HarmCategory, HarmBlockThreshold
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

# ---------- ENV ----------
def env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)

TG_TOKEN = env("TG_TOKEN")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID")
TELEGRAM_CHAT_GROUP_ID = env("TELEGRAM_CHAT_GROUP_ID")
TELEGRAM_CHANNEL_USERNAME = env("TELEGRAM_CHANNEL_USERNAME")
CHAT_CHANNEL_URL = env("CHAT_CHANNEL_URL")
TELEGRAPH_TOKEN = env("TELEGRAPH_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY")
PERPLEXITY_API_KEY = env("PERPLEXITY_API_KEY")
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

# ---------- DIGEST IMAGES (USER-PROVIDED) ----------
DIGEST_IMAGE_URLS = [
    "https://images.unsplash.com/photo-1516483638261-f4dbaf036963?q=80&w=2800&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D",
    "https://images.pexels.com/photos/3408744/pexels-photo-3408744.jpeg?auto=compress&cs=tinysrgb&w=1260&h=750&dpr=2",
    "https://cdn.pixabay.com/photo/2017/01/20/00/30/maldives-1993704_1280.jpg"
]

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

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

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
    return {"sent_links": {}, "delete_queue": [], "last_social_post_time": "1970-01-01T00:00:00Z", "last_ai_analysis_time": "1970-01-01T00:00:00Z", "digest_candidates": []}

def _ensure_state_shapes(state: Dict[str, Any]):
    if "sent_links" not in state: state["sent_links"] = {}
    if "delete_queue" not in state: state["delete_queue"] = []
    if "last_social_post_time" not in state: state["last_social_post_time"] = "1970-01-01T00:00:00Z"
    if "last_ai_analysis_time" not in state: state["last_ai_analysis_time"] = "1970-01-01T00:00:00Z"
    if "digest_candidates" not in state: state["digest_candidates"] = []

def load_state() -> Tuple[Dict[str, Any], int | None]:
    if not _blob: return (_default_state(), None)
    try:
        if not _blob.exists(): return _default_state(), None
        _blob.reload()
        state_data = json.loads(_blob.download_as_bytes())
        _ensure_state_shapes(state_data)
        return state_data, _blob.generation
    except Exception as e:
        log.warning(f"load_state fallback: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], gen: int | None):
    if not _blob: return
    payload = json.dumps(state).encode('utf-8')
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

def sanitizing_startup_check(state: Dict[str, Any]) -> int:
    """
    Sprawdza i naprawia kolejkę 'delete_queue' pod kątem uszkodzonych wpisów chat_id.
    Jest to jednorazowa funkcja naprawcza uruchamiana przy starcie.
    Zwraca liczbę naprawionych wpisów.
    """
    if "delete_queue" not in state or not isinstance(state.get("delete_queue"), list):
        return 0

    fixed_entries_count = 0
    sanitized_queue = []
    
    import re
    id_pattern = re.compile(r"^(-?\d+)")

    for item in state.get("delete_queue", []):
        if not isinstance(item, dict) or "chat_id" not in item:
            sanitized_queue.append(item)
            continue

        chat_id = item["chat_id"]
        
        if isinstance(chat_id, str) and ' ' in chat_id:
            original_id = chat_id
            match = id_pattern.match(original_id)
            if match:
                clean_id = match.group(1)
                item["chat_id"] = clean_id
                fixed_entries_count += 1
                log.info(f"Sanitized chat_id: '{original_id}' -> '{clean_id}'")
            else:
                log.warning(f"Could not sanitize chat_id '{original_id}'. Keeping original but this is an error.")
        
        sanitized_queue.append(item)

    if fixed_entries_count > 0:
        state["delete_queue"] = sanitized_queue
        log.info(f"SANITIZING COMPLETE: Repaired {fixed_entries_count} entries in the delete_queue.")

    return fixed_entries_count


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
                    if len(text) > 500:
                        last_space = text.rfind(' ', 0, 500)
                        if last_space != -1:
                            return text[:last_space] + '...'
                        else:
                            return text[:500] + '...'
                    else:
                        return text
    except Exception as e:
        dbg(f"Could not scrape description for {url}: {e}")
    return None



async def gemini_api_call_with_retry(model, prompt_parts, max_retries=4):
    """
    Calls the Gemini API with exponential backoff retry mechanism.
    Handles 429 (Too Many Requests) and 503 (Service Unavailable) errors.
    """
    if not model:
        log.error("Gemini model not provided to retry function.")
        return None

    for attempt in range(max_retries):
        try:
            response = await model.generate_content_async(
                prompt_parts,
                safety_settings=SAFETY_SETTINGS
            )
            return response
        except Exception as e:
            error_str = str(e).lower()
            if ("429" in error_str and "resource has been exhausted" in error_str) or "503" in error_str or "service unavailable" in error_str:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    log.warning(f"Rate limit hit or service unavailable on attempt {attempt + 1}/{max_retries}. Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    log.error(f"Gemini API call failed after {max_retries} attempts. Final error: {e}")
                    return None
            else:
                log.error(f"Non-retryable Gemini API error: {e}")
                return None

    return None


async def audit_offer_with_perplexity(title: str, description: str | None) -> Dict[str, Any]:
    """
    Uses Perplexity API (via httpx) to audit a high-scoring offer.
    Returns a dictionary with 'is_active', 'verdict', etc.
    """
    if not PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'is_active': False, 'verdict': 'SKIPPED', 'market_context': 'Perplexity API key not configured.', 'reason_code': 'NO_API_KEY'}

    system_prompt = "Jesteś bezkompromisowym ekspertem i GURU od wyszukiwania okazji turystycznych (deal-hunting guru). Twoim celem nie jest neutralna analiza, ale wydanie JEDNOZNACZNEJ, twardej rekomendacji. WSZYSTKIE ODPOWIEDZI MUSZĄ BYĆ PO POLSKU. Twoim zadaniem jest ocena, czy podana oferta to prawdziwa 'perełka', którą trzeba rezerwować natychmiast, czy tylko 'zapychacz'. Bądź bardzo krytyczny wobec ceny. Jeśli oferta jest tylko 'OK' lub 'przeciętna', nie wahaj się użyć werdyktu 'CENA RYNKOWA'. Werdykt 'SUPER OKAZJA' rezerwuj tylko dla absolutnych hitów. Sprawdź podany link i oceń realną dostępność. Odpowiedz ZAWSZE w formacie JSON, zawierającym klucze: 'is_active' (boolean), 'verdict' (string, np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'), 'market_context' (string, BARDZO zwięzłe uzasadnienie werdyktu, MAX 2 zdania. Bądź ekstremalnie zwięzły.), oraz 'reason_code' (string, np. 'ACTIVE_HIT', 'ACTIVE_OK', 'EXPIRED')."
    user_prompt = f"Tytuł oferty: {title}\nOpis: {description or 'Brak opisu.'}"

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "is_active": {"type": "boolean", "description": "Status aktywności oferty."},
                        "verdict": {"type": "string", "description": "Werdykt np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'."},
                        "market_context": {"type": "string", "description": "Analiza rynkowa/uzasadnienie."},
                        "reason_code": {"type": "string", "description": "Kod błędu lub statusu, np. 'ACTIVE_OK', 'EXPIRED', 'API_ERROR'."},
                    },
                    "required": ["is_active", "verdict", "market_context", "reason_code"]
                }
            }
        }
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {PERPLEXITY_API_KEY}"
    }

    try:
        async with make_async_client() as client:
            response = await client.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers, timeout=120.0)
            response.raise_for_status()
            
            response_json = response.json()
            raw_content = response_json['choices'][0]['message']['content']
            
            audit_result = json.loads(raw_content)
            
            log.info(f"Perplexity audit for '{title[:30]}...' successful. Active: {audit_result.get('is_active')}")
            return audit_result

    except httpx.HTTPStatusError as e:
        log.error(f"Perplexity API returned status {e.response.status_code}: {e.response.text}", exc_info=True)
        return {'is_active': False, 'verdict': 'ERROR', 'market_context': f'API call failed: {e.response.text}', 'reason_code': 'HTTP_STATUS_ERROR'}
    except Exception as e:
        log.error(f"Perplexity API audit failed for '{title[:30]}...'. Error: {e}", exc_info=True)
        return {'is_active': False, 'verdict': 'ERROR', 'market_context': f'API call failed: {e}', 'reason_code': 'CLIENT_EXCEPTION'}

async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not gemini_model:
        log.error("Gemini AI model not initialized. Skipping AI analysis.")
        return []

    system_prompt = f"""
Jesteś Globalnym Ekspertem Rynku Lotniczego i Turystycznego. WSZYSTKIE ODPOWIEDZI TEKSTOWE MUSZĄ BYĆ W JĘZYKU POLSKIM. Twoim zadaniem jest analiza listy ofert RSS i zwrócenie listy wyników w formacie JSON.
Twoim celem jest kategoryzacja i ocena treści, a nie ich całkowite odrzucanie, chyba że jest to spam.

Przetwórz KAŻDĄ ofertę z poniższej listy. Dla każdej oferty wykonaj następujące kroki:

KROK 1: KONTEKST ŹRÓDŁA (Dostosuj perspektywę):
    • 'The Flight Deal' (i inne z USA jak 'theflightdeal.com'): Rynek USA. Waluta USD. Loty wewnątrz USA lub z USA są atrakcyjne. Nie obniżaj oceny za wylot z Ameryki.
    • 'Fly4Free' (i inne PL/EU jak 'fly4free.pl', 'wakacyjnipiraci.pl', 'travel-dealz.com'): Rynek Europejski (szczególnie Polska). Waluta PLN/EUR. Priorytet: Polska + Huby (Berlin, Praga, Wiedeń, Londyn, Sztokholm - tani dolot).
    • 'Travel Dealz' (lub wzmianka o 'Business Class' w tytule/opisie): Rynek Premium. Oczekuj wysokich cen (np. 5000 PLN). Jeśli to Biznes Klasa - oceniaj jako okazję, nie jako drożyznę.
    • Dla wszystkich innych źródeł: Ocena globalna.

KROK 2: OCENA (1-10):
    • 9-10: Mega Hit, Error Fare, Biznes w cenie Economy, **Ważny News** (strajki, wizy, zmiany w przepisach).
    • 7-8: Dobra, solidna oferta cenowa.
    • 6: Wystarczająco dobra oferta LUB **interesujący news/relacja turystyczna**, żeby wrzucić na czat.
    • 1-5: Przeciętna cena, reklama, spam, nieistotne informacje. (ODRZUĆ, is_good: false).

KROK 3: KLASYFIKACJA TREŚCI:
    • Ustaw `"content_type"`: "offer" dla konkretnych ofert cenowych.
    • Ustaw `"content_type"`: "news" dla wiadomości, relacji turystycznych, ogłoszeń (np. o strajkach, nowych trasach).

KROK 4: GENEROWANIE TREŚCI:
    • `channel_msg`: Krótki, dziennikarski styl, max 200 znaków. Idealny jako tytuł do podsumowania.
    • `chat_msg`: Wiadomość w formacie Markdown. Wybierz jeden z poniższych szablonów, który najlepiej pasuje do oferty. Bądź kreatywny przy tworzeniu opisu.

      ---
      **Szablon 1: LOTY**
      `✈️ **[KIERUNEK]** (z: [MIASTO_WYLOTU])`
      `📅 Termin: [DATA_LUB_MIESIĄC]`
      `💰 Cena: **[CENA]**`
      ``
      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`
      `───────────────`
      `#[tag1] #[tag2] #[tag3]`

      ---
      **Szablon 2: PAKIETY (Lot + Hotel)**
      `🌴 **[KIERUNEK]** (Pakiet z: [MIASTO_WYLOTU])`
      `📅 Termin: [DATA_LUB_MIESIĄC]`
      `💰 Cena: **[CENA]** (za pakiet)`
      ``
      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`
      `───────────────`
      `#[tag1] #[tag2] #[tag3]`

      ---
      **Szablon 3: HOTELE / NOCLEGI**
      `🏨 **[NAZWA HOTELU]** w [MIEJSCOWOŚĆ]`
      `📅 Dostępność: [DATA_LUB_MIESIĄC]`
      `💰 Cena: **[CENA]** (za noc)`
      ``
      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`
      `───────────────`
      `#[tag1] #[tag2] #[tag3]`

      ---
      **Szablon 4: WYCIECZKI / WYDARZENIA / INNE**
      `🎟️ **[NAZWA WYDARZENIA / ATRAKCJI]** w [MIEJSCOWOŚĆ]`
      `📅 Kiedy: [DATA_LUB_MIESIĄC]`
      `💰 Cena: **[CENA]** (wstęp/bilet)`
      ``
      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`
      `───────────────`
      `#[tag1] #[tag2] #[tag3]`

      ---
      **Zasady dodatkowe:**
      - **Zasada dla Daty:** Jeśli nie ma konkretnej daty, ale jest zakres (np. styczeń-marzec), użyj go. Jeśli nie ma żadnych informacji o dacie, napisz "Różne terminy". Nigdy nie pisz "Brak danych".
      - Jeśli oferta nie pasuje idealnie do żadnego szablonu, użyj najbardziej zbliżonego i logicznie go dostosuj.
      - **Na końcu wiadomości, po separatorze, dodaj 3-5 trafnych hashtagów po polsku, bez znaków specjalnych (np. #wakacje #hiszpania #podroze).**

KROK 5: SELEKCJA NA CZAT:
    • Ustaw 'post_to_chat': true TYLKO dla ocen 9-10 (Hity) lub Ważnych Newsów (np. o strajkach, zmianach wizowych). Nie chcemy spamu na czacie.

Twoja odpowiedź MUSI być pojedynczym obiektem JSON, zawierającym klucz "results", który jest listą obiektów. Każdy obiekt w liście musi odpowiadać jednej ofercie z wejścia i zawierać jej oryginalne "id".

Format odpowiedzi:
{{
  "results": [
    {{ "id": 0, "score": int, "is_good": bool, "post_to_chat": bool, "channel_msg": str, "chat_msg": str, "content_type": "offer" | "news" }},
    {{ "id": 1, "score": int, "is_good": bool, "post_to_chat": bool, "channel_msg": str, "chat_msg": str, "content_type": "offer" | "news" }}
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
    
    user_message = "\n---\n".join(batch_prompt_parts)

    log.info(f"Sending a batch of {len(candidates)} candidates to Gemini AI via retry handler.")
    response = await gemini_api_call_with_retry(gemini_model, [system_prompt, user_message])

    if not response or not response.text:
        log.warning("Gemini API returned no response for batch after retries or due to a non-retryable error.")
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

async def generate_social_message_ai(target: str) -> str | None:
    if not gemini_model:
        log.error("Gemini AI model not initialized. Cannot generate social message.")
        return None

    if target == "channel":
        prompt_text = "Napisz krótki, zachęcający i nieco tajemniczy post na kanał Telegram. Celem jest zachęcenie użytkowników do przejścia na powiązaną grupę czatową, aby podyskutować o najnowszych ofertach i podzielić się wrażeniami. Unikaj bezpośredniego linkowania. Bądź naturalny i kreatywny, żeby post nie wyglądał jak automat. Max 150 znaków."
    elif target == "chat_group":
        prompt_text = """
Jesteś community managerem kanału o tanich lotach. Twoim zadaniem jest napisanie krótkiego, angażującego posta na GRUPĘ CZATOWĄ, który zachęci użytkowników do sprawdzenia głównego KANAŁU VIP, gdzie publikowane są tylko najlepsze, zweryfikowane okazje.

Bądź kreatywny i naturalny. Twój post powinien być inspirowany jedną z poniższych idei:
- Idea 1: Podkreśl, że na czacie jest duży ruch ("przemial"), a na kanale jest czysta jakość.
- Idea 2: Użyj metafory szukania "igły w stogu siana" i wskaż, że na kanale są już te znalezione "igły".
- Idea 3: Zagraj na strachu przed przegapieniem (FOMO) - na kanale są pewniaki, których nie można przegapić.
- Idea 4: Użyj zwięzłego, chwytliwego hasła rozróżniającego cel czatu (dyskusje) i kanału (konkretne oferty).

Przykłady inspiracji (nie kopiuj ich 1:1):
"🌪️ Ale dzisiaj przemiał! Jeśli wolisz samą jakość bez spamu, wbijaj na nasz KANAŁ VIP. Tam tylko zweryfikowane hity."
"🧐 Szukasz igły w stogu siana? My już ją znaleźliśmy! Najlepsze okazje (9/10) lądują na KANALE. Tutaj zostawiamy strumień dla łowców."
"🚀 Boisz się, że najlepsza oferta zginie w tłumie? Włącz powiadomienia na KANALE - tam trafiają tylko pewniaki!"
"💎 Czat jest do gadania, Kanał jest do latania! Zweryfikowane okazje znajdziesz na Kanale."
"""
    else:
        log.error(f"Invalid target for social message generation: {target}")
        return None

    system_prompt = """
Twoim zadaniem jest wygenerowanie posta na Telegram.
Odpowiedź ZAWSZE w formacie JSON, zawierającym jeden klucz: "post".
Przykład: {"post": "Treść Twojego kreatywnego posta tutaj."}
"""
    log.info(f"Generating social message for {target} using Gemini AI via retry handler.")
    response = await gemini_api_call_with_retry(gemini_model, [system_prompt, prompt_text])

    if not response or not response.text:
        log.warning(f"Gemini API returned no response for social message generation ({target}) after retries.")
        return None

    message = response.text.strip()
    log.info(f"Generated social message for {target}: {message[:70]}...")
    return message

# ---------- PRZEBUDOWANA LOGIKA WYSYŁANIA ----------

async def send_social_telegram_message_async(message_content: str, chat_id: str, button_text: str, button_url: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message_content,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "url": button_url}
                    ]]
                }
            }
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Social message sent: {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false for social message: {body}")
        except Exception as e:
            log.error(f"Telegram send error for social message to {chat_id}: {e}")
    return None

async def send_photo_with_button_async(chat_id: str, photo_url: str, caption: str, button_text: str, button_url: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML", # Caption can be HTML
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "url": button_url}
                    ]]
                }
            }
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Photo sent to {chat_id}: {photo_url}")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false for sendPhoto: {body}")
        except Exception as e:
            log.error(f"Telegram sendPhoto error to {chat_id} (URL: {photo_url}): {e}", exc_info=True)
    return None

async def send_telegram_message_async(message_content: str, link: str, chat_id: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message_content,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "👉 SPRAWDŹ OFERTĘ", "url": link}
                    ]]
                }
            }
            
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Message sent (Markdown): {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false: {body}")
                if body.get("description") and "can't parse entities" in body["description"]:
                    log.warning(f"MARKDOWN PARSE ERROR. Offending text was: \n---\n{message_content}\n---")

        except Exception as e:
            log.error(f"Telegram send error for {link}: {e}", exc_info=True)
    return None

# ---------- ORYGINALNE FUNKCJE (Bez zmian) ----------
def remember_for_deletion(state: Dict[str, Any], chat_id: str, message_id: int, source_url: str):
    log.info(f"DEBUG: remember_for_deletion called. Value of DELETE_AFTER_HOURS: {DELETE_AFTER_HOURS}")
    delete_at = (datetime.now(timezone.utc) + timedelta(hours=DELETE_AFTER_HOURS)).replace(minute=0, second=0, microsecond=0)
    state["delete_queue"].append({ "chat_id": str(chat_id), "message_id": int(message_id), "delete_at": delete_at.isoformat(), "source_url": source_url })

async def sweep_delete_queue(state: Dict[str, Any]) -> int:
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
                    cleaned_from_queue_count += 1
                    log.info(f"Wiadomość {item_id} już nie istniała. Uznaję za posprzątane i usuwam z kolejki.")
                
                elif "message is too old to be deleted" in description or "message can't be deleted" in description:
                    cleaned_from_queue_count += 1
                    log.warning(f"Nie można usunąć wiadomości {item_id}, była za stara (limit 48h). Mimo to usuwam z kolejki.")

                else:
                    final_queue.append(item)
                    log.error(f"Nie udało się usunąć wiadomości {item_id} z powodu błędu API: {res.status_code} {description}. Zostawiam do ponownej próby.")
                continue
            
            final_queue.append(item)
            log.error(f"Server-side error for message {item_id}. Will retry. Status: {res.status_code}, Response: {res.text}")

    total_processed = actually_deleted_count + cleaned_from_queue_count
    items_to_retry = len(process_now) - total_processed

    if total_processed > 0:
        state["delete_queue"] = final_queue
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

async def handle_social_posts(state: Dict[str, Any], current_generation: int):
    if not TELEGRAM_CHANNEL_ID or not TELEGRAM_CHAT_GROUP_ID or not TELEGRAM_CHANNEL_USERNAME:
        log.warning("Skipping social posts: TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_GROUP_ID or TELEGRAM_CHANNEL_USERNAME not set.")
        return

    log.info("Initiating social engagement post sequence.")
    
    channel_msg_raw = await generate_social_message_ai("channel")
    if channel_msg_raw:
        try:
            channel_data = json.loads(channel_msg_raw)
            channel_msg = channel_data.get("post", channel_msg_raw)
        except json.JSONDecodeError:
            channel_msg = channel_msg_raw

        log.info("Sending social channel message with inline button.")
        await send_social_telegram_message_async(
            message_content=channel_msg,
            chat_id=TELEGRAM_CHANNEL_ID,
            button_text="💬 Wejdź na czat",
            button_url=CHAT_CHANNEL_URL or "https://t.me/+iKncwXtipa02MWNk"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    chat_group_msg_raw = await generate_social_message_ai("chat_group")
    if chat_group_msg_raw:
        try:
            chat_group_data = json.loads(chat_group_msg_raw)
            chat_group_msg = chat_group_data.get("post", chat_group_msg_raw)
        except json.JSONDecodeError:
            chat_group_msg = chat_group_msg_raw

        log.info("Sending social chat group message with inline button.")
        await send_social_telegram_message_async(
            message_content=chat_group_msg,
            chat_id=TELEGRAM_CHAT_GROUP_ID,
            button_text="👉 Sprawdź Kanał VIP",
            button_url=f"https://t.me/{TELEGRAM_CHANNEL_USERNAME.lstrip('@')}"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    now_utc = datetime.now(timezone.utc)
    state["last_social_post_time"] = now_utc.isoformat()
    try:
        current_state, current_generation = load_state()
        current_state["last_social_post_time"] = state["last_social_post_time"]
        save_state_atomic(current_state, current_generation)
        log.info(f"Updated last_social_post_time to {state['last_social_post_time']}.")
    except Exception as e:
        log.error(f"Failed to save state after social post: {e}")

# ---------- GŁÓWNA LOGIKA (Używamy ostatniej, prostej wersji) ----------
async def publish_digest_async() -> str:
    log.info("Starting weekly digest generation...")
    state, generation = load_state()

    if not TELEGRAPH_TOKEN:
        log.error("TELEGRAPH_TOKEN is not configured. Cannot publish digest.")
        return "Error: Telegraph token not configured."

    digest_candidates = state.get("digest_candidates", [])
    if not digest_candidates:
        log.info("Digest candidates list is empty. Skipping digest generation.")
        return "Digest candidates list is empty, no digest to generate."

    unique_offers_dict = {}
    for offer in digest_candidates:
        dedup_key = offer.get('dedup_key')
        if not dedup_key: continue
        
        score = int(offer.get('score', 0))
        if dedup_key not in unique_offers_dict or score > int(unique_offers_dict[dedup_key].get('score', 0)):
            unique_offers_dict[dedup_key] = offer
    
    unique_offers = list(unique_offers_dict.values())
    log.info(f"Found {len(digest_candidates)} offers in candidates list, {len(unique_offers)} after deduplication.")

    sorted_by_score = sorted(unique_offers, key=lambda o: int(o.get('score', 0)), reverse=True)
    top_25_offers = sorted_by_score[:25]

    sorted_alphabetically = sorted(top_25_offers, key=lambda o: o.get('title', ''))
    log.info(f"Selected {len(sorted_alphabetically)} offers for the digest.")

    telegraph = Telegraph(TELEGRAPH_TOKEN)
    
    content_html = ""
    for offer in sorted_alphabetically:
        title_for_digest = offer.get('ai_generated_title', offer.get('original_title', 'Brak tytułu'))
        verdict = offer.get('verdict', 'Nieokreślony werdykt')
        market_context = offer.get('market_context', 'Brak szczegółów analizy rynkowej.')
        link = offer.get('link')
        source_name = offer.get('source_name', 'Nieznane')
        
        content_html += f"<h4>{html.escape(title_for_digest)}</h4>"
        content_html += f"<p><b>Werdykt:</b> {html.escape(str(verdict))}</p>"
        content_html += f"<p><i>Analiza:</i> {html.escape(str(market_context))}</p>"
        content_html += f"<p><b>Źródło:</b> {html.escape(source_name)}</p>"
        content_html += f"<p><a href='{html.escape(link)}'>👉 SPRAWDŹ OFERTĘ</a></p>"
        content_html += "<hr/>"

    try:
        page_title = f"Hity Tygodnia: Podsumowanie Ofert ({datetime.now().strftime('%Y-%m-%d')})"
        response = telegraph.create_page(
            title=page_title,
            html_content=content_html,
            author_name="Travel Bot",
        )
        page_url = response['url']
        log.info(f"Successfully created Telegra.ph page: {page_url}")

        engaging_caption = "🔥 <b>GORĄCE HITY TYGODNIA SĄ GOTOWE!</b> 🔥\n\nOto starannie wyselekcjonowane, najlepsze okazje z ostatnich dni. Nie przegap – mogą szybko zniknąć!\n\n<i>Sprawdź, klikając w przycisk poniżej!</i>"
        digest_button_text = "💎 Zobacz Ekskluzywne Hity! 💎"
        
        selected_photo_url = random.choice(DIGEST_IMAGE_URLS)

        sending_tasks = []
        if TELEGRAM_CHANNEL_ID:
            sending_tasks.append(send_photo_with_button_async(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo_url=selected_photo_url,
                caption=engaging_caption,
                button_text=digest_button_text,
                button_url=page_url
            ))
        
        if sending_tasks:
            await asyncio.gather(*sending_tasks)

        state["digest_candidates"] = []
        save_state_atomic(state, generation)
        log.info("Digest candidates list has been cleared and state saved.")
        
        return f"Weekly Digest published successfully: {page_url}"

    except Exception as e:
        log.error(f"Failed to create or publish Telegra.ph page: {e}", exc_info=True)
        return "Error during digest publication."

async def send_promotional_post_async() -> str:
    log.info("Starting promotional post sequence...")
    try:
        state, generation = load_state()
        await handle_social_posts(state, generation)
        log.info("Promotional post sequence completed.")
        return "Promotional post sequence completed."
    except Exception as e:
        log.error(f"Error during promotional post sequence: {e}", exc_info=True)
        return f"Error during promotional post sequence: {e}"


async def master_scheduler():
    now_utc = datetime.now(timezone.utc)
    log.info(f"Master scheduler running at {now_utc.isoformat()}")

    log.info("Scheduler: Kicking off ingestion process.")
    await process_sources_async()
    
    digest_sent = False
    
    if now_utc.hour in [10, 20]:
        log.info(f"Scheduler: It's {now_utc.hour}:00 UTC, publishing digest.")
        await publish_digest_async()
        digest_sent = True
    
    is_promo_time = (now_utc.hour % 2 != 0 and 9 <= now_utc.hour <= 23) or now_utc.hour == 20

    if is_promo_time:
         log.info(f"Scheduler: It's {now_utc.hour}:00 UTC, running promotional post.")
         await send_promotional_post_async() 

    log.info("Master scheduler run finished.")
    return "Scheduler run complete."

async def process_sources_async() -> str:
    log.info("Starting a simple RSS-only processing run...")

    if not TG_TOKEN or not TELEGRAM_CHANNEL_ID: return "Missing critical environment variables."
    state, generation = load_state()

    try:
        fixed_count = sanitizing_startup_check(state)
        if fixed_count > 0:
            log.warning(f"CRITICAL REPAIR: Found and fixed {fixed_count} corrupted entries in state file.")
            try:
                save_state_atomic(state, generation)
                log.info("Successfully saved repaired state. Reloading state to continue run.")
                state, generation = load_state()
            except Exception as e:
                log.critical(f"CRITICAL FAILURE: Could not save repaired state file. Aborting run. Error: {e}")
                return "Critical: State repair failed during save."
    except Exception as e:
        log.error(f"An unexpected error occurred during the sanitizing check: {e}")

    log.info("Running the integrated sweep job at the start of the main run...")
    try:
        deleted_count = await sweep_delete_queue(state)
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

    if MAX_POSTS_PER_RUN > 0: candidates = candidates[:MAX_POSTS_PER_RUN]
    
    if not candidates:
        log.info("No new posts to send. (All posts were duplicates or no posts were found).")
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after pruning: {e}")
        return "Run complete. No new posts."

    log.info(f"Found {len(candidates)} new candidates to process.")

    now_utc = datetime.now(timezone.utc)
    last_analysis_time_str = state.get("last_ai_analysis_time", "1970-01-01T00:00:00Z")
    try:
        last_analysis_time = datetime.fromisoformat(last_analysis_time_str)
    except ValueError:
        log.warning(f"Malformed last_ai_analysis_time in state: {last_analysis_time_str}. Resetting.")
        last_analysis_time = datetime.fromisoformat("1970-01-01T00:00:00Z")

    time_since_last_analysis = now_utc - last_analysis_time
    if time_since_last_analysis < timedelta(minutes=3):
        log.info(f"AI analysis skipped. Last analysis was {time_since_last_analysis.total_seconds():.1f} seconds ago. Need to wait 3 minutes.")
        try:
            save_state_atomic(state, generation)
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after skipping AI analysis: {e}")
        return "Run complete. AI analysis skipped due to 3-minute cooldown."
    
    log.info("Proceeding with AI analysis.")
    state["last_ai_analysis_time"] = now_utc.isoformat()
    
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

    BATCH_SIZE = 5
    candidate_chunks = [detailed_candidates[i:i + BATCH_SIZE] for i in range(0, len(detailed_candidates), BATCH_SIZE)]
    
    all_ai_results = []
    for i, chunk in enumerate(candidate_chunks):
        results = await analyze_batch(chunk)
        all_ai_results.extend(results)
        if i < len(candidate_chunks) - 1:
            wait_time = 1
            log.info(f"Processed chunk {i+1}/{len(candidate_chunks)}. Waiting {wait_time}s before next batch to respect API rate limits.")
            await asyncio.sleep(wait_time)

    if not all_ai_results:
        log.warning("AI analysis returned no results for any batch.")
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after empty AI result: {e}")
        return "Run complete. AI analysis yielded no results."
    
    candidates_by_id = {c['id']: c for c in detailed_candidates}

    sent_count_channel = 0
    sent_count_chat = 0
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    
    for ai_result in all_ai_results:
        result_id = ai_result.get("id")
        if result_id is None: continue

        original_candidate = candidates_by_id.get(result_id)
        if not original_candidate:
            log.warning(f"AI returned a result with ID {result_id} that does not match any original candidate.")
            continue
            
        state["sent_links"Пожалуйста, предоставьте мне `old_string` и `new_string` для анализа. Я проанализирую `new_string` на предмет проблем с экранированием и предоставлю исправленную версию в формате JSON.```json
{
  "corrected_new_string_escaping": "# Final Hybrid Bot Code v6.0 (Refactored & Simplified)\nimport os\nimport logging\nimport asyncio\nimport httpx\nimport feedparser\nimport json\nimport time\nimport random\nimport html\nfrom flask import Flask, request, jsonify\nfrom google.cloud import storage \nimport google.generativeai as genai\nfrom telegraph import Telegraph\nfrom google.generativeai.types import HarmCategory, HarmBlockThreshold\nfrom urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote\nfrom typing import Dict, Any, Tuple, List\nfrom datetime import datetime, timedelta, timezone\nfrom bs4 import BeautifulSoup\n\n# ---------- LOGGING ----------\nlogging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')\nlog = logging.getLogger(__name__)\n\n# ---------- APP / GCS ----------\napp = Flask(__name__)\nstorage_client = storage.Client()\n\n# ---------- ENV ----------\ndef env(name: str, default: Any = None) -> Any:\n    return os.environ.get(name, default)\n\nTG_TOKEN = env(\"TG_TOKEN\")\nTELEGRAM_CHANNEL_ID = env(\"TELEGRAM_CHANNEL_ID\")\nTELEGRAM_CHAT_GROUP_ID = env(\"TELEGRAM_CHAT_GROUP_ID\")\nTELEGRAM_CHANNEL_USERNAME = env(\"TELEGRAM_CHANNEL_USERNAME\")\nCHAT_CHANNEL_URL = env(\"CHAT_CHANNEL_URL\")\nTELEGRAPH_TOKEN = env(\"TELEGRAPH_TOKEN\")\nGEMINI_API_KEY = env(\"GEMINI_API_KEY\")\nPERPLEXITY_API_KEY = env(\"PERPLEXITY_API_KEY\")\nBUCKET_NAME = env(\"BUCKET_NAME\")\nSENT_LINKS_FILE = env(\"SENT_LINKS_FILE\", \"sent_links.json\")\nHTTP_TIMEOUT = float(env(\"HTTP_TIMEOUT\", \"15.0\"))\nTELEGRAM_SECRET = env(\"TELEGRAM_SECRET\")\nDEBUG_FEEDS = env(\"DEBUG_FEEDS\", \"0\") in {\"1\", \"true\", \"True\", \"yes\", \"YES\"}\nMAX_POSTS_PER_RUN = int(env(\"MAX_POSTS_PER_RUN\", \"0\"))\n\nDELETE_AFTER_HOURS = int(env(\"DELETE_AFTER_HOURS\", \"48\"))\nDEDUP_TTL_HOURS = int(env(\"DEDUP_TTL_HOURS\", \"336\"))\n\nMAX_PER_DOMAIN = int(env(\"MAX_PER_DOMAIN\", \"8\"))\nPER_HOST_CONCURRENCY = int(env(\"PER_HOST_CONCURRENCY\", \"2\"))\nJITTER_MIN_MS = int(env(\"JITTER_MIN_MS\", \"120\"))\nJITTER_MAX_MS = int(env(\"JITTER_MAX_MS\", \"400\"))\n\nSECRETFLYING_HOST = \"secretflying.com\"\n\n# ---------- DIGEST IMAGES (USER-PROVIDED) ----------\nDIGEST_IMAGE_URLS = [\n    \"https://images.unsplash.com/photo-1516483638261-f4dbaf036963?q=80&w=2800&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D\",\n    \"https://images.pexels.com/photos/3408744/pexels-photo-3408744.jpeg?auto=compress&cs=tinysrgb&w=1260&h=750&dpr=2\",\n    \"https://cdn.pixabay.com/photo/2017/01/20/00/30/maldives-1993704_1280.jpg\"\n]\n\n# ---------- GEMINI AI CONFIGURATION ----------\nif GEMINI_API_KEY:\n    genai.configure(api_key=GEMINI_API_KEY)\n    gemini_model = genai.GenerativeModel(\n        'gemini-2.5-flash',\n        generation_config={"response_mime_type": "application/json"}\n    )\n    log.info(\"Gemini AI model configured.\")\nelse:\n    gemini_model = None\n    log.warning(\"GEMINI_API_KEY not set. AI analysis will be disabled.\")\n\nSAFETY_SETTINGS = {\n    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,\n    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,\n    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,\n    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,\n}\n\n# NOWA SEKACJA: EMOTIKONY (Bez zmian)\nEMOJI_KEYWORDS = {\n    '🇬🇧': ['londyn', 'london', 'anglia', 'uk', 'brytanii'],\n    '🇪🇸': ['hiszpanii', 'spain', 'barcelona', 'madryt', 'madrid', 'majorka', 'mallorca'],\n    '🇮🇹': ['włochy', 'italy', 'rzym', 'rome', 'mediolan', 'milan'],\n    '🇫🇷': ['francja', 'france', 'paryż', 'paris'],\n    '🇩🇪': ['niemcy', 'germany', 'berlin'],\n    '🇵🇹': ['portugalia', 'portugal', 'lizbona', 'lisbon'],\n    '🇺🇸': ['usa', 'stany', 'york', 'chicago', 'miami'],\n    '🇦🇪': ['dubaj', 'dubai', 'emiraty', 'emirates'],\n    '🇯🇵': ['japonia', 'japan', 'tokio', 'tokyo'],\n    '🇹🇭': ['tajlandia', 'thailand', 'bangkok'],\n    '🏖️': ['plaża', 'beach', 'wakacje', 'holiday', 'morze', 'sea', 'wyspy', 'islands'],\n    '✈️': ['loty', 'flights', 'lot', 'flight'],\n    '🏨': ['hotel', 'nocleg'],\n    '💰': ['okazja', 'deal', 'tanio', 'cheap', 'promocja'],\n}\n\ndef dbg(msg: str):\n    if DEBUG_FEEDS: log.info(f"DEBUG {msg}")\n\n# ---------- GCS STATE MANAGEMENT (Bez zmian) ----------\n_bucket = storage_client.bucket(BUCKET_NAME) if BUCKET_NAME else None\n_blob = _bucket.blob(SENT_LINKS_FILE) if _bucket else None\n\nDROP_PARAMS = {\n    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",\n    "fbclid","gclid","igshid","mc_cid","mc_eid","ref","ref_src","src"\n}\n\ndef canonicalize_url(url: str) -> str:\n    try:\n        u = unquote(url.strip())\n        p = urlparse(u)\n        scheme = (p.scheme or "https").lower()\n        netloc = p.netloc.lower().replace("www.", "")\n        path = p.path or "/"\n        if path != "/" and path.endswith("/"): path = path[:-1]\n        q = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS])\n        return urlunparse((scheme, netloc, path, p.params, urlencode(q, doseq=True), ""))\n    except Exception:\n        return url.strip()\n\ndef _default_state() -> Dict[str, Any]:\n    return {"sent_links": {}, "delete_queue": [], "last_social_post_time": "1970-01-01T00:00:00Z", "last_ai_analysis_time": "1970-01-01T00:00:00Z", "digest_candidates": []}\n\ndef _ensure_state_shapes(state: Dict[str, Any]):\n    if "sent_links" not in state: state["sent_links"] = {}\n    if "delete_queue" not in state: state["delete_queue"] = []\n    if "last_social_post_time" not in state: state["last_social_post_time"] = "1970-01-01T00:00:00Z"\n    if "last_ai_analysis_time" not in state: state["last_ai_analysis_time"] = "1970-01-01T00:00:00Z"\n    if "digest_candidates" not in state: state["digest_candidates"] = []\n\ndef load_state() -> Tuple[Dict[str, Any], int | None]:\n    if not _blob: return (_default_state(), None)\n    try:\n        if not _blob.exists(): return _default_state(), None\n        _blob.reload()\n        state_data = json.loads(_blob.download_as_bytes())\n        _ensure_state_shapes(state_data)\n        return state_data, _blob.generation\n    except Exception as e:\n        log.warning(f"load_state fallback: {e}")\n        return _default_state(), None\n\ndef save_state_atomic(state: Dict[str, Any], gen: int | None):\n    if not _blob: return\n    payload = json.dumps(state).encode('utf-8')\n    for _ in range(10):\n        try:\n            _blob.upload_from_string(payload, if_generation_match=gen or 0, content_type="application/json")\n            return\n        except Exception as e:\n            if "PreconditionFailed" in str(e) or "412" in str(e):\n                log.warning("State save conflict, retrying...")\n                time.sleep(random.uniform(0.3, 0.8)); _, gen = load_state()\n                continue\n            raise\n    raise RuntimeError("Atomic state save failed.")\n\ndef sanitizing_startup_check(state: Dict[str, Any]) -> int:\n    """\n    Sprawdza i naprawia kolejkę 'delete_queue' pod kątem uszkodzonych wpisów chat_id.\n    Jest to jednorazowa funkcja naprawcza uruchamiana przy starcie.\n    Zwraca liczbę naprawionych wpisów.\n    """\n    if "delete_queue" not in state or not isinstance(state.get("delete_queue"), list):\n        return 0\n\n    fixed_entries_count = 0\n    sanitized_queue = []\n    \n    import re\n    id_pattern = re.compile(r"^(-?\d+)")\n\n    for item in state.get("delete_queue", []):\n        if not isinstance(item, dict) or "chat_id" not in item:\n            sanitized_queue.append(item)\n            continue\n\n        chat_id = item["chat_id"]\n        \n        if isinstance(chat_id, str) and ' ' in chat_id:\n            original_id = chat_id\n            match = id_pattern.match(original_id)\n            if match:\n                clean_id = match.group(1)\n                item["chat_id"] = clean_id\n                fixed_entries_count += 1\n                log.info(f"Sanitized chat_id: '{original_id}' -> '{clean_id}'")\n            else:\n                log.warning(f"Could not sanitize chat_id '{original_id}'. Keeping original but this is an error.")\n        \n        sanitized_queue.append(item)\n\n    if fixed_entries_count > 0:\n        state["delete_queue"] = sanitized_queue\n        log.info(f"SANITIZING COMPLETE: Repaired {fixed_entries_count} entries in the delete_queue.")\n\n    return fixed_entries_count\n\n\n# ---------- DOMAIN-SPECIFIC CONFIG & HTTP CLIENT (Bez zmian) ----------\nDOMAIN_CONFIG: Dict[str, Dict[str, Any]] = {\n    "travel-dealz.com": { \n        "selectors": ['article.article-item h2 a', 'article.article h2 a'],\n        "headers": { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" } \n    },\n    "secretflying.com": { "selectors": ['article.post-item .post-title a', 'article h2 a'], "rss": ["https://www.secretflying.com/feed/"] },\n    "wakacyjnipiraci.pl": { "selectors": ['article.post-list__item a.post-list__link'], "rss": ["https://www.wakacyjnipiraci.pl/feed"], "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" } },\n    "holidaypirates.com": { "selectors": ['article.post-list__item a.post-list__link'], "rss": ["https://www.holidaypirates.com/feed"], "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" } },\n    "theflightdeal.com": { "selectors": ['article h2 a', '.entry-title a'], "rss": ["https://www.theflightdeal.com/feed/"] },\n    "travelfree.info": { "headers": { "Accept-Encoding": "gzip, deflate", "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7" } },\n    "fly4free.pl": { "rss": ["https://www.fly4free.pl/feed/"] },\n    "loter.pl": { "selectors": ['article h2 a', 'article h3 a'] }\n}\nGENERIC_FALLBACK_SELECTORS = ['article h2 a', 'article h3 a', 'h2 a', 'h3 a']\nBASE_HEADERS = {"Accept-Encoding": "gzip, deflate", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}\n\ndef build_headers(url: str) -> Dict[str, str]:\n    host = urlparse(url).netloc.lower().replace("www.", "")\n    headers = BASE_HEADERS.copy()\n    domain_headers = DOMAIN_CONFIG.get(host, {}).get("headers")\n    if domain_headers: headers.update(domain_headers)\n    return headers\n\ndef make_async_client() -> httpx.AsyncClient:\n    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, http2=True)\n\ndef get_sources(filename: str) -> List[str]:\n    try:\n        with open(filename, 'r', encoding='utf-8') as f:\n            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]\n    except FileNotFoundError:\n        log.warning(f"Source file not found: {filename}")\n        return []\n\n# ---------- CONCURRENCY HELPERS (Bez zmian) ----------\n_host_semaphores: Dict[str, asyncio.Semaphore] = {}\ndef _sem_for(url: str) -> asyncio.Semaphore:\n    host = urlparse(url).netloc.lower()\n    if host not in _host_semaphores: _host_semaphores[host] = asyncio.Semaphore(PER_HOST_CONCURRENCY)\n    return _host_semaphores[host]\n\nasync def _jitter():\n    await asyncio.sleep(random.uniform(JITTER_MIN_MS/1000.0, JITTER_MAX_MS/1000.0))\n\n# ---------- NOWE FUNKCJE POMOCNICZE (INTELIGENCJA) ----------\n# Funkcja shorten_link usunięta zgodnie z prośbą użytkownika.\n\n# ########## ZBĘDNE FUNKCJE USUNIĘTE ##########\n# Usunięto: is_image_safe\n# Usunięto: _run_face_detection_sync\n# Usunięto: find_safe_image_url\n# ###########################################\n\nasync def scrape_description(client: httpx.AsyncClient, url: str) -> str | None:\n    try:\n        async with _sem_for(url):\n            r = await client.get(url, headers=build_headers(url))\n        r.raise_for_status()\n        soup = BeautifulSoup(r.text, "html.parser")\n        \n        selectors = ['article p', '.entry-content p', '.post-content p', 'main p']\n        for sel in selectors:\n            p_tag = soup.select_one(sel)\n            if p_tag:\n                text = p_tag.get_text(separator=' ', strip=True)\n                if len(text) > 40:\n                    if len(text) > 500:\n                        last_space = text.rfind(' ', 0, 500)\n                        if last_space != -1:\n                            return text[:last_space] + '...'\n                        else:\n                            return text[:500] + '...'
                    else:\n                        return text\n    except Exception as e:\n        dbg(f"Could not scrape description for {url}: {e}")\n    return None\n\n\n\nasync def gemini_api_call_with_retry(model, prompt_parts, max_retries=4):\n    """\n    Calls the Gemini API with exponential backoff retry mechanism.\n    Handles 429 (Too Many Requests) and 503 (Service Unavailable) errors.\n    """\n    if not model:\n        log.error("Gemini model not provided to retry function.")\n        return None\n\n    for attempt in range(max_retries):\n        try:\n            response = await model.generate_content_async(\n                prompt_parts,\n                safety_settings=SAFETY_SETTINGS\n            )\n            return response\n        except Exception as e:\n            error_str = str(e).lower()\n            if ("429" in error_str and "resource has been exhausted" in error_str) or "503" in error_str or "service unavailable" in error_str:\n                if attempt < max_retries - 1:\n                    wait_time = 2 ** (attempt + 1)\n                    log.warning(f"Rate limit hit or service unavailable on attempt {attempt + 1}/{max_retries}. Retrying in {wait_time} seconds...")\n                    await asyncio.sleep(wait_time)\n                else:\n                    log.error(f"Gemini API call failed after {max_retries} attempts. Final error: {e}")\n                    return None\n            else:\n                log.error(f"Non-retryable Gemini API error: {e}")\n                return None\n\n    return None\n\n\nasync def audit_offer_with_perplexity(title: str, description: str | None) -> Dict[str, Any]:\n    """\n    Uses Perplexity API (via httpx) to audit a high-scoring offer.\n    Returns a dictionary with 'is_active', 'verdict', etc.\n    """\n    if not PERPLEXITY_API_KEY:\n        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")\n        return {'is_active': False, 'verdict': 'SKIPPED', 'market_context': 'Perplexity API key not configured.', 'reason_code': 'NO_API_KEY'}\n\n    system_prompt = "Jesteś bezkompromisowym ekspertem i GURU od wyszukiwania okazji turystycznych (deal-hunting guru). Twoim celem nie jest neutralna analiza, ale wydanie JEDNOZNACZNEJ, twardej rekomendacji. WSZYSTKIE ODPOWIEDZI MUSZĄ BYĆ PO POLSKU. Twoim zadaniem jest ocena, czy podana oferta to prawdziwa 'perełka', którą trzeba rezerwować natychmiast, czy tylko 'zapychacz'. Bądź bardzo krytyczny wobec ceny. Jeśli oferta jest tylko 'OK' lub 'przeciętna', nie wahaj się użyć werdyktu 'CENA RYNKOWA'. Werdykt 'SUPER OKAZJA' rezerwuj tylko dla absolutnych hitów. Sprawdź podany link i oceń realną dostępność. Odpowiedz ZAWSZE w formacie JSON, zawierającym klucze: 'is_active' (boolean), 'verdict' (string, np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'), 'market_context' (string, BARDZO zwięzłe uzasadnienie werdyktu, MAX 2 zdania. Bądź ekstremalnie zwięzły.), oraz 'reason_code' (string, np. 'ACTIVE_HIT', 'ACTIVE_OK', 'EXPIRED')."\n    user_prompt = f"Tytuł oferty: {title}\nOpis: {description or 'Brak opisu.'}"\n\n    payload = {\n        "model": "sonar",\n        "messages": [\n            {"role": "system", "content": system_prompt},\n            {"role": "user", "content": user_prompt}\n        ],\n        "response_format": {\n            "type": "json_schema",\n            "json_schema": {\n                "schema": {\n                    "type": "object",\n                    "properties": {\n                        "is_active": {"type": "boolean", "description": "Status aktywności oferty."},\n                        "verdict": {"type": "string", "description": "Werdykt np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'."},\n                        "market_context": {"type": "string", "description": "Analiza rynkowa/uzasadnienie."},\n                        "reason_code": {"type": "string", "description": "Kod błędu lub statusu, np. 'ACTIVE_OK', 'EXPIRED', 'API_ERROR'."},\n                    },\n                    "required": ["is_active", "verdict", "market_context", "reason_code"]\n                }\n            }\n        }\n    }\n\n    headers = {\n        "accept": "application/json",\n        "content-type": "application/json",\n        "authorization": f"Bearer {PERPLEXITY_API_KEY}"\n    }\n\n    try:\n        async with make_async_client() as client:\n            response = await client.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers, timeout=120.0)\n            response.raise_for_status()\n            \n            response_json = response.json()\n            raw_content = response_json['choices'][0]['message']['content']\n            \n            audit_result = json.loads(raw_content)\n            \n            log.info(f"Perplexity audit for '{title[:30]}...' successful. Active: {audit_result.get('is_active')}")\n            return audit_result\n\n    except httpx.HTTPStatusError as e:\n        log.error(f"Perplexity API returned status {e.response.status_code}: {e.response.text}", exc_info=True)\n        return {'is_active': False, 'verdict': 'ERROR', 'market_context': f'API call failed: {e.response.text}', 'reason_code': 'HTTP_STATUS_ERROR'}\n    except Exception as e:\n        log.error(f"Perplexity API audit failed for '{title[:30]}...'. Error: {e}", exc_info=True)\n        return {'is_active': False, 'verdict': 'ERROR', 'market_context': f'API call failed: {e}', 'reason_code': 'CLIENT_EXCEPTION'}\n\nasync def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:\n    if not gemini_model:\n        log.error("Gemini AI model not initialized. Skipping AI analysis.")\n        return []\n\n    system_prompt = f"""\nJesteś Globalnym Ekspertem Rynku Lotniczego i Turystycznego. WSZYSTKIE ODPOWIEDZI TEKSTOWE MUSZĄ BYĆ W JĘZYKU POLSKIM. Twoim zadaniem jest analiza listy ofert RSS i zwrócenie listy wyników w formacie JSON.\nTwoim celem jest kategoryzacja i ocena treści, a nie ich całkowite odrzucanie, chyba że jest to spam.\n\nPrzetwórz KAŻDĄ ofertę z poniższej listy. Dla każdej oferty wykonaj następujące kroki:\n\nKROK 1: KONTEKST ŹRÓDŁA (Dostosuj perspektywę):\n    • 'The Flight Deal' (i inne z USA jak 'theflightdeal.com'): Rynek USA. Waluta USD. Loty wewnątrz USA lub z USA są atrakcyjne. Nie obniżaj oceny za wylot z Ameryki.\n    • 'Fly4Free' (i inne PL/EU jak 'fly4free.pl', 'wakacyjnipiraci.pl', 'travel-dealz.com'): Rynek Europejski (szczególnie Polska). Waluta PLN/EUR. Priorytet: Polska + Huby (Berlin, Praga, Wiedeń, Londyn, Sztokholm - tani dolot).\n    • 'Travel Dealz' (lub wzmianka o 'Business Class' w tytule/opisie): Rynek Premium. Oczekuj wysokich cen (np. 5000 PLN). Jeśli to Biznes Klasa - oceniaj jako okazję, nie jako drożyznę.\n    • Dla wszystkich innych źródeł: Ocena globalna.\n\nKROK 2: OCENA (1-10):\n    • 9-10: Mega Hit, Error Fare, Biznes w cenie Economy, **Ważny News** (strajki, wizy, zmiany w przepisach).\n    • 7-8: Dobra, solidna oferta cenowa.\n    • 6: Wystarczająco dobra oferta LUB **interesujący news/relacja turystyczna**, żeby wrzucić na czat.\n    • 1-5: Przeciętna cena, reklama, spam, nieistotne informacje. (ODRZUĆ, is_good: false).\n\nKROK 3: KLASYFIKACJA TREŚCI:\n    • Ustaw `"content_type"`: "offer" dla konkretnych ofert cenowych.\n    • Ustaw `"content_type"`: "news" dla wiadomości, relacji turystycznych, ogłoszeń (np. o strajkach, nowych trasach).\n\nKROK 4: GENEROWANIE TREŚCI:\n    • `channel_msg`: Krótki, dziennikarski styl, max 200 znaków. Idealny jako tytuł do podsumowania.\n    • `chat_msg`: Wiadomość w formacie Markdown. Wybierz jeden z poniższych szablonów, który najlepiej pasuje do oferty. Bądź kreatywny przy tworzeniu opisu.\n\n      ---\n      **Szablon 1: LOTY**\n      `✈️ **[KIERUNEK]** (z: [MIASTO_WYLOTU])`\n      `📅 Termin: [DATA_LUB_MIESIĄC]`\n      `💰 Cena: **[CENA]**`\n      ``\n      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`\n      `───────────────`\n      `#[tag1] #[tag2] #[tag3]`\n\n      ---\n      **Szablon 2: PAKIETY (Lot + Hotel)**\n      `🌴 **[KIERUNEK]** (Pakiet z: [MIASTO_WYLOTU])`\n      `📅 Termin: [DATA_LUB_MIESIĄC]`\n      `💰 Cena: **[CENA]** (za pakiet)`\n      ``\n      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`\n      `───────────────`\n      `#[tag1] #[tag2] #[tag3]`\n\n      ---\n      **Szablon 3: HOTELE / NOCLEGI**\n      `🏨 **[NAZWA HOTELU]** w [MIEJSCOWOŚĆ]`\n      `📅 Dostępność: [DATA_LUB_MIESIĄC]`\n      `💰 Cena: **[CENA]** (za noc)`\n      ``\n      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`\n      `───────────────`\n      `#[tag1] #[tag2] #[tag3]`\n\n      ---\n      **Szablon 4: WYCIECZKI / WYDARZENIA / INNE**\n      `🎟️ **[NAZWA WYDARZENIA / ATRAKCJI]** w [MIEJSCOWOŚĆ]`\n      `📅 Kiedy: [DATA_LUB_MIESIĄC]`\n      `💰 Cena: **[CENA]** (wstęp/bilet)`\n      ``\n      `📝 [TWOJE_DWA_KREATYWNE_I_ZACHĘCAJĄCE_ZDANIA_OPISU]`\n      `───────────────`\n      `#[tag1] #[tag2] #[tag3]`\n\n      ---\n      **Zasady dodatkowe:**\n      - **Zasada dla Daty:** Jeśli nie ma konkretnej daty, ale jest zakres (np. styczeń-marzec), użyj go. Jeśli nie ma żadnych informacji o dacie, napisz \"Różne terminy\". Nigdy nie pisz \"Brak danych\".\n      - Jeśli oferta nie pasuje idealnie do żadnego szablonu, użyj najbardziej zbliżonego i logicznie go dostosuj.\n      - **Na końcu wiadomości, po separatorze, dodaj 3-5 trafnych hashtagów po polsku, bez znaków specjalnych (np. #wakacje #hiszpania #podroze).**\n\nKROK 5: SELEKCJA NA CZAT:\n    • Ustaw 'post_to_chat': true TYLKO dla ocen 9-10 (Hity) lub Ważnych Newsów (np. o strajkach, zmianach wizowych). Nie chcemy spamu na czacie.\n\nTwoja odpowiedź MUSI być pojedynczym obiektem JSON, zawierającym klucz \"results\", który jest listą obiektów. Każdy obiekt w liście musi odpowiadać jednej ofercie z wejścia i zawierać jej oryginalne \"id\".\n\nFormat odpowiedzi:\n{{\n  \"results\": [\n    {{ \"id\": 0, \"score\": int, \"is_good\": bool, \"post_to_chat\": bool, \"channel_msg\": str, \"chat_msg\": str, \"content_type\": \"offer\" | \"news\" }},\n    {{ \"id\": 1, \"score\": int, \"is_good\": bool, \"post_to_chat\": bool, \"channel_msg\": str, \"chat_msg\": str, \"content_type\": \"offer\" | \"news\" }}\n  ]\n}}\n"""\n    \n    batch_prompt_parts = []\n    for candidate in candidates:\n        batch_prompt_parts.append(\n            f"OFERTA ID: {candidate['id']}\n"\n            f"Źródło: {candidate['source_name']}\n"\n            f"Tytuł: {candidate['title']}\n"\n            f"Opis: {candidate['description'] or 'Brak opisu.'}"\n        )\n    \n    user_message = "\n---\n".join(batch_prompt_parts)\n\n    log.info(f"Sending a batch of {len(candidates)} candidates to Gemini AI via retry handler.")\n    response = await gemini_api_call_with_retry(gemini_model, [system_prompt, user_message])\n\n    if not response or not response.text:\n        log.warning("Gemini API returned no response for batch after retries or due to a non-retryable error.")\n        return []\n        \n    try:\n        ai_results_wrapper = json.loads(response.text)\n        ai_results = ai_results_wrapper.get("results", [])\n        \n        if not isinstance(ai_results, list):\n            log.error(f"Gemini API returned 'results' that is not a list: {ai_results}")\n            return []\n        \n        log.info(f"AI processed batch and returned {len(ai_results)} results.")\n        return ai_results\n\n    except (json.JSONDecodeError, KeyError):\n        log.error(f"Gemini API returned invalid JSON or missing 'results' key for batch: {response.text[:200]}")\n        return []\n\nasync def generate_social_message_ai(target: str) -> str | None:\n    if not gemini_model:\n        log.error("Gemini AI model not initialized. Cannot generate social message.")\n        return None\n\n    if target == "channel":\n        prompt_text = "Napisz krótki, zachęcający i nieco tajemniczy post na kanał Telegram. Celem jest zachęcenie użytkowników do przejścia na powiązaną grupę czatową, aby podyskutować o najnowszych ofertach i podzielić się wrażeniami. Unikaj bezpośredniego linkowania. Bądź naturalny i kreatywny, żeby post nie wyglądał jak automat. Max 150 znaków."
    elif target == "chat_group":\n        prompt_text = """\nJesteś community managerem kanału o tanich lotach. Twoim zadaniem jest napisanie krótkiego, angażującego posta na GRUPĘ CZATOWĄ, który zachęci użytkowników do sprawdzenia głównego KANAŁU VIP, gdzie publikowane są tylko najlepsze, zweryfikowane okazje.\n\nBądź kreatywny i naturalny. Twój post powinien być inspirowany jedną z poniższych idei:\n- Idea 1: Podkreśl, że na czacie jest duży ruch (\"przemial\"), a na kanale jest czysta jakość.\n- Idea 2: Użyj metafory szukania \"igły w stogu siana\" i wskaż, że na kanale są już te znalezione \"igły\".\n- Idea 3: Zagraj na strachu przed przegapieniem (FOMO) - na kanale są pewniaki, których nie można przegapić.\n- Idea 4: Użyj zwięzłego, chwytliwego hasła rozróżniającego cel czatu (dyskusje) i kanału (konkretne oferty).\n\nPrzykłady inspiracji (nie kopiuj ich 1:1):\n\"🌪️ Ale dzisiaj przemiał! Jeśli wolisz samą jakość bez spamu, wbijaj na nasz KANAŁ VIP. Tam tylko zweryfikowane hity.\"\n\"🧐 Szukasz igły w stogu siana? My już ją znaleźliśmy! Najlepsze okazje (9/10) lądują na KANALE. Tutaj zostawiamy strumień dla łowców.\"\n\"🚀 Boisz się, że najlepsza oferta zginie w tłumie? Włącz powiadomienia na KANALE - tam trafiają tylko pewniaki!\"\n\"💎 Czat jest do gadania, Kanał jest do latania! Zweryfikowane okazje znajdziesz na Kanale.\"\n"""
    else:\n        log.error(f"Invalid target for social message generation: {target}")\n        return None\n\n    system_prompt = """\nTwoim zadaniem jest wygenerowanie posta na Telegram.\nOdpowiedź ZAWSZE w formacie JSON, zawierającym jeden klucz: \"post\".\nPrzykład: {\"post\": \"Treść Twojego kreatywnego posta tutaj.\"}\n"""
    log.info(f"Generating social message for {target} using Gemini AI via retry handler.")
    response = await gemini_api_call_with_retry(gemini_model, [system_prompt, prompt_text])

    if not response or not response.text:\n        log.warning(f"Gemini API returned no response for social message generation ({target}) after retries.")
        return None

    message = response.text.strip()
    log.info(f"Generated social message for {target}: {message[:70]}...")
    return message

# ---------- PRZEBUDOWANA LOGIKA WYSYŁANIA ----------

async def send_social_telegram_message_async(message_content: str, chat_id: str, button_text: str, button_url: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message_content,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "url": button_url}
                    ]]
                }
            }
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Social message sent: {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false for social message: {body}")
        except Exception as e:
            log.error(f"Telegram send error for social message to {chat_id}: {e}")
    return None

async def send_photo_with_button_async(chat_id: str, photo_url: str, caption: str, button_text: str, button_url: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML", # Caption can be HTML
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "url": button_url}
                    ]]
                }
            }
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Photo sent to {chat_id}: {photo_url}")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false for sendPhoto: {body}")
        except Exception as e:
            log.error(f"Telegram sendPhoto error to {chat_id} (URL: {photo_url}): {e}", exc_info=True)
    return None

async def send_telegram_message_async(message_content: str, link: str, chat_id: str) -> int | None:
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message_content,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "👉 SPRAWDŹ OFERTĘ", "url": link}
                    ]]
                }
            }
            
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Message sent (Markdown): {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false: {body}")
                if body.get("description") and "can't parse entities" in body["description"]:
                    log.warning(f"MARKDOWN PARSE ERROR. Offending text was: \n---\n{message_content}\n---")

        except Exception as e:
            log.error(f"Telegram send error for {link}: {e}", exc_info=True)
    return None

# ---------- ORYGINALNE FUNKCJE (Bez zmian) ----------
def remember_for_deletion(state: Dict[str, Any], chat_id: str, message_id: int, source_url: str):
    log.info(f"DEBUG: remember_for_deletion called. Value of DELETE_AFTER_HOURS: {DELETE_AFTER_HOURS}")
    delete_at = (datetime.now(timezone.utc) + timedelta(hours=DELETE_AFTER_HOURS)).replace(minute=0, second=0, microsecond=0)
    state["delete_queue"].append({ "chat_id": str(chat_id), "message_id": int(message_id), "delete_at": delete_at.isoformat(), "source_url": source_url })

async def sweep_delete_queue(state: Dict[str, Any]) -> int:
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
                    cleaned_from_queue_count += 1
                    log.info(f"Wiadomość {item_id} już nie istniała. Uznaję za posprzątane i usuwam z kolejki.")
                
                elif "message is too old to be deleted" in description or "message can't be deleted" in description:
                    cleaned_from_queue_count += 1
                    log.warning(f"Nie można usunąć wiadomości {item_id}, była za stara (limit 48h). Mimo to usuwam z kolejki.")

                else:
                    final_queue.append(item)
                    log.error(f"Nie udało się usunąć wiadomości {item_id} z powodu błędu API: {res.status_code} {description}. Zostawiam do ponownej próby.")
                continue
            
            final_queue.append(item)
            log.error(f"Server-side error for message {item_id}. Will retry. Status: {res.status_code}, Response: {res.text}")

    total_processed = actually_deleted_count + cleaned_from_queue_count
    items_to_retry = len(process_now) - total_processed

    if total_processed > 0:
        state["delete_queue"] = final_queue
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

async def handle_social_posts(state: Dict[str, Any], current_generation: int):
    if not TELEGRAM_CHANNEL_ID or not TELEGRAM_CHAT_GROUP_ID or not TELEGRAM_CHANNEL_USERNAME:
        log.warning("Skipping social posts: TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_GROUP_ID or TELEGRAM_CHANNEL_USERNAME not set.")
        return

    log.info("Initiating social engagement post sequence.")
    
    channel_msg_raw = await generate_social_message_ai("channel")
    if channel_msg_raw:
        try:
            channel_data = json.loads(channel_msg_raw)
            channel_msg = channel_data.get("post", channel_msg_raw)
        except json.JSONDecodeError:
            channel_msg = channel_msg_raw

        log.info("Sending social channel message with inline button.")
        await send_social_telegram_message_async(
            message_content=channel_msg,
            chat_id=TELEGRAM_CHANNEL_ID,
            button_text="💬 Wejdź na czat",
            button_url=CHAT_CHANNEL_URL or "https://t.me/+iKncwXtipa02MWNk"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    chat_group_msg_raw = await generate_social_message_ai("chat_group")
    if chat_group_msg_raw:
        try:
            chat_group_data = json.loads(chat_group_msg_raw)
            chat_group_msg = chat_group_data.get("post", chat_group_msg_raw)
        except json.JSONDecodeError:
            chat_group_msg = chat_group_msg_raw

        log.info("Sending social chat group message with inline button.")
        await send_social_telegram_message_async(
            message_content=chat_group_msg,
            chat_id=TELEGRAM_CHAT_GROUP_ID,
            button_text="👉 Sprawdź Kanał VIP",
            button_url=f"https://t.me/{TELEGRAM_CHANNEL_USERNAME.lstrip('@')}"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    now_utc = datetime.now(timezone.utc)
    state["last_social_post_time"] = now_utc.isoformat()
    try:
        current_state, current_generation = load_state()
        current_state["last_social_post_time"] = state["last_social_post_time"]
        save_state_atomic(current_state, current_generation)
        log.info(f"Updated last_social_post_time to {state['last_social_post_time']}.")
    except Exception as e:
        log.error(f"Failed to save state after social post: {e}")

# ---------- GŁÓWNA LOGIKA (Używamy ostatniej, prostej wersji) ----------
async def publish_digest_async() -> str:
    log.info("Starting weekly digest generation...")
    state, generation = load_state()

    if not TELEGRAPH_TOKEN:
        log.error("TELEGRAPH_TOKEN is not configured. Cannot publish digest.")
        return "Error: Telegraph token not configured."

    digest_candidates = state.get("digest_candidates", [])
    if not digest_candidates:
        log.info("Digest candidates list is empty. Skipping digest generation.")
        return "Digest candidates list is empty, no digest to generate."

    unique_offers_dict = {}
    for offer in digest_candidates:
        dedup_key = offer.get('dedup_key')
        if not dedup_key: continue
        
        score = int(offer.get('score', 0))
        if dedup_key not in unique_offers_dict or score > int(unique_offers_dict[dedup_key].get('score', 0)):
            unique_offers_dict[dedup_key] = offer
    
    unique_offers = list(unique_offers_dict.values())
    log.info(f"Found {len(digest_candidates)} offers in candidates list, {len(unique_offers)} after deduplication.")

    sorted_by_score = sorted(unique_offers, key=lambda o: int(o.get('score', 0)), reverse=True)
    top_25_offers = sorted_by_score[:25]

    sorted_alphabetically = sorted(top_25_offers, key=lambda o: o.get('title', ''))
    log.info(f"Selected {len(sorted_alphabetically)} offers for the digest.")

    telegraph = Telegraph(TELEGRAPH_TOKEN)
    
    content_html = ""
    for offer in sorted_alphabetically:
        title_for_digest = offer.get('ai_generated_title', offer.get('original_title', 'Brak tytułu'))
        verdict = offer.get('verdict', 'Nieokreślony werdykt')
        market_context = offer.get('market_context', 'Brak szczegółów analizy rynkowej.')
        link = offer.get('link')
        source_name = offer.get('source_name', 'Nieznane')
        
        content_html += f"<h4>{html.escape(title_for_digest)}</h4>"
        content_html += f"<p><b>Werdykt:</b> {html.escape(str(verdict))}</p>"
        content_html += f"<p><i>Analiza:</i> {html.escape(str(market_context))}</p>"
        content_html += f"<p><b>Źródło:</b> {html.escape(source_name)}</p>"
        content_html += f"<p><a href='{html.escape(link)}'>👉 SPRAWDŹ OFERTĘ</a></p>"
        content_html += "<hr/>"

    try:
        page_title = f"Hity Tygodnia: Podsumowanie Ofert ({datetime.now().strftime('%Y-%m-%d')})"
        response = telegraph.create_page(
            title=page_title,
            html_content=content_html,
            author_name="Travel Bot",
        )
        page_url = response['url']
        log.info(f"Successfully created Telegra.ph page: {page_url}")

        engaging_caption = "🔥 <b>GORĄCE HITY TYGODNIA SĄ GOTOWE!</b> 🔥\n\nOto starannie wyselekcjonowane, najlepsze okazje z ostatnich dni. Nie przegap – mogą szybko zniknąć!\n\n<i>Sprawdź, klikając w przycisk poniżej!</i>"
        digest_button_text = "💎 Zobacz Ekskluzywne Hity! 💎"
        
        selected_photo_url = random.choice(DIGEST_IMAGE_URLS)

        sending_tasks = []
        if TELEGRAM_CHANNEL_ID:
            sending_tasks.append(send_photo_with_button_async(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo_url=selected_photo_url,
                caption=engaging_caption,
                button_text=digest_button_text,
                button_url=page_url
            ))
        
        if sending_tasks:
            await asyncio.gather(*sending_tasks)

        state["digest_candidates"] = []
        save_state_atomic(state, generation)
        log.info("Digest candidates list has been cleared and state saved.")
        
        return f"Weekly Digest published successfully: {page_url}"

    except Exception as e:
        log.error(f"Failed to create or publish Telegra.ph page: {e}", exc_info=True)
        return "Error during digest publication."

async def send_promotional_post_async() -> str:
    log.info("Starting promotional post sequence...")
    try:
        state, generation = load_state()
        await handle_social_posts(state, generation)
        log.info("Promotional post sequence completed.")
        return "Promotional post sequence completed."
    except Exception as e:
        log.error(f"Error during promotional post sequence: {e}", exc_info=True)
        return f"Error during promotional post sequence: {e}"


async def master_scheduler():
    now_utc = datetime.now(timezone.utc)
    log.info(f"Master scheduler running at {now_utc.isoformat()}")

    log.info("Scheduler: Kicking off ingestion process.")
    await process_sources_async()
    
    digest_sent = False
    
    if now_utc.hour in [10, 20]:
        log.info(f"Scheduler: It's {now_utc.hour}:00 UTC, publishing digest.")
        await publish_digest_async()
        digest_sent = True
    
    is_promo_time = (now_utc.hour % 2 != 0 and 9 <= now_utc.hour <= 23) or now_utc.hour == 20

    if is_promo_time:
         log.info(f"Scheduler: It's {now_utc.hour}:00 UTC, running promotional post.")
         await send_promotional_post_async() 

    log.info("Master scheduler run finished.")
    return "Scheduler run complete."

async def process_sources_async() -> str:
    log.info("Starting a simple RSS-only processing run...")

    if not TG_TOKEN or not TELEGRAM_CHANNEL_ID: return "Missing critical environment variables."
    state, generation = load_state()

    try:
        fixed_count = sanitizing_startup_check(state)
        if fixed_count > 0:
            log.warning(f"CRITICAL REPAIR: Found and fixed {fixed_count} corrupted entries in state file.")
            try:
                save_state_atomic(state, generation)
                log.info("Successfully saved repaired state. Reloading state to continue run.")
                state, generation = load_state()
            except Exception as e:
                log.critical(f"CRITICAL FAILURE: Could not save repaired state file. Aborting run. Error: {e}")
                return "Critical: State repair failed during save."
    except Exception as e:
        log.error(f"An unexpected error occurred during the sanitizing check: {e}")

    log.info("Running the integrated sweep job at the start of the main run...")
    try:
        deleted_count = await sweep_delete_queue(state)
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

    if MAX_POSTS_PER_RUN > 0: candidates = candidates[:MAX_POSTS_PER_RUN]
    
    if not candidates:
        log.info("No new posts to send. (All posts were duplicates or no posts were found).")
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after pruning: {e}")
        return "Run complete. No new posts."

    log.info(f"Found {len(candidates)} new candidates to process.")

    now_utc = datetime.now(timezone.utc)
    last_analysis_time_str = state.get("last_ai_analysis_time", "1970-01-01T00:00:00Z")
    try:
        last_analysis_time = datetime.fromisoformat(last_analysis_time_str)
    except ValueError:
        log.warning(f"Malformed last_ai_analysis_time in state: {last_analysis_time_str}. Resetting.")
        last_analysis_time = datetime.fromisoformat("1970-01-01T00:00:00Z")

    time_since_last_analysis = now_utc - last_analysis_time
    if time_since_last_analysis < timedelta(minutes=3):
        log.info(f"AI analysis skipped. Last analysis was {time_since_last_analysis.total_seconds():.1f} seconds ago. Need to wait 3 minutes.")
        try:
            save_state_atomic(state, generation)
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after skipping AI analysis: {e}")
        return "Run complete. AI analysis skipped due to 3-minute cooldown."
    
    log.info("Proceeding with AI analysis.")
    state["last_ai_analysis_time"] = now_utc.isoformat()
    
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

    BATCH_SIZE = 5
    candidate_chunks = [detailed_candidates[i:i + BATCH_SIZE] for i in range(0, len(detailed_candidates), BATCH_SIZE)]
    
    all_ai_results = []
    for i, chunk in enumerate(candidate_chunks):
        results = await analyze_batch(chunk)
        all_ai_results.extend(results)
        if i < len(candidate_chunks) - 1:
            wait_time = 1
            log.info(f"Processed chunk {i+1}/{len(candidate_chunks)}. Waiting {wait_time}s before next batch to respect API rate limits.")
            await asyncio.sleep(wait_time)

    if not all_ai_results:
        log.warning("AI analysis returned no results for any batch.")
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info("Successfully saved state after pruning old links.")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after empty AI result: {e}")
        return "Run complete. AI analysis yielded no results."
    
    candidates_by_id = {c['id']: c for c in detailed_candidates}

    sent_count_channel = 0
    sent_count_chat = 0
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    
    for ai_result in all_ai_results:
        result_id = ai_result.get("id")
        if result_id is None: continue

        original_candidate = candidates_by_id.get(result_id)
        if not original_candidate:
            log.warning(f"AI returned a result with ID {result_id} that does not match any original candidate.")
            continue
            
        state["sent_links"][original_candidate['dedup_key']] = now_utc_iso
        
        offer_score = ai_result.get("score", 0)
        offer_title = original_candidate['title']
        content_type = ai_result.get("content_type", "offer")

        if content_type == "news":
            log.info(f"Content '{offer_title[:40]}...' is 'news'. Sending to Chat Group.")
            chat_text = ai_result.get("chat_msg") or f"📰 News: {offer_title}"
            
            chat_message_id = await send_telegram_message_async(
                message_content=chat_text,
                link=original_candidate['link'],
                chat_id=TELEGRAM_CHAT_GROUP_ID
            )
            if chat_message_id:
                sent_count_chat += 1
                if DELETE_AFTER_HOURS > 0:
                    remember_for_deletion(state, TELEGRAM_CHAT_GROUP_ID, chat_message_id, original_candidate['source_url'])
                log.info(f"Successfully sent 'news' item to Chat Group and queued for deletion.")

        elif content_type == "offer":
            if 6 <= offer_score <= 8:
                log.info(f"Offer '{offer_title[:40]}...' (Score: {offer_score}) qualifies for Chat Group.")
                chat_text = ai_result.get("chat_msg") or f"Nowa oferta: {offer_title}"
                
                chat_message_id = await send_telegram_message_async(
                    message_content=chat_text,
                    link=original_candidate['link'],
                    chat_id=TELEGRAM_CHAT_GROUP_ID
                )
                if chat_message_id:
                    sent_count_chat += 1
                    if DELETE_AFTER_HOURS > 0:
                        remember_for_deletion(state, TELEGRAM_CHAT_GROUP_ID, chat_message_id, original_candidate['source_url'])
                    log.info(f"Successfully sent mid-tier offer to Chat Group and queued for deletion.")

            elif offer_score >= 9:
                log.info(f"Offer '{offer_title[:40]}...' (Score: {offer_score}) qualifies for VIP treatment. Auditing with Perplexity...")
                audit_result = await audit_offer_with_perplexity(offer_title, original_candidate.get("description"))

                if audit_result.get("is_active"):
                    log.info(f"Perplexity confirmed offer is active. Verdict: {audit_result.get('verdict')}. Adding to digest candidates.")
                    
                    existing_candidate_keys = {c.get('dedup_key') for c in state["digest_candidates"]}
                    if original_candidate['dedup_key'] not in existing_candidate_keys:
                                            state["digest_candidates"].append({
                                                "original_title": offer_title,
                                                "ai_generated_title": ai_result.get('channel_msg', offer_title),
                                                "link": original_candidate['link'],
                                                "score": offer_score,
                                                "dedup_key": original_candidate['dedup_key'],
                                                "source_name": original_candidate['source_name'],
                                                "market_context": audit_result.get('market_context', 'Brak szczegółów analizy rynkowej.'),
                                                "verdict": audit_result.get('verdict', 'Nieokreślony werdykt.'),
                                            })
                                            log.info(f"Offer '{offer_title[:40]}...' added to digest candidates.")
                    else:
                        log.info(f"Offer '{offer_title[:40]}...' already exists in digest candidates (deduplicated).")
                else:
                    log.warning(f"Perplexity audit failed or offer inactive for '{offer_title[:40]}...'. Demoting to Chat Group.")
                    chat_text = ai_result.get("chat_msg") or f"Nowa oferta: {offer_title}"
                    
                    chat_message_id = await send_telegram_message_async(
                        message_content=chat_text,
                        link=original_candidate['link'],
                        chat_id=TELEGRAM_CHAT_GROUP_ID
                    )
                    if chat_message_id:
                        sent_count_chat += 1
                        if DELETE_AFTER_HOURS > 0:
                            remember_for_deletion(state, TELEGRAM_CHAT_GROUP_ID, chat_message_id, original_candidate['source_url'])
                        log.info(f"Successfully sent demoted VIP offer to Chat Group and queued for deletion.")

        await asyncio.sleep(random.uniform(0.2, 0.5))

    total_sent = sent_count_channel + sent_count_chat
    if total_sent > 0:
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
            log.info(f"Successfully saved state for {total_sent} new items ({sent_count_channel} to channel, {sent_count_chat} to chat).")
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED: {e}")
            return "Critical: State save failed."
            
    return f"Run complete. Found {len(all_posts)} posts, sent {sent_count_channel} to channel and {sent_count_chat} to chat group."


# ---------- FLASK ROUTES ----------
@app.route("/")
def index():
    return "Travel-Bot v6.0 Refactored is running.", 200

@app.route("/run", methods=['POST'])
def run_main_scheduler():
    """Main endpoint to be triggered by an hourly cron job."""
    try:
        result = asyncio.run(master_scheduler())
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        log.exception("Error in /run (master_scheduler) endpoint")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/sweep", methods=['POST'])
def handle_sweep():
    auth_header = request.headers.get("X-Bot-Secret-Token")
    if not TELEGRAM_SECRET or auth_header != TELEGRAM_SECRET:
        return "Unauthorized", 401
    
    state, generation = load_state()
    deleted_count = asyncio.run(sweep_delete_queue(state))
    try:
        save_state_atomic(state, generation) 
        log.info("Stan zapisany po ręcznym zadaniu sweep.")
    except Exception as e:
        log.error(f"Nie udało się zapisać stanu po ręcznym sweep: {e}")

    log.info(f"Ręczne zadanie sweep zakończone. Przetworzono {deleted_count} wiadomości.")
    return jsonify({"status": "ok", "processed_count": deleted_count}), 200

if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
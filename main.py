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
from perplexity import AsyncPerplexity
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
# Usunięto vision_client

# ---------- ENV ----------
def env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)

TG_TOKEN = env("TG_TOKEN")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID") # Renamed from TG_CHAT_ID
TELEGRAM_CHAT_GROUP_ID = env("TELEGRAM_CHAT_GROUP_ID") # New for AI-selected posts
TELEGRAM_CHANNEL_USERNAME = env("TELEGRAM_CHANNEL_USERNAME") # New for linking to channel messages
CHAT_CHANNEL_URL = env("CHAT_CHANNEL_URL") # New for CTA button in VIP messages
GEMINI_API_KEY = env("GEMINI_API_KEY") # New for Gemini AI
PERPLEXITY_API_KEY = env("PERPLEXITY_API_KEY") # New for Perplexity AI audit
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
    return {"sent_links": {}, "delete_queue": [], "last_social_post_time": "1970-01-01T00:00:00Z", "last_ai_analysis_time": "1970-01-01T00:00:00Z"}

def _ensure_state_shapes(state: Dict[str, Any]):
    if "sent_links" not in state: state["sent_links"] = {}
    if "delete_queue" not in state: state["delete_queue"] = []
    if "last_social_post_time" not in state: state["last_social_post_time"] = "1970-01-01T00:00:00Z"
    if "last_ai_analysis_time" not in state: state["last_ai_analysis_time"] = "1970-01-01T00:00:00Z"

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
    
    # Używamy wyrażenia regularnego do bezpiecznego wyciągnięcia ID
    import re
    id_pattern = re.compile(r"^(-?\d+)")

    for item in state.get("delete_queue", []):
        if not isinstance(item, dict) or "chat_id" not in item:
            sanitized_queue.append(item)
            continue

        chat_id = item["chat_id"]
        
        # Sprawdzamy, czy chat_id to string i czy zawiera spację - to sygnatura uszkodzenia
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
            # More robust check for retryable errors from the google-generativeai library
            error_str = str(e).lower()
            if ("429" in error_str and "resource has been exhausted" in error_str) or "503" in error_str or "service unavailable" in error_str:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                    log.warning(f"Rate limit hit or service unavailable on attempt {attempt + 1}/{max_retries}. Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    log.error(f"Gemini API call failed after {max_retries} attempts. Final error: {e}")
                    return None # Failed after all retries
            else:
                log.error(f"Non-retryable Gemini API error: {e}")
                return None # Non-retryable error

    return None # Should be unreachable, but as a fallback


async def audit_offer_with_perplexity(title: str, description: str | None) -> Dict[str, Any]:
    """
    Uses Perplexity API (via SDK) to audit a high-scoring offer.
    Returns a dictionary with 'is_active', 'verdict', etc.
    """
    if not PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'is_active': False, 'verdict': 'SKIPPED', 'market_context': 'Perplexity API key not configured.', 'reason_code': 'NO_API_KEY'}

    # DIAGNOSTIC LOG: Check the length of the key being used.
    log.info(f"DEBUG: Perplexity key read. Length: {len(PERPLEXITY_API_KEY) if PERPLEXITY_API_KEY else 'None / 0'}")

    try:
        client = AsyncPerplexity(api_key=PERPLEXITY_API_KEY)
        
        system_prompt = "Jesteś analitykiem rynku turystycznego. Twoim zadaniem jest błyskawiczna weryfikacja czy podana oferta jest wciąż aktywna. Sprawdź podany link i oceń realną dostępność oferty. Porównaj też jej cenę z aktualnymi warunkami rynkowymi. Odpowiedz ZAWSZE w formacie JSON, zawierającym klucze: 'is_active' (boolean), 'verdict' (string, np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'), 'market_context' (string, krótka analiza), oraz 'reason_code' (string, np. 'ACTIVE_OK', 'EXPIRED', 'API_ERROR')."
        user_prompt = f"Tytuł oferty: {title}\nOpis: {description or 'Brak opisu.'}"

        json_schema = {
            "type": "object",
            "properties": {
                "is_active": {"type": "boolean", "description": "Status aktywności oferty."},
                "verdict": {"type": "string", "description": "Werdykt np. 'SUPER OKAZJA', 'CENA RYNKOWA', 'WYGASŁA'."},
                "market_context": {"type": "string", "description": "Analiza rynkowa/uzasadnienie."},
                "reason_code": {"type": "string", "description": "Kod błędu lub statusu, np. 'ACTIVE_OK', 'EXPIRED', 'API_ERROR'."},
            },
            "required": ["is_active", "verdict", "market_context", "reason_code"]
        }

        response = await client.chat.completions.create(
            model="mistral-7b-instruct",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_schema", "json_schema": {"schema": json_schema}},
        )
        
        # The SDK with json_schema should return a parsed JSON object directly.
        audit_result = json.loads(response.choices[0].message.content)

        log.info(f"Perplexity audit for '{title[:30]}...' successful. Active: {audit_result.get('is_active')}")
        return audit_result

    except Exception as e:
        log.error(f"Perplexity API audit failed for '{title[:30]}...'. Error: {e}", exc_info=True)
        return {'is_active': False, 'verdict': 'ERROR', 'market_context': f'API call failed: {e}', 'reason_code': 'SDK_EXCEPTION'}

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
    • 6: Wystarczająco dobra, żeby wrzucić na czat.
    • 1-5: Przeciętna cena, reklama, spam. (ODRZUĆ, is_good: false).

    KROK 3: GENEROWANIE TREŚCI (Dwa Warianty):
    • 'channel_msg': Styl dziennikarski, informacyjny, konkretne daty, ceny, miejsca docelowe, emoji. Max 200 znaków.
    • 'chat_msg': Styl luźny, "vibe coding", pytanie angażujące społeczność (np. "Kto leci?", "Kto się skusi?"), emoji. Max 150 znaków.
    
    **WAŻNE DLA OFERT SPOZA EUROPY:**
    Jeśli oferta dotyczy regionu spoza Europy (np. Ameryki Północnej, Azji, Afryki, Australii, Ameryki Południowej), nawiąż do tego w wiadomości 'chat_msg' (a czasem też w 'channel_msg'). Dostosuj treść, aby była bardziej relewantna dla potencjalnych odbiorców w tamtym regionie, np. "Dla podróżujących z USA...", "Ktoś w Azji szuka okazji?". Bądź kreatywny.

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
        # This target is no longer used in the new strategy, but we keep the prompt for potential future use.
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
    """Wysyła wiadomość na Telegram z jednym przyciskiem Inline Keyboard."""
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

async def send_telegram_message_async(message_content: str, link: str, host: str, chat_id: str, reply_markup: Dict[str, Any] | None = None) -> int | None:
    async with make_async_client() as client:
        try:
            safe_text = html.escape(message_content, quote=False)

            text = f"{safe_text}\n\n<a href='{link}'>👉 Zobacz ofertę</a>"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Message sent: {message_content[:60]}…")
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

async def handle_social_posts(state: Dict[str, Any], current_generation: int):
    if not TELEGRAM_CHANNEL_ID or not TELEGRAM_CHAT_GROUP_ID or not TELEGRAM_CHANNEL_USERNAME:
        log.warning("Skipping social posts: TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_GROUP_ID or TELEGRAM_CHANNEL_USERNAME not set.")
        return

    # Check for quiet hours (23:00 to 09:00 UTC)
    now_utc = datetime.now(timezone.utc)
    if 23 <= now_utc.hour or now_utc.hour < 9:
        log.info("Skipping social posts during quiet hours (23:00-09:00 UTC).")
        return

    # Check if enough time has passed (2 hours)
    last_post_time_str = state.get("last_social_post_time", "1970-01-01T00:00:00Z")
    try:
        last_post_time = datetime.fromisoformat(last_post_time_str)
    except ValueError:
        log.warning(f"Malformed last_social_post_time in state: {last_post_time_str}. Resetting.")
        last_post_time = datetime.fromisoformat("1970-01-01T00:00:00Z") # Reset to trigger new post

    time_since_last_post = now_utc - last_post_time
    if time_since_last_post < timedelta(hours=2):
        log.info(f"Not yet time for a social post. Last post: {last_post_time_str}. Time since: {time_since_last_post}")
        return

    log.info("Initiating social engagement post sequence.")
    
    # --- Post na Kanał (zachęta do dyskusji na czacie) ---
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
            button_text="💬 Wejdź na czat / Komentarze",
            button_url=CHAT_CHANNEL_URL or "https://t.me/+iKncwXtipa02MWNk" # Fallback link
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    # --- Post na Czat (zachęta do sprawdzania kanału VIP) ---
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
            button_url=f"https://t.me/{TELEGRAM_CHANNEL_USERNAME.replace('@', '')}"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))

    # Update last_social_post_time and save state
    state["last_social_post_time"] = now_utc.isoformat()
    try:
        # We need to reload state/generation to ensure atomic update after previous saves if any
        # This function might be called multiple times in a single run of process_sources_async
        # if there are many offers, so it's safer to load and save its own state changes.
        current_state, current_generation = load_state()
        current_state["last_social_post_time"] = state["last_social_post_time"]
        save_state_atomic(current_state, current_generation)
        log.info(f"Updated last_social_post_time to {state['last_social_post_time']}.")
    except Exception as e:
        log.error(f"Failed to save state after social post: {e}")

# ---------- GŁÓWNA LOGIKA (Używamy ostatniej, prostej wersji) ----------
async def process_sources_async() -> str:
    log.info("Starting a simple RSS-only processing run...")

    if not TG_TOKEN or not TELEGRAM_CHANNEL_ID: return "Missing critical environment variables."
    state, generation = load_state()

    # --- SANITIZING STARTUP CHECK ---
    try:
        fixed_count = sanitizing_startup_check(state)
        if fixed_count > 0:
            log.warning(f"CRITICAL REPAIR: Found and fixed {fixed_count} corrupted entries in state file.")
            try:
                save_state_atomic(state, generation)
                log.info("Successfully saved repaired state. Reloading state to continue run.")
                # Ponownie załaduj stan, aby uzyskać nowy numer generacji i mieć pewność, że wszystko jest czyste
                state, generation = load_state()
            except Exception as e:
                log.critical(f"CRITICAL FAILURE: Could not save repaired state file. Aborting run. Error: {e}")
                return "Critical: State repair failed during save."
    except Exception as e:
        log.error(f"An unexpected error occurred during the sanitizing check: {e}")
    # --- END SANITIZING ---

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

    log.info(f"Found {len(candidates)} new candidates to process.")

    # --- Time-based AI Call Caching ---
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
        # We still save the state because other things like pruning or sweeping might have happened
        try:
            save_state_atomic(state, generation)
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after skipping AI analysis: {e}")
        return "Run complete. AI analysis skipped due to 3-minute cooldown."
    
    log.info("Proceeding with AI analysis.")
    state["last_ai_analysis_time"] = now_utc.isoformat()
    # --- End Time-based Caching ---
    
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
    BATCH_SIZE = 5
    candidate_chunks = [detailed_candidates[i:i + BATCH_SIZE] for i in range(0, len(detailed_candidates), BATCH_SIZE)]
    
    all_ai_results = []
    for i, chunk in enumerate(candidate_chunks):
        results = await analyze_batch(chunk)
        all_ai_results.extend(results)
        # If there are more chunks to process, wait to avoid hitting rate limits.
        if i < len(candidate_chunks) - 1:
            wait_time = 1 # Wait 1 second
            log.info(f"Processed chunk {i+1}/{len(candidate_chunks)}. Waiting {wait_time}s before next batch to respect API rate limits.")
            await asyncio.sleep(wait_time)

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
        
        # --- ŚCIEŻKA 1: Czat Ogólny (oceny 6, 7, 8) ---
        if 6 <= offer_score <= 8:
            log.info(f"Offer '{offer_title[:40]}...' (Score: {offer_score}) qualifies for Chat Group.")
            chat_text = ai_result.get("chat_msg") or f"Nowa oferta: {offer_title}"
            
            chat_message_id = await send_telegram_message_async(
                message_content=chat_text,
                link=original_candidate['link'],
                host=original_candidate['host'],
                chat_id=TELEGRAM_CHAT_GROUP_ID
            )
            if chat_message_id:
                sent_count_chat += 1
                if DELETE_AFTER_HOURS > 0:
                    remember_for_deletion(state, TELEGRAM_CHAT_GROUP_ID, chat_message_id, original_candidate['source_url'])
                log.info(f"Successfully sent to Chat Group and queued for deletion.")

        # --- ŚCIEŻKA 2: Lejek VIP (oceny 9 i 10) ---
        elif offer_score >= 9:
            log.info(f"Offer '{offer_title[:40]}...' (Score: {offer_score}) qualifies for VIP Channel. Auditing with Perplexity...")
            audit_result = await audit_offer_with_perplexity(offer_title, original_candidate.get("description"))

            # --- Jeśli audyt się powiedzie -> Kanał VIP ---
            if audit_result.get("is_active"):
                log.info(f"Perplexity confirmed offer is active. Verdict: {audit_result.get('verdict')}. Posting to VIP Channel.")
                market_context = audit_result.get('market_context', 'Weryfikacja pomyślna.')
                vip_message = f"💎 {offer_title}\n\n✅ **ZWERYFIKOWANO**: {market_context}"
                
                # Przygotowanie przycisku CTA
                cta_button = None
                if CHAT_CHANNEL_URL:
                    cta_button = {
                        "inline_keyboard": [[
                            {"text": "Więcej ofert high-volume znajdziesz na naszym głównym CZACIE!", "url": CHAT_CHANNEL_URL}
                        ]]
                    }

                channel_message_id = await send_telegram_message_async(
                    message_content=vip_message,
                    link=original_candidate['link'],
                    host=original_candidate['host'],
                    chat_id=TELEGRAM_CHANNEL_ID,
                    reply_markup=cta_button
                )
                
                if channel_message_id:
                    sent_count_channel += 1
                    if DELETE_AFTER_HOURS > 0:
                        remember_for_deletion(state, TELEGRAM_CHANNEL_ID, channel_message_id, original_candidate['source_url'])
                    log.info(f"Successfully sent VIP message to Channel and queued for deletion.")
            
            # --- Jeśli audyt się nie powiedzie -> Czat Ogólny (degradacja) ---
            else:
                log.warning(f"Perplexity audit failed or offer inactive for '{offer_title[:40]}...'. Demoting to Chat Group.")
                chat_text = ai_result.get("chat_msg") or f"Nowa oferta: {offer_title}"
                
                chat_message_id = await send_telegram_message_async(
                    message_content=chat_text,
                    link=original_candidate['link'],
                    host=original_candidate['host'],
                    chat_id=TELEGRAM_CHAT_GROUP_ID
                )
                if chat_message_id:
                    sent_count_chat += 1
                    if DELETE_AFTER_HOURS > 0:
                        remember_for_deletion(state, TELEGRAM_CHAT_GROUP_ID, chat_message_id, original_candidate['source_url'])
                    log.info(f"Successfully sent demoted VIP offer to Chat Group and queued for deletion.")

        # Add a small, random delay between each candidate to avoid hitting Telegram's own limits
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
            
    # After all offer processing, run social engagement posts
    try:
        await handle_social_posts(state, generation)
    except Exception as e:
        log.error(f"Error in final social posts handler: {e}")
            
    return f"Run complete. Found {len(all_posts)} posts, sent {sent_count_channel} to channel and {sent_count_chat} to chat group."


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
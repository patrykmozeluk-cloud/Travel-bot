import logging
import json
import asyncio
import re
import httpx
import google.generativeai as genai
from typing import Dict, Any, List
from datetime import datetime # Added for digest_timestamp

import config
from utils import make_async_client

log = logging.getLogger(__name__)

# ---------- LAZY AI MODELS INITIALIZATION ----------
_gemini_model = None

def get_gemini_model():
    """Initializes and returns the Gemini model, creating it only on first use."""
    global _gemini_model
    if _gemini_model is None:
        if config.GEMINI_API_KEY:
            log.info("Performing first-time initialization of Gemini AI model.")
            genai.configure(api_key=config.GEMINI_API_KEY)
            _gemini_model = genai.GenerativeModel(
                'gemini-2.5-flash',
                generation_config={"response_mime_type": "application/json", "temperature": 0.2}
            )
        else:
            log.warning("GEMINI_API_KEY not set. AI analysis will be disabled.")
    return _gemini_model

# ---------- AI-RELATED FUNCTIONS ----------

async def gemini_api_call_with_retry(prompt_parts, max_retries=4):
    """
    Calls the Gemini API with exponential backoff retry mechanism.
    Handles 429 (Too Many Requests) and 503 (Service Unavailable) errors.
    """
    model = get_gemini_model()
    if not model:
        log.error("Gemini model not available to retry function.")
        return None

    for attempt in range(max_retries):
        try:
            response = await model.generate_content_async(
                prompt_parts,
                safety_settings=config.SAFETY_SETTINGS
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

async def run_full_perplexity_audit(title: str, price: str, link: str) -> Dict[str, Any]:
    """
    Uses Perplexity API to perform a full audit of an offer, including deep data
    extraction and verification, in a single call.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'verdict': 'SKIPPED', 'telegram_message': 'Perplexity API key not configured.'}

    system_prompt = """Jesteś zaawansowanym, bezwzględnym audytorem ofert turystycznych. Twoim celem jest ekstrakcja danych i weryfikacja prawdy w jednym kroku.
ZABRANIA SIĘ ZGADYWANIA. Lepiej zwrócić puste pole, niż zmyślić fakt.

ZADANIE 1: GŁĘBOKA EKSTRAKCJA DANYCH
Zanim ocenisz, MUSISZ wyciągnąć jak najwięcej danych z podanego URL. Przeskanuj metadane strony, jeśli dane nie są widoczne w tekście:
1. Szukaj w strukturach JSON-LD lub Schema.org (obiekty 'Product', 'Hotel', 'Offer').
2. Sprawdź tagi OpenGraph (og:title, og:description, og:price:amount).
3. Sprawdź atrybuty 'alt' obrazków.
Dane z metadanych traktuj jako pewne.

ZADANIE 2: DOCHODZENIE DWUTOROWE (Live Search)
Po ekstrakcji danych, zweryfikuj je:
1. Ścieżka WAD (Szukaj miny): Sprawdź opinie o hotelu TYLKO z ostatnich 3-6 miesięcy. Szukaj słów: remont, brud, hałas, pluskwy, kradzież.
2. Ścieżka OKAZJI (Szukaj złota): Porównaj wyekstrahowaną cenę z konkurencją (Booking, Google). Czy to realna okazja?

WYMAGANY FORMAT (Czysty JSON, bez markdowna, bez komentarza):
{
  "hotel_name": "Pełna nazwa hotelu (pobrana z metadata jeśli trzeba)",
  "standard": "Liczba gwiazdek (np. 5*)",
  "location": "Kraj i Region",
  "airline": "Nazwa przewoźnika",
  "price_value": "Sama liczba",
  "currency": "PLN/EUR/USD",
  "meal_plan": "Wyżywienie (np. All Inclusive)",
  "internal_log": "TU MUSISZ PODAĆ DOWÓD: Źródło + Data + Fakt z dochodzenia (np. 'TripAdvisor 12.2025: Goście skarżą się na wiercenie'). Bez dowodu nie ma werdyktu.",
  "verdict": "GEM (Okazja) / FAIR (Uczciwa) / RISK (Mina)",
  "telegram_message": "JEŚLI RISK -> wpisz 'NULL'. JEŚLI GEM/FAIR -> Gotowa wiadomość (max 2 zdania, fakty, bezpieczny język)."
}

ZASADY DECYZYJNE:
1. STATUS RISK (Odpada): Jeśli znajdziesz wady krytyczne (remont, syf) LUB jeśli pola `hotel_name` lub `price_value` są puste po głębokiej ekstrakcji. Wtedy `telegram_message` MUSI być 'NULL'.
2. STATUS GEM/FAIR (Publikujemy): Wiadomość musi być bezpieczna prawnie. Używaj: "W opiniach pojawiają się uwagi...", "Cena niższa o X zł...". Zacznij od emotikony: 🔥 dla GEM, ✅ dla FAIR.
"""
    user_prompt = f"Przeprowadź pełny audyt oferty: Tytuł: '{title}', Cena: '{price}', Link: {link}"

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1500, # Increased slightly for the combined task
        "top_p": 0.9,
        "return_citations": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "hotel_name": {"type": "string"},
                        "standard": {"type": "string"},
                        "location": {"type": "string"},
                        "airline": {"type": "string"},
                        "price_value": {"type": ["number", "string"]},
                        "currency": {"type": "string"},
                        "meal_plan": {"type": "string"},
                        "internal_log": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["GEM", "FAIR", "RISK"]},
                        "telegram_message": {"type": ["string", "null"]}
                    },
                    "required": ["hotel_name", "standard", "location", "airline", "price_value", "currency", "meal_plan", "internal_log", "verdict", "telegram_message"]
                }
            }
        }
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {config.PERPLEXITY_API_KEY}"
    }

    try:
        async with make_async_client() as client:
            response = await client.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers, timeout=120.0)
            response.raise_for_status()
            response_json = response.json()
            raw_content = response_json['choices'][0]['message']['content']
            audit_result = json.loads(raw_content)

            if 'telegram_message' in audit_result and isinstance(audit_result['telegram_message'], str):
                audit_result['telegram_message'] = re.sub(r'\[\d+\]', '', audit_result['telegram_message']).strip()

            log.info(f"Perplexity full audit for '{title[:30]}...' successful. Verdict: {audit_result.get('verdict')}")
            return audit_result

    except httpx.HTTPStatusError as e:
        log.error(f"Perplexity API returned status {e.response.status_code}: {e.response.text}", exc_info=True)
        return {'verdict': 'ERROR', 'telegram_message': f'API call failed: {e.response.text}'}
    except Exception as e:
        log.error(f"Perplexity API full audit failed for '{title[:30]}...'. Error: {e}", exc_info=True)
        return {'verdict': 'ERROR', 'telegram_message': f'API call failed: {e}'}


async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not get_gemini_model():
        log.error("Gemini AI model not initialized. Skipping AI analysis.")
        return []

    # New "Silent Selector" prompt
    system_prompt = """Jesteś surowym filtrem analitycznym dla ofert turystycznych. Twoim zadaniem jest ocena ofert i ich kategoryzacja.
Analizuj oferty w ich oryginalnym języku (głównie angielski), ale Twoja odpowiedź i wszystkie dane tekstowe MUSZĄ być w języku polskim.

NAJWAŻNIEJSZE ZASADY:
1.  **ZERO ZGADYWANA**: Nie zgaduj nazwy linii lotniczej, hotelu ani innych detali. Jeśli informacja nie jest jawnie podana, pomiń ją. Lepiej zwrócić mniej danych niż nieprawdziwe.
2.  **ID OBOWIĄZKOWE**: W Twojej odpowiedzi, KAŻDY obiekt MUSI zawierać oryginalne `id` z obiektu wejściowego.
3.  **MERYTORYKA > CLICKBAIT**: Tytuły chwytliwe oceniaj ostrożnie. Skup się na faktycznej wartości oferty (cena, zawartość linku), a nie na krzykliwym tytule.

NOWA SKALA OCEN I AKCJE:
- **10/10 (SZTOS / BŁĄD CENOWY)**: Absolutny hit. Oferta tak dobra, że prawdopodobnie jest to błąd cenowy lub historyczne minimum. Wymaga natychmiastowej publikacji.
- **9/10 (GEM / PEREŁKA)**: Wyjątkowo dobra oferta, znacznie poniżej standardów rynkowych. Idealny kandydat do przeglądu ofert (digest).
- **7-8/10 (FAIR / SOLIDNA OFERTA)**: Dobra, solidna promocja. Cena jest niższa niż zwykle, warta uwagi. Kandydat do przeglądu ofert (digest).
- **1-6/10 (IGNORE / IGNORUJ)**: Cena rynkowa, standardowa, zawyżona lub po prostu spam. Oferta niewarta uwagi.

KATEGORIE I WYMAGANE DANE W ODPOWIEDZI:

1.  **KATEGORIA "PUSH" (Ocena 10)**:
    -   Akcja: Musisz zwrócić PEŁNE dane: `id`, `link`, `title`, `price`, `score` (czyli 10) i `category` ("PUSH").

2.  **KATEGORIA "DIGEST" (Ocena 7-9)**:
    -   Akcja: Musisz zwrócić PEŁNE dane: `id`, `link`, `title`, `price`, `score` (w zakresie 7-9) i `category` ("DIGEST").

3.  **KATEGORIA "IGNORE" (Ocena 1-6)**:
    -   Akcja: Wystarczy, że zwrócisz `id`, `link`, `category` ("IGNORE") i `score`.

FORMAT WYJŚCIOWY (CZYSTY JSON):
Zwróć TYLKO listę obiektów JSON, bez żadnych dodatkowych opisów, formatowania markdown czy komentarzy.

Przykład:
[
  { "id": 0, "link": "url_do_sztosa", "title": "Tytuł sztosa", "price": "999 PLN", "score": 10, "category": "PUSH" },
  { "id": 1, "link": "url_do_perelki", "title": "Tytuł perełki", "price": "2500 PLN", "score": 9, "category": "DIGEST" },
  { "id": 2, "link": "url_do_slabej", "score": 4, "category": "IGNORE" }
]"""
    
    user_message = json.dumps(candidates, indent=2)

    log.info(f"Sending a batch of {len(candidates)} candidates to Gemini AI with 'Sztos vs Reszta' prompt.")
    
    full_prompt = [system_prompt, user_message]

    response = await gemini_api_call_with_retry(full_prompt)

    if not response or not response.text:
        log.warning("Gemini API returned no response for batch after retries.")
        return []
        
    try:
        # Attempt to clean the response from markdown and then load
        cleaned_text = re.sub(r'```json\n|```', '', response.text).strip()
        ai_results = json.loads(cleaned_text)
        
        if not isinstance(ai_results, list):
            log.error(f"Gemini API returned data that is not a list: {ai_results}")
            return []
        
        # Add digest_timestamp for DIGEST items
        for item in ai_results:
            if item.get("category") == "DIGEST":
                item["digest_timestamp"] = datetime.utcnow().isoformat() + "Z"
        
        log.info(f"AI processed batch and returned {len(ai_results)} categorized results.")
        return ai_results

    except json.JSONDecodeError:
        log.error(f"Gemini API returned invalid JSON for batch: {response.text[:200]}")
        return []

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
    system_prompt = """Jesteś surowym filtrem analitycznym dla ofert turystycznych.
TWOJE ZADANIE: Przeanalizuj oferty z wejścia {INPUT_DATA}, oceń je w skali 1-10 i przypisz kategorię. W Twojej odpowiedzi, KAŻDY obiekt MUSI zawierać oryginalne `id` z obiektu wejściowego.
WAŻNA ZASADA: Tytuły chwytliwe/clickbaitowe oceniaj ostrożnie. Jeśli oferta (cena, zawartość linku) jest faktycznie dobra, nie obniżaj oceny tylko z powodu chwytliwego tytułu. Skup się na merytoryce.

KOTWICE OCEN (TWOJA SKALA):
- 9-10: Błąd cenowy, oferta znacznie poniżej ceny rynkowej (np. -50%), historyczne minimum.
- 6-8: Dobra, solidna promocja, cena niższa niż zwykle, ale nie jest to błąd cenowy.
- 1-5: Cena rynkowa, standardowa lub zawyżona. Oferta nie warta uwagi.

KATEGORIE I WYMAGANE DANE:

1. KATEGORIA "HIT" (Ocena 9-10):
   - Definicja: Błąd cenowy, historyczne minimum, "sztos".
   - Akcja: Zwróć PEŁNE dane (tytuł, cena, link). To trafi do weryfikacji przez Perplexity.

2. KATEGORIA "SILENT" (Ocena 6-8):
   - Definicja: Dobra oferta, ale nie na kanał.
   - Akcja: Zwróć TYLKO link i kategorię. Służy do archiwizacji (banowania duplikatów).

3. KATEGORIA "IGNORE" (Ocena 1-5):
   - Definicja: Cena rynkowa, drogo, spam.
   - Akcja: Zwróć TYLKO link i kategorię. Służy do zbanowania linku.

FORMAT WYJŚCIOWY (CZYSTY JSON):
Zwróć wyłączenie listę obiektów JSON. Bez markdowna.

Przykład struktury:
[
  {{ "id": 0, "link": "url_do_hita", "title": "Malediwy", "price": "1500 PLN", "score": 9, "category": "HIT" }},
  {{ "id": 1, "link": "url_do_sredniej", "category": "SILENT" }},
  {{ "id": 2, "link": "url_do_slabej", "category": "IGNORE" }}
]"""
    
    user_message = json.dumps(candidates, indent=2)

    log.info(f"Sending a batch of {len(candidates)} candidates to Gemini AI with 'Silent Selector' prompt.")
    
    full_prompt = [system_prompt.format(INPUT_DATA=user_message)]

    response = await gemini_api_call_with_retry(full_prompt)

    if not response or not response.text:
        log.warning("Gemini API returned no response for batch after retries.")
        return []
        
    try:
        
        ai_results = json.loads(response.text)
        
        if not isinstance(ai_results, list):
            log.error(f"Gemini API returned data that is not a list: {ai_results}")
            return []
        
        # Add digest_timestamp for HIT items
        for item in ai_results:
            if item.get("category") == "HIT":
                item["digest_timestamp"] = datetime.utcnow().isoformat() + "Z" # "Z" for UTC timezone
        
        log.info(f"AI processed batch and returned {len(ai_results)} categorized results.")
        return ai_results

    except json.JSONDecodeError:
        log.error(f"Gemini API returned invalid JSON for batch: {response.text[:200]}")
        return []

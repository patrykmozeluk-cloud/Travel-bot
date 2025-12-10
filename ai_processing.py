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

async def extract_offer_data_with_perplexity(link: str) -> Dict[str, Any]:
    """
    Uses Perplexity API to extract detailed data from an offer URL, including from metadata.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform data extraction.")
        return {'verdict': 'SKIPPED', 'reasoning': 'Perplexity API key not configured.'}

    system_prompt = """Jesteś zaawansowanym analitykiem ofert turystycznych. Twoim celem jest wyekstrahowanie twardych danych do pliku JSON.

ZADANIE:
Przeanalizuj podany URL pod kątem atrakcyjności oferty turystycznej.

INSTRUKCJA KRYTYCZNA (Omijanie "ślepoty" na zdjęcia):
Wiele stron biur podróży (TUI, Itaka, Wakacje.pl) ukrywa cenę i nazwę hotelu na zdjęciach, których Ty nie widzisz jako tekst.
JEDNAK te dane ZAWSZE znajdują się w kodzie źródłowym strony dla robotów Google (SEO).

Zanim napiszesz "Brak danych", MUSISZ przeskanować metadane strony:
1. Szukaj w strukturach **JSON-LD** lub **Schema.org** (obiekty typu 'Product', 'Hotel', 'Offer').
2. Sprawdź tagi **OpenGraph** (og:title, og:description, og:price:amount).
3. Sprawdź atrybuty **alt** obrazków.

Jeśli znajdziesz dane w kodzie/metadanych, traktuj je jako PEWNE i wpisz do raportu.

FORMAT WYJŚCIOWY (Czysty JSON, bez markdowna, bez komentarza):
{
  "hotel_name": "Pełna nazwa hotelu (jeśli brak w tekście, pobierz z metadata)",
  "standard": "Liczba gwiazdek (np. 5*)",
  "location": "Kraj i Region",
  "airline": "Nazwa przewoźnika (szukaj w sekcji flight details lub metadata)",
  "price_value": "Sama liczba",
  "currency": "PLN/EUR/USD",
  "meal_plan": "Wyżywienie (np. All Inclusive)",
  "verdict": "SUPER_OKAZJA / DOBRA_OFERTA / STANDARD",
  "reasoning": "Krótkie uzasadnienie w 1 zdaniu (np. Hotel 5* w cenie 3*)"
}

ZASADY OCENY (LOGIKA DEGRADACJI):
1. Jeśli hotel ma 5* lub 4* i cenę znacznie poniżej rynkowej -> SUPER_OKAZJA.
2. Jeśli znalazłeś dane w metadanych (ukryte dla oka, widoczne dla SEO) -> NIE degraduj oferty. Oceń ją normalnie.
3. Jeśli mimo skanowania kodu pola są puste -> Dopiero wtedy oznacz jako STANDARD i wpisz w reasoning "Brak kluczowych danych w ofercie".
"""
    user_prompt = f"Wyekstrahuj dane z oferty pod tym linkiem: {link}"

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
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
                        "price_value": {"type": ["number", "string"]}, # allow string for "brak" or similar
                        "currency": {"type": "string"},
                        "meal_plan": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["SUPER_OKAZJA", "DOBRA_OFERTA", "STANDARD"]},
                        "reasoning": {"type": "string"}
                    },
                    "required": ["hotel_name", "standard", "location", "airline", "price_value", "currency", "meal_plan", "verdict", "reasoning"]
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
            extraction_result = json.loads(raw_content)

            log.info(f"Perplexity data extraction for '{link}' successful. Verdict: {extraction_result.get('verdict')}")
            return extraction_result

    except httpx.HTTPStatusError as e:
        log.error(f"Perplexity API returned status {e.response.status_code}: {e.response.text}", exc_info=True)
        return {'verdict': 'ERROR', 'reasoning': f'API call failed: {e.response.text}'}
    except Exception as e:
        log.error(f"Perplexity API data extraction failed for '{link}'. Error: {e}", exc_info=True)
        return {'verdict': 'ERROR', 'reasoning': f'API call failed: {e}'}


async def audit_offer_with_perplexity(extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Uses Perplexity API to audit a high-scoring offer and return structured data.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'verdict': 'SKIPPED', 'analysis': 'Perplexity API key not configured.'}

    system_prompt = """Jesteś Bezwzględnym Audytorem Faktów (Fact-Checker).
Twoim celem jest weryfikacja prawdy w Live Search.
ZABRANIA SIĘ ZGADYWANIA. Lepiej odrzucić ofertę, niż zmyślić fakt.

ZADANIE - DOCHODZENIE DWUTOROWE:
1. Ścieżka WAD (Szukaj miny): Sprawdź opinie TYLKO z ostatnich 3-6 miesięcy. Szukaj słów: remont, brud, hałas, pluskwy, kradzież.
2. Ścieżka OKAZJI (Szukaj złota): Porównaj cenę z konkurencją (Booking, Google). Czy to realna okazja?

WYMAGANY FORMAT (Czysty JSON):
{
  "hotel_name": "Pełna nazwa",
  "internal_log": "TU MUSISZ PODAĆ DOWÓD: Źródło + Data + Fakt (np. 'TripAdvisor 12.2024: Goście skarżą się na wiercenie'). Bez dowodu nie ma werdyktu.",
  "verdict": "GEM (Okazja) / FAIR (Uczciwa) / RISK (Mina)",
  "telegram_message": "JEŚLI RISK -> wpisz 'NULL'. JEŚLI GEM/FAIR -> Gotowa wiadomość (max 2 zdania, fakty, bezpieczny język)."
}

ZASADY DECYZYJNE:
1. STATUS RISK (Odpada):
   - Jeśli znajdziesz wady krytyczne (remont, syf) lub brak świeżych opinii.
   - Wtedy `telegram_message` MUSI być 'NULL'. Nie publikujemy tego.

2. STATUS GEM/FAIR (Publikujemy):
   - Wiadomość musi być bezpieczna prawnie.
   - NIE UŻYWAJ słów: oszustwo, złodzieje.
   - UŻYWAJ: "W opiniach pojawiają się uwagi...", "Cena niższa o X zł...".
   - Zacznij od emotikony: 🔥 dla GEM, ✅ dla FAIR.

Pamiętaj: Halucynacja to porażka. Bądź precyzyjny.
"""
    user_prompt = f"Zweryfikuj ofertę, używając poniższych, wstępnie wyekstrahowanych danych: {json.dumps(extracted_data, ensure_ascii=False)}"

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,       # Betonowa precyzja
        "max_tokens": 1024,       # Limit wystarczający na analizę, ale bez lania wody
        "top_p": 0.9,
        "return_citations": True, # Wymusza podawanie źródeł
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "hotel_name": {"type": "string"},
                        "internal_log": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["GEM", "FAIR", "RISK"]},
                        "telegram_message": {"type": ["string", "null"]}
                    },
                    "required": ["hotel_name", "internal_log", "verdict", "telegram_message"]
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

            # --- NOWOŚĆ: Usunięcie przypisów typu [1], [2] z analizy ---
            if 'telegram_message' in audit_result and isinstance(audit_result['telegram_message'], str):
                audit_result['telegram_message'] = re.sub(r'\[\d+\]', '', audit_result['telegram_message']).strip()
            # -------------------------------------------------------------

            log.info(f"Perplexity audit for '{extracted_data.get('hotel_name', extracted_data.get('link'))}' successful. Verdict: {audit_result.get('verdict')}")
            return audit_result

    except httpx.HTTPStatusError as e:
        log.error(f"Perplexity API returned status {e.response.status_code}: {e.response.text}", exc_info=True)
        return {'verdict': 'ERROR', 'analysis': f'API call failed: {e.response.text}'}
    except Exception as e:
        log.error(f"Perplexity API audit failed for '{title[:30]}...'. Error: {e}", exc_info=True)
        return {'verdict': 'ERROR', 'analysis': f'API call failed: {e}'}

async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not get_gemini_model():
        log.error("Gemini AI model not initialized. Skipping AI analysis.")
        return []

    # New "Silent Selector" prompt
    system_prompt = """Jesteś surowym filtrem analitycznym dla ofert turystycznych.
TWOJE ZADANIE: Przeanalizuj oferty z wejścia {INPUT_DATA}, oceń je w skali 1-10 i przypisz kategorię. W Twojej odpowiedzi, KAŻDY obiekt MUSI zawierać oryginalne `id` z obiektu wejściowego.
WAŻNA ZASADA: Tytuły chwytliwe/clickbaitowe oceniaj ostrożnie. Jeśli oferta (cena, zawartość linku) jest faktycznie dobra, nie obniżaj oceny tylko z powodu chwytliwego tytułu. Skup się na merytoryce.

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

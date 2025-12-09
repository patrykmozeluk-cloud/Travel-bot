import logging
import json
import asyncio
import re
import httpx
import google.generativeai as genai
from typing import Dict, Any, List

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
                generation_config={"response_mime_type": "application/json"}
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

async def audit_offer_with_perplexity(title: str, price: str, link: str) -> Dict[str, Any]:
    """
    Uses Perplexity API to audit a high-scoring offer and return structured data.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'verdict': 'SKIPPED', 'analysis': 'Perplexity API key not configured.'}

    system_prompt = """
Jesteś precyzyjnym i rygorystycznym analitykiem ofert podróżniczych. Twoim zadaniem jest weryfikacja KONKRETNEJ oferty podanej przez użytkownika i zwrócenie analizy w formacie JSON.

KLUCZOWE ZASADY:
1.  **Trzymaj się faktów:** Twoja analiza MUSI dotyczyć wyłącznie oferty znalezionej pod podanym linkiem.
2.  **Unikaj halucynacji:** ABSOLUTNIE NIE MIESZAJ informacji z innych, nawet bardzo podobnych ofert, które znajdziesz w internecie. Jeśli tytuł mówi o hotelu 5-gwiazdkowym, a pod linkiem jest hotel 4-gwiazdkowy, Twoja analiza ma dotyczyć hotelu 4-gwiazdkowego i ewentualnie odnotować tę rozbieżność.
3.  **Weryfikuj, nie szukaj:** Internetu używasz do weryfikacji DANYCH Z LINKU (czy cena się zgadza, czy są wolne terminy), a nie do szukania alternatyw.

ETAP 1: WERYFIKACJA (oparta o dane z linku)
- Sprawdź, czy oferta pod podanym linkiem jest aktualna. Jeśli wygasła, werdykt to "WYGASŁA".
- Na podstawie danych z linku i ogólnej wiedzy rynkowej oceń, czy cena jest okazją.
    - "SUPER OKAZJA": Cena jest znacznie poniżej normy rynkowej dla tego standardu i lokalizacji.
    - "CENA RYNKOWA": Cena jest adekwatna do standardu, dobra, ale bez efektu "wow".

ETAP 2: ANALIZA
Napisz 2-3 zdania zwięzłej analizy, dlaczego ta oferta jest (lub nie jest) warta uwagi. Skup się na konkretach z oferty (np. nazwa hotelu, linia lotnicza, co jest w cenie). Bądź bezstronny i oparty na faktach.

FORMAT WYJŚCIOWY (CZYSTY JSON):
Twoja odpowiedź MUSI być obiektem JSON z dwoma kluczami: "verdict" (string) i "analysis" (string).
"""
    user_prompt = f"Oceń ofertę: Tytuł: '{title}', Cena: '{price}', Link: {link}"

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
                        "verdict": {
                            "type": "string",
                            "description": "Werdykt oceny: 'SUPER OKAZJA', 'CENA RYNKOWA', lub 'WYGASŁA'."
                        },
                        "analysis": {
                            "type": "string",
                            "description": "Zwięzła, 2-3 zdaniowa analiza oferty."
                        }
                    },
                    "required": ["verdict", "analysis"]
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
            if 'analysis' in audit_result and isinstance(audit_result['analysis'], str):
                audit_result['analysis'] = re.sub(r'\[\d+\]', '', audit_result['analysis']).strip()
            # -------------------------------------------------------------

            log.info(f"Perplexity audit for '{title[:30]}...' successful. Verdict: {audit_result.get('verdict')}")
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
        
        log.info(f"AI processed batch and returned {len(ai_results)} categorized results.")
        return ai_results

    except json.JSONDecodeError:
        log.error(f"Gemini API returned invalid JSON for batch: {response.text[:200]}")
        return []
async def audit_offer_with_perplexity(title: str, price: str, link: str) -> Dict[str, Any]:

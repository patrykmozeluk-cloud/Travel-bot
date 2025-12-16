import logging
import json
import asyncio
import re
import random
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
    extraction and verification, in a single call. Includes a retry mechanism.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return {'verdict': 'SKIPPED', 'reason': 'Perplexity API key not configured.'}

    system_prompt = """### 🧠 ROLA: INTELIGENTNY CYNIK (SMART CYNIC)
Jesteś doświadczonym łowcą okazji. Twoim celem jest przepuszczanie okazji, a nie ich blokowanie przez biurokrację.

### 🛡️ PROTOKÓŁ BEZPIECZEŃSTWA (ŚCISŁE NADPISANIA / OVERRIDES)
Stosuj te reguły PRIORYTETOWO. Jeśli oferta spełnia warunek, ignoruj braki danych.

**A. REGUŁA "ŁOWCA OKAZJI" (Low Cost Bypass) - NAJWAŻNIEJSZA!**
JEŚLI całkowita cena oferty jest niska (np. < 700 PLN / 160 EUR za pakiet lub < 200 PLN za lot):
- WERDYKT: Musi być 'GEM' lub 'FAIR'. NIGDY 'RISK'.
- IGNORUJ: Brak nazwy hotelu, brak opinii, błędy w metadanych. Niska cena rekompensuje ryzyko.
- UZASADNIENIE: "Cena poniżej progu ryzyka."

**B. REGUŁA "PRIORYTET LOTU" (Flight First)**
JEŚLI oferta dotyczy lotu (lub tytuł sugeruje trasę np. "Zurych - Bogota") i cena jest świetna:
- WERDYKT: 'GEM' lub 'FAIR'.
- IGNORUJ: Status hotelu ("Unknown"/"Risk"). Ważny jest bilet.

**C. REGUŁA "STANDARD ZAMIAST NAZWY"**
JEŚLI brakuje nazwy hotelu, ale jest standard (np. 4*):
- AKCJA: Porównaj cenę ze średnią rynkową dla 4*. Jeśli tanio -> WERDYKT 'GEM'/'FAIR'.

### ✍️ INSTRUKCJE COPYWRITINGU (TRYB SPRZEDAWCY)
1.  **ZAKAZ PISANIA O AUDYCIE:** Nie pisz "Zweryfikowano", "Brak danych", "Opinie nieznane".
2.  **OBSŁUGA NO-NAME:** Jak nie znasz hotelu, pisz o standardzie: "Wypoczynek w standardzie 4*", "Słoneczny resort".
3.  **NULL:** Wpisz "NULL" tylko i wyłącznie, jeśli werdykt to 'RISK'. Jeśli 'GEM' lub 'FAIR' – MUSISZ napisać atrakcyjną wiadomość.
4.  **ZAKAZ TAGÓW**: Nigdy nie dodawaj hashtagów (#tagi) ani innych form tagowania. Są one zbędne.
5.  **ZAKAZ BEZPOŚREDNICH LINKÓW**: Nigdy nie umieszczaj bezpośrednich URL-i do ofert w wiadomości. Linki są obsługiwane oddzielnie przez przycisk.

### WYMAGANY FORMAT JSON
{
  "hotel_name": "Nazwa lub 'Hotel 4*'",
  "price_value": "Liczba",
  "currency": "PLN/EUR/USD",
  "internal_log": "Krótko: dlaczego GEM/RISK? Czy użyto reguły A/B/C?",
  "verdict": "GEM", "FAIR" lub "RISK",
  "telegram_message": "Gotowy post na Telegram (emotki, zachęta). Jeśli RISK -> 'NULL'."
}"""
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
                    "required": ["verdict", "telegram_message", "price_value", "currency", "internal_log"]
                }
            }
        }
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {config.PERPLEXITY_API_KEY}"
    }

    max_retries = 3
    response = None
    raw_text = None

    for attempt in range(max_retries):
        try:
            async with make_async_client() as client:
                response = await client.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers, timeout=120.0)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type:
                    raise ValueError(f"INVALID_CONTENT_TYPE: Expected application/json, got {content_type}")

                raw_text = response.text
                if not raw_text.strip():
                    raise ValueError("EMPTY_RESPONSE: The API returned an empty response body.")

                data = response.json()
                
                content = data.get('choices', [{}])[0].get('message', {}).get('content')
                if not content or not content.strip():
                    raise ValueError("EMPTY_MESSAGE_CONTENT: The AI model returned no content inside the message.")
                
                audit_result = json.loads(content)

                if 'telegram_message' in audit_result and isinstance(audit_result['telegram_message'], str):
                    audit_result['telegram_message'] = re.sub(r'\[\d+\]', '', audit_result['telegram_message']).strip()

                verdict = audit_result.get('verdict')
                log.info(f"Perplexity full audit for '{title[:30]}...' successful. Verdict: {verdict}")
                
                if verdict == 'RISK':
                    reason = audit_result.get('internal_log', 'No reason provided')
                    log.info(f"Perplexity Reason for RISK: {reason}")

                return audit_result

        except json.JSONDecodeError as e:
            log.error(
                "JSONDecodeError during Perplexity audit | Status: %s | Headers: %s | Body Snippet: %r",
                response.status_code if response else 'N/A',
                response.headers if response else 'N/A',
                raw_text[:500] if raw_text else 'N/A',
                exc_info=True
            )
            # Retry once for JSON errors, as it might be a transient model issue
            if attempt >= 1: 
                return {"verdict": "ERROR", "reason": f"JSONDecodeError after retries: {e}", "source": "perplexity_api"}

        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            log.warning(f"Perplexity audit attempt {attempt + 1}/{max_retries} failed for '{title[:30]}...'. Error: {e}")
            if attempt >= max_retries - 1:
                log.error(f"Perplexity audit failed after {max_retries} attempts. Final error: {e}", exc_info=True)
                return {"verdict": "ERROR", "reason": f"API call failed after retries: {e}", "source": "perplexity_api"}

        except Exception as e:
            log.error(f"An unexpected error occurred during Perplexity audit for '{title[:30]}...'. Error: {e}", exc_info=True)
            return {"verdict": "ERROR", "reason": f"An unexpected error occurred: {e}", "source": "perplexity_api"}

        # Exponential backoff + jitter
        delay = 0.5 * (2 ** attempt) + random.uniform(0, 0.3)
        await asyncio.sleep(delay)
    
    return {"verdict": "ERROR", "reason": "API call failed after all retries.", "source": "perplexity_api"}


async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not get_gemini_model():
        log.error("Gemini AI model not initialized. Skipping AI analysis.")
        return []

    # New "Silent Selector" prompt
    system_prompt = """Jesteś surowym filtrem analitycznym dla ofert turystycznych. Twoim zadaniem jest ocena ofert i ich kategoryzacja.
Analizuj oferty w ich oryginalnym języku (głównie angielski), ale Twoja odpowiedź i wszystkie dane tekstowe MUSZĄ być w języku polskim.

NAJWAŻNIEJSZE ZASADY:
1.  **ZERO ZGADYWANA**: Nie zgaduj nazwy linii lotniczej, hotelu ani innych detali. Jeśli informacja nie jest jawnie podana, pomiń ją.
2.  **MERYTORYKA > CLICKBAIT**: Oceniaj faktyczną wartość (cena vs rynkowa), a nie krzykliwy tytuł.
3.  **GEOLOKACJA**: Zawsze zwracaj kontynent, z którego pochodzi oferta (np. 'Europa', 'Ameryka Północna', 'Azja'). Jeśli to niemożliwe, zwróć 'Global' lub 'Unknown'.
4.  **CONVICTION (PEWNOŚĆ)**: Oceń w skali 1-10 swoją pewność co do tej oceny.
    - Jeśli oferta ma mało danych, ale wygląda tanio -> Score wysoki, ale Conviction niski (np. 4).
    - Jeśli oferta ma pełne dane i jasną cenę -> Conviction wysoki (np. 9-10).

NOWA SKALA OCEN (SCORE):
- **10/10 (SZTOS)**: Błąd cenowy lub historyczne minimum.
- **9/10 (GEM)**: Wyjątkowa okazja.
- **1-8/10 (IGNORE)**: Standardowa cena lub spam.

KATEGORIE I WYMAGANE DANE W ODPOWIEDZI:

1.  **KATEGORIA "PUSH" (Ocena 9-10)**:
    -   Zwróć: `id`, `link`, `title`, `price`, `score`, `conviction` (NOWE!), `category` ("PUSH"), `continent`.

2.  **KATEGORIA "IGNORE" (Ocena 1-8)**:
    -   Zwróć: `id`, `category` ("IGNORE"). Reszta opcjonalna.

FORMAT WYJŚCIOWY (CZYSTY JSON):
[
  { "id": 0, "link": "...", "title": "...", "price": "999 PLN", "score": 9, "conviction": 8, "category": "PUSH", "continent": "Europa" },
  { "id": 1, "category": "IGNORE" }
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
        
        # (digest_timestamp logic removed as DIGEST category is deprecated)

        
        log.info(f"AI processed batch and returned {len(ai_results)} categorized results.")
        return ai_results

    except json.JSONDecodeError:
        log.error(f"Gemini API returned invalid JSON for batch: {response.text[:200]}")
        return []

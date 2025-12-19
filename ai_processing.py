import logging
import json
import asyncio
import re
import random
import httpx
from google import genai
from typing import Dict, Any, List
from datetime import datetime # Added for digest_timestamp

import config
from utils import make_async_client

log = logging.getLogger(__name__)

# ---------- LAZY AI MODELS INITIALIZATION ----------
_gemini_client = None

def get_gemini_client():
    """Initializes and returns the Gemini client, creating it only on first use."""
    global _gemini_client
    if _gemini_client is None:
        if config.GEMINI_API_KEY:
            log.info("Performing first-time initialization of Gemini AI client.")
            _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
        else:
            log.warning("GEMINI_API_KEY not set. AI analysis will be disabled.")
    return _gemini_client

# ---------- AI-RELATED FUNCTIONS ----------

async def gemini_api_call_with_retry(prompt_parts, max_retries=4):
    """
    Calls the Gemini API with exponential backoff retry mechanism.
    Handles 429 (Too Many Requests) and 503 (Service Unavailable) errors.
    """
    client = get_gemini_client()
    if not client:
        log.error("Gemini client not available to retry function.")
        return None

    for attempt in range(max_retries):
        try:
            response = await client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt_parts,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                    "safety_settings": config.SAFETY_SETTINGS
                }
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

async def run_batch_perplexity_audit(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Uses Perplexity API to perform a batch audit of up to 3 offers in a single request.
    Uses an "Anti-Lazy" prompt to force independent searches.
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return [{'verdict': 'SKIPPED', 'reason': 'Perplexity API key not configured.', 'id': c.get('id')} for c in batch]

    # Construct the user prompt with the list of offers
    offers_text = ""
    for i, item in enumerate(batch):
        offers_text += f"\n--- OFERTA {i+1} (ID: {item.get('id')}) ---\nTytuł: {item.get('title')}\nCena: {item.get('price', 'N/A')}\nLink: {item.get('link')}\n"

    system_prompt = """### 🧠 ROLA: EKSPERT-SPRZEDAWCA (TRYB BATCH)
Otrzymujesz listę max 3 ofert turystycznych. Twoim zadaniem jest ich audyt i przygotowanie wpisów sprzedażowych.

⚠️ **INSTRUKCJE KRYTYCZNE (STOSUJ DO KAŻDEJ OFERTY):**
1. **NIEZALEŻNOŚĆ:** Dla KAŻDEJ z ofert wykonaj OSOBNE, NIEZALEŻNE wyszukiwanie w internecie. Nie łącz faktów, nie szukaj części wspólnych. Traktuj każdą ofertę jako oddzielne, unikalne zadanie.
2. **PRIORYTET FAKTÓW:** Ściśle weryfikuj terminy i dane Z TEKSTU WEJŚCIOWEGO. Jeśli input mówi "Styczeń", sprawdzaj styczeń. Nie zmieniaj daty na inną (np. marzec), chyba że oferta wygasła. Bądź precyzyjny co do faktów (np. linii lotniczych, miast wylotu, warunków oferty).
3. **OBSŁUGA LIST:** Jeśli oferta to artykuł zbiorczy (np. "12 pakietów do ZEA"), NIE ODRZUCAJ GO jako zbyt ogólny. Znajdź w tekście jedną, konkretną i najatrakcyjniejszą ofertę (np. konkretny hotel) i zweryfikuj JĄ jako reprezentanta całego wpisu.
4. **JĘZYK I SKŁADNIA:** WYŁĄCZNIE poprawny polski z zachowaniem naturalnej, nienagannej składni gramatycznej. Tłumacz dane z zagranicznych źródeł tak, by brzmiały naturalnie dla Polaka (ABSOLUTNY ZAKAZ kalk językowych typu "pakiety startujące od" czy "hotel jest umiejscowiony").
5. **WERDYKT:** Jeśli oferta jest słaba, nieaktualna lub dane się nie zgadzają -> 'RISK'. Jeśli dobra -> 'GEM' lub 'FAIR'.

### 📝 ZASADY TWORZENIA TREŚCI (Pole "telegram_message")
Dla każdej oferty stwórz post na Telegram. Pisz jako profesjonalny analityk ofert turystycznych. Stosuj WYŁĄCZNIE poniższą strukturę:

**STRUKTURA (STOSUJ DOKŁADNIE):**
1. **TYTUŁ:** [Kierunek] za [Cena] — [Krótki komentarz]! [Emoji]
2. **OPIS:** 3-5 zdania płynnego tekstu wyjaśniające, dlaczego oferta jest dobra i dla kogo (np. loty transatlantyckie, ferie). Napisz szczerze, czego brakuje (np. hotelu).
3. **SEKCJA "🔥 Co ważne:":**
   - Cena: [Pogrubiona Kwota] za [Zakres, np. bilet].
   - Zakres: Krótka informacja co wchodzi w skład (np. tylko loty, all inclusive).
4. **PRO-TIP:** Jedna, mięsista wskazówka techniczna (np. o bagażu, pogodzie, transporcie z lotniska lub wizie). Unikaj ogólników.
5. **CTA:** Jedno krótkie zdanie zachęcające do szybkiej akcji.

### WYMAGANY FORMAT JSON
Zwróć obiekt z listą "audits":
{
  "audits": [
    {
      "id": "PRZEPISZ DOKŁADNIE ID Z INPUTU",
      "hotel_name": "Polski tytuł oferty (poprawna składnia)",
      "price_value": 2500,  // WAŻNE: Liczba (int)
      "currency": "WYKRYTA WALUTA (np. PLN, EUR, USD)",
      "internal_log": "Info techniczne z audytu",
      "verdict": "GEM", // FAIR, RISK
      "sztos_score": 9,     // Liczba (int)
      "telegram_message": "Twój post po polsku wg zasad powyżej. Pamiętaj o pogrubieniach i merytorycznym Pro-Tipie."
    },
    ...
  ]
}"""

    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Wykonaj audyt dla tych {len(batch)} ofert:\n{offers_text}"}
        ],
        "temperature": 0.1,
        "max_tokens": 2000, 
        "top_p": 0.9,
        "return_citations": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "audits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": ["string", "integer"]},
                                    "hotel_name": {"type": "string"},
                                    "price_value": {"type": ["number", "string", "integer"]},
                                    "currency": {"type": "string"},
                                    "internal_log": {"type": "string"},
                                    "verdict": {"type": "string", "enum": ["GEM", "FAIR", "RISK"]},
                                    "sztos_score": {"type": "integer"},
                                    "telegram_message": {"type": ["string", "null"]}
                                },
                                "required": ["id", "verdict", "telegram_message", "price_value", "currency", "internal_log", "hotel_name"]
                            }
                        }
                    },
                    "required": ["audits"]
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
    for attempt in range(max_retries):
        try:
            async with make_async_client() as client:
                response = await client.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers, timeout=120.0)
                response.raise_for_status()
                
                content = response.json().get('choices', [{}])[0].get('message', {}).get('content')
                if not content: raise ValueError("Empty content from AI")
                
                result_data = json.loads(content)
                audits = result_data.get('audits', [])
                
                # Clean citations and ensure Polish
                for audit in audits:
                    if audit.get('telegram_message'):
                        audit['telegram_message'] = re.sub(r'\[\d+\]', '', audit['telegram_message']).strip()
                
                log.info(f"Perplexity batch audit successful. Processed {len(audits)} offers.")
                return audits

        except Exception as e:
            log.warning(f"Batch audit attempt {attempt+1} failed: {e}")
            await asyncio.sleep(1 * (attempt + 1))

    log.error("Batch audit failed after retries.")
    # Return failure dummy results
    return [{'id': c.get('id'), 'verdict': 'ERROR'} for c in batch]


async def analyze_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not get_gemini_client():
        log.error("Gemini AI client not initialized. Skipping AI analysis.")
        return []

    # New "Silent Selector" prompt
    system_prompt = """Jesteś surowym, ekonomicznym filtrem analitycznym dla ofert turystycznych.
Twój cel: Działaj jak bezlitosny filtr. Odrzucaj bez wahania oferty przeciętne i "tylko dobre". Zwracaj w JSON tylko te wybitne.
Analizuj tekst w oryginale (EN/PL), odpowiedź JSON generuj w języku POLSKIM.

ZASADY OCENY (SCORE & CONVICTION):
1.  **CONVICTION (1-10)**: Twoja pewność co do jakości danych.
    - Jeśli cena jest super niska, ale brakuje dat/linii -> Score może być wysoki, ale Conviction NISKI (np. 3).
    - Jeśli oferta jest kompletna i pewna -> Conviction WYSOKI (8-10).
2.  **SCORE (1-10)**: Atrakcyjność oferty.
    - **10 (SZTOS)**: Ewidentny błąd cenowy (Error Fare) lub historyczne minimum.
    - **9 (GEM)**: Bardzo rzadka okazja (np. loty do USA < 1500 PLN).
    - **1-8 (IGNORE)**: Ceny standardowe, reklamy, spam.

WYMAGANY FORMAT JSON (Lista obiektów):

SCENARIUSZ A: OFERTA "PUSH" (Score 9-10)
Zwróć pełne dane, aby można było wysłać powiadomienie:
{
  "id": (zachowaj ID z inputu),
  "category": "PUSH",
  "score": 9,
  "conviction": 9,
  "title": "Krótki, chwytliwy tytuł po polsku",
  "price": "np. 126 USD",
  "price_value": 126,       // (int) sama liczba dla sortowania, 0 jeśli brak
  "currency": "USD",        // (string) kod waluty lub NULL
  "continent": "Ameryka Północna", // (Europa, Azja, Ameryka Północna, Ameryka Południowa, Afryka, Australia, Global)
  "origin_continent": "Europa", // Skąd wylot?
  "link": "...",
  "reasoning": "Cena o 50% niższa niż średnia rynkowa na tej trasie."
}

SCENARIUSZ B: OFERTA "IGNORE" (Score 1-8)
Oszczędzaj tokeny. Zwróć tylko minimum:
{
  "id": (zachowaj ID),
  "category": "IGNORE"
}

INSTRUKCJA TECHNICZNA:
- Zwracaj WYŁĄCZNIE czysty JSON. Żadnych wstępów, żadnych markdownów (```).
- Jeśli brakuje kluczowych danych (cena/kierunek), a tytuł nie sugeruje błędu cenowego -> Kategoria IGNORE.
"""
    
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

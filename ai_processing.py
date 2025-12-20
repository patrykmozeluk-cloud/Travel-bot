import logging
import json
import asyncio
import re
import random
import httpx
from google import genai
from typing import Dict, Any, List
from datetime import datetime

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
    Uses the ENTERPRISE PROMPT 2.1 (Fact Enforcement + New Telegram Style).
    """
    if not config.PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY not set. Cannot perform audit.")
        return [{'verdict': 'SKIPPED', 'reason': 'Perplexity API key not configured.', 'id': c.get('id')} for c in batch]

    # Construct the user prompt with the list of offers
    offers_text = ""
    for i, item in enumerate(batch):
        offers_text += f"\n--- OFERTA {i+1} (ID: {item.get('id')}) ---\nTytuł: {item.get('title')}\nCena: {item.get('price', 'N/A')}\nLink: {item.get('link')}\n"

    # --- NOWY SYSTEM PROMPT (Enterprise Batch Version 2.1) ---
    system_prompt = """### 🧠 ROLA: EKSPERT-SPRZEDAWCA (TRYB BATCH)
Otrzymujesz listę max 3 ofert turystycznych. Twoim zadaniem jest ich audyt i przygotowanie wpisów sprzedażowych.

⚠️ **INSTRUKCJE KRYTYCZNE (STOSUJ DO KAŻDEJ OFERTY):**
1. **IZOLACJA:** Każdą ofertę z listy analizuj OSOBNO. Nie łącz faktów, nie szukaj części wspólnych. Traktuj każdą pozycję jako oddzielne zadanie.
2. **PRIORYTET FAKTÓW:** Ściśle weryfikuj dane. Jeśli input mówi "Styczeń", nie zmieniaj na marzec.
3. **OBSŁUGA LIST:** Jeśli oferta to artykuł zbiorczy, wybierz jedną najlepszą (reprezentatywną) ofertę z tekstu i opisz ją.

---

### KROK 1: EKSTRAKCJA DANYCH (Fact Enforcement)
Zanim napiszesz treść, uzupełnij pola JSON twardymi danymi:
1. **Linie Lotnicze (`airlines`):** Znajdź nazwę przewoźnika (np. Lufthansa, Air China).
   - Jeśli źródło pisze tylko "Full Service" i nie podaje nazwy -> wpisz "Linia Tradycyjna".
   - Jeśli widzisz "obsługiwane przez Condor", wpisz "Condor".
   - Jeśli to pakiet i linia jest nieznana, wpisz "Charter / Low-cost".
2. **Daty (`date_range`):** Szukaj zakresu miesięcy (np. "Styczeń - Marzec 2026"). Unikaj konkretnych dni, chyba że oferta jest na sztywny termin. NIGDY nie pisz "do potwierdzenia".
3. **Cena (`price_value`):** Najniższa dostępna cena (liczba).

### KROK 2: ANALIZA (`internal_log`)
W brudnopisie oceń opłacalność, haczyki (bagaż, przesiadki) i strategię sprzedaży. Wykorzystaj to, by wyeliminować błędy logiczne.

### KROK 3: TREŚĆ TELEGRAM (`telegram_message`)
Stwórz post gotowy do publikacji.
**STYL:** Krótki, męski, konkretny. Jak SMS eksperta do kumpla. Zero marketingu.
**STRUKTURA:**
1. **NAGŁÓWEK:** `[Emoji] Kierunek + [Konkretna Kwota] + (wartość z pola airlines)`
   - ⚠️ **WAŻNE:** Musisz wpisać LICZBĘ i WALUTĘ z pola `price` (np. **289 USD**). Nie zostawiaj pustego miejsca!
   - *Wzór:* 🇺🇸 Nowy Jork z Londynu za **258 GBP** (Norse Atlantic)
2. **ODSTĘP (Pusta linia)**
3. **TREŚĆ (Max 3 zdania):**
   - Pisz ciągłym tekstem (prozą).
   - Połącz ocenę okazji ("historyczne minimum") z uwagami technicznymi ("brak bagażu") w jedno płynne zdanie.
   - **POGRUBIENIA:** Użyj **bolda** w treści TYLKO RAZ dla najważniejszego atutu (np. **lot bezpośredni**). Nie pogrubiaj całych zdań.
   - ZABRONIONE: Nagłówki ("Werdykt:", "Pro-Tip:"), listy punktowane, asekuranctwo ("sprawdź daty").

### KROK 4: WERDYKT (`verdict`)
- **GEM:** Super okazja / błąd cenowy.
- **FAIR:** Uczciwa cena rynkowa.
- **RISK:** Słaba oferta / brak danych / podejrzenie oszustwa.
    """

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
                "name": "travel_audit_batch_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "audits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": { "type": ["string", "integer"] },
                                    "hotel_name": { "type": "string", "description": "Krótki tytuł oferty po polsku" },
                                    "destination": { "type": "string" },
                                    "origin": { "type": "string" },
                                    "airlines": { 
                                        "type": "string",
                                        "description": "Konkretna linia lub 'Charter'. Nie wpisuj 'nieznana'."
                                    },
                                    "date_range": { 
                                        "type": "string",
                                        "description": "Zakres miesięcy (np. 'Styczeń - Marzec 2026')."
                                    },
                                    "price_value": { "type": ["number", "integer"] },
                                    "currency": { "type": "string" },
                                    "internal_log": { 
                                        "type": "string",
                                        "description": "Analiza logiczna oferty i haczyków."
                                    },
                                    "verdict": { 
                                        "type": "string", 
                                        "enum": ["GEM", "FAIR", "RISK"] 
                                    },
                                    "sztos_score": { "type": "integer" },
                                    "telegram_message": { 
                                        "type": "string",
                                        "description": "Gotowy post na Telegram wg zasad formatowania."
                                    }
                                },
                                "required": [
                                    "id", 
                                    "hotel_name", 
                                    "destination", 
                                    "origin", 
                                    "airlines", 
                                    "date_range", 
                                    "price_value", 
                                    "currency", 
                                    "internal_log", 
                                    "verdict", 
                                    "sztos_score", 
                                    "telegram_message"
                                ],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["audits"],
                    "additionalProperties": False
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
                        # Usuwanie cytatów [1] itp.
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

    # System Prompt "Sztos vs Reszta" (Dla Gemini)
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
        cleaned_text = re.sub(r'```json\n|```', '', response.text).strip()
        ai_results = json.loads(cleaned_text)
        
        if not isinstance(ai_results, list):
            log.error(f"Gemini API returned data that is not a list: {ai_results}")
            return []
        
        log.info(f"AI processed batch and returned {len(ai_results)} categorized results.")
        return ai_results

    except json.JSONDecodeError:
        log.error(f"Gemini API returned invalid JSON for batch: {response.text[:200]}")
        return []

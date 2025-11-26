# Podsumowanie Zmian - Bot RSS (26 Listopada 2025)

W tej sesji wdrożono zaawansowane mechanizmy weryfikacji ofert i strategicznego routingu, a także naprawiono serię błędów w procesie budowania i wdrażania aplikacji.

### Główne Zmiany i Ulepszenia:

1.  **Integracja z Perplexity AI i "Lejek Jakości":**
    *   Zaimplementowano zaawansowany, dwuścieżkowy "lejek jakości" do przetwarzania ofert.
    *   **Czat Ogólny** otrzymuje oferty o średniej jakości (ocena 6-8/10) oraz hity (9+/10), które nie przeszły pozytywnie dodatkowej weryfikacji.
    *   **Kanał VIP** otrzymuje **wyłącznie** hity (9+/10), które zostały zweryfikowane w czasie rzeczywistym przez Perplexity AI jako aktywne.

2.  **Przebudowa i Stabilizacja API Perplexity:**
    *   Całkowicie przepisano integrację z Perplexity, implementując oficjalne SDK (`perplexityai`), co zastąpiło ręczne wywołania `httpx`.
    *   Wprowadzono ścisły schemat `json_schema` dla odpowiedzi z API, co gwarantuje stabilność i eliminuje błędy parsowania.

3.  **Ulepszone Wiadomości i CTA (Call to Action):**
    *   Wiadomości na Kanale VIP zostały wzbogacone o przycisk "Więcej ofert...", który kieruje użytkowników bezpośrednio do grupy czatowej, tworząc spójny ekosystem.
    *   Funkcja wysyłania wiadomości została rozbudowana, aby wspierać opcjonalne przyciski.

4.  **Przywrócenie Dwukierunkowej Promocji Socjalnej:**
    *   Logika postów socjalnych została przywrócona do działania dwukierunkowego.
    *   Czat promuje Kanał VIP, a Kanał VIP promuje Czat, co wzmacnia społeczność i przepływ użytkowników między nimi.

5.  **Naprawa Procesu Wdrażania (Build & Runtime Fixes):**
    *   Zdiagnozowano i naprawiono serię błędów uniemożliwiających wdrożenie na Google Cloud Run:
        *   Poprawiono nazwę pakietu `perplexityai` w `requirements.txt`.
        *   Zmieniono wersję Pythona w `Dockerfile` na stabilną (`3.11-slim`), aby zapewnić kompatybilność pakietów.
        *   Dodano brakujący pakiet `gunicorn` do `requirements.txt`, co rozwiązało błąd uruchamiania kontenera.

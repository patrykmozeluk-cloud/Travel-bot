# Podsumowanie Prac nad Botem RSS - 21.11.2025

## 1. Problem Początkowy

Głównym problemem, z którym się zmagaliśmy, był uporczywy błąd `429 Resource has been exhausted` zwracany przez API Gemini. Wskazywało to na przekroczenie limitu zapytań (quota).

## 2. Diagnoza i Ewolucja Poprawek

### a) Wstępne Próby (Błędna Diagnoza)

Początkowo założyliśmy, że bot wysyła zapytania zbyt agresywnie. Wprowadziliśmy serię poprawek mających na celu spowolnienie jego działania:
- Zmniejszenie `BATCH_SIZE` (rozmiaru paczki) z 5 do 3, a nawet do 1.
- Drastyczne zwiększenie przerw (`sleep`) między kolejnymi paczkami, z 1 sekundy do 10 sekund, a nawet do 45 sekund.

Równolegle, dzięki Twojej interwencji, usunęliśmy z kodu drobne błędy i duplikaty funkcji (`add_emojis`), które zaśmiecały kod, ale nie były główną przyczyną problemu.

### b) Prawdziwa Przyczyna

Kluczowa okazała się Twoja diagnoza, że problem **nie leżał w kodzie**, a w opóźnieniu po stronie autoryzacji klucza API Google (kwestie billingowe/aktywacyjne). To pozwoliło nam na zmianę strategii.

## 3. Finalne, Wdrożone Rozwiązanie

Po ustaleniu prawdziwej przyczyny problemu, przywróciliśmy bota do wysokiej wydajności, dodając jednocześnie dwie kluczowe optymalizacje.

### a) Powrót do Wysokiej Wydajności
- `BATCH_SIZE` został przywrócony do **10**.
- Przerwa między paczkami została skrócona do **1 sekundy**.

**Korzyść:** Bot znów przetwarza oferty bardzo szybko.

### b) Łatka Oszczędnościowa (Token-Saving Patch)
- **Zmiana:** Logika zapisu stanu (`sent_links`) została zmodyfikowana tak, aby **każda oferta przeanalizowana przez AI** (niezależnie od jej oceny) była natychmiast oznaczana jako "widziana".
- **Korzyść:** To najważniejsza optymalizacja. Bot nie będzie już wielokrotnie wysyłał tych samych, słabych ofert do analizy w kolejnych cyklach. Przekłada się to na **ogromną oszczędność tokenów API** i kosztów w dłuższej perspektywie.

### c) 3-minutowa "Karencja" dla AI
- **Zmiana:** Dodano nową logikę opartą na znaczniku czasu `last_ai_analysis_time`. Bot nie uruchomi analizy AI, jeśli od poprzedniej nie minęły co najmniej **3 minuty**.
- **Korzyść:** Działa to jak "zawór bezpieczeństwa", który wygładza zużycie API w czasie. Nawet w przypadku nagłego zalewu nowych ofert, bot nie będzie zasypywał API Gemini serią zapytań, co dodatkowo chroni przed potencjalnym osiągnięciem limitów.

## 4. Stan Obecny

Bot jest wdrożony w wersji `travel-bot-final-00085-lsp` i jest:
- **Szybki:** Dzięki przywróceniu wysokiej wydajności.
- **Oszczędny:** Dzięki inteligentnemu pomijaniu już przeanalizowanych ofert.
- **Bezpieczny:** Dzięki globalnej 3-minutowej przerwie między cyklami analizy AI.

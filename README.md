Project Title: Travel Deal Hybrid Bot (v6.0) ✈️💰
Table of Contents





Introduction
The Travel Deal Hybrid Bot (v6.0) is a high-performance, asynchronous Python application designed to aggregate real-time flight and holiday deals from multiple external sources (RSS Feeds) and publish them to a Telegram channel. The system includes an automated cleanup mechanism to manage content lifecycle.

This project was developed to overcome the limitations of simple feed readers by adding deduplication logic, content scraping, intelligent emoji tagging, and a robust atomic state management system.

Key Features
Real-time Aggregation: Simultaneously monitors over 10 flight and travel deal RSS feeds (e.g., fly4free.pl, wakacyjnipiraci.pl, secretflying.com).

Asynchronous Processing: Utilizes httpx and asyncio with per-host concurrency limits and jitter delays to ensure efficient, non-blocking requests and avoid IP bans.

Intelligent Content: Automatically extracts a brief description from the linked deal page and adds relevant flag and category emojis based on keyword detection (e.g., 🇪🇸, 🇯🇵, 🏖️, 💰).

Atomic State Management: Uses Google Cloud Storage (GCS) for robust, atomic state locking, ensuring no two concurrent runs overwrite the list of already-sent links (sent_links.json).

Content Lifecycle Management: Implements an automated sweep job to delete messages from the Telegram channel after a specified TTL (e.g., 48 hours), keeping the channel fresh and relevant.

URL Canonicalization: Cleans links by removing common tracking parameters (utm_, fbclid, gclid, etc.) before storage and sending.

Architecture and Technology Stack
The bot runs as a containerized web service with a Flask-based endpoint for triggering the main job, making it suitable for deployment on cloud platforms like Google Cloud Run or a dedicated VM.

Language: Python 3.13

Asynchronous/HTTP: asyncio, httpx[http2]

Data Processing: feedparser, orjson (for fast JSON), beautifulsoup4 (for scraping)

Cloud/State: google-cloud-storage (for atomic state persistence)

Deployment: Docker, Gunicorn, Flask

Portfolio Highlight (Technical Breakdown)
This project showcases my ability to develop highly resilient and scalable data pipeline logic.

Concurrency Control: I implemented a custom asyncio.Semaphore system (_sem_for) to limit concurrent requests to the same source host (e.g., max 2 connections per domain), preventing potential rate-limiting issues while maintaining overall high speed.

Robust Deduplication: Posts are tracked using a stable GUID from the RSS feed, not just the URL, to prevent resending identical content even if the URL structure slightly changes. The state is pruned using a 336-hour TTL to manage the storage footprint.

Content Extraction Logic: Developed scrape_description to intelligently find the most relevant paragraph on a deal page, truncate it neatly at the last space within 200 characters, and use it as the main Telegram message text, significantly improving message quality.

Failure Resilience: The sweep_delete_queue function handles Telegram API errors (400/403) gracefully, specifically logging messages that are "too old" or "not found" and removing them from the queue without retries, thus cleaning up the state.

Contact
LinkedIn: [Link do Twojego profilu LinkedIn]

Email: [Twój adres email]

Other Projects: [Link do Twojego głównego profilu GitHub]

🇵🇱 Wersja Polska
Tytuł Projektu: Hybrydowy Bot Ofert Podróżniczych (v6.0) ✈️💰
Spis Treści





Wprowadzenie
Hybrydowy Bot Ofert Podróżniczych (v6.0) to wysokowydajna, asynchroniczna aplikacja w Pythonie, zaprojektowana do agregowania ofert lotniczych i wakacyjnych w czasie rzeczywistym z wielu źródeł zewnętrznych (feedów RSS) i publikowania ich na kanale Telegrama. System zawiera zautomatyzowany mechanizm porządkowania, który zarządza cyklem życia treści.

Projekt ten został stworzony, aby pokonać ograniczenia prostych czytników RSS poprzez dodanie logiki deduplikacji, scrapowania treści, inteligentnego tagowania emotikonami oraz solidnego systemu atomowego zarządzania stanem.

Główne Funkcjonalności
Agregacja w Czasie Rzeczywistym: Jednoczesne monitorowanie ponad 10 feedów RSS z ofertami lotniczymi i podróżniczymi (np. fly4free.pl, wakacyjnipiraci.pl, secretflying.com).

Przetwarzanie Asynchroniczne: Wykorzystanie bibliotek httpx i asyncio z limitami współbieżności na hosta i opóźnieniami typu jitter, aby zapewnić wydajne, nieblokujące żądania i uniknąć blokad adresów IP.

Inteligentna Treść: Automatyczne pobieranie krótkiego opisu ze strony oferty i dodawanie odpowiednich emotikon flag i kategorii na podstawie wykrytych słów kluczowych (np. 🇪🇸, 🇯🇵, 🏖️, 💰).

Atomowe Zarządzanie Stanem: Wykorzystanie Google Cloud Storage (GCS) do niezawodnego, atomowego blokowania stanu, gwarantujące, że dwie równoległe instancje nie nadpiszą listy już wysłanych linków (sent_links.json).

Zarządzanie Cyklem Życia Treści: Wdrożenie automatycznego zadania sweep (sprzątania) do usuwania wiadomości z kanału Telegrama po określonym czasie życia (TTL, np. 48 godzin), co utrzymuje aktualność kanału.

Kanoniczna Weryfikacja URL: Czyszczenie linków z popularnych parametrów śledzących (utm_, fbclid, gclid itd.) przed zapisem i wysłaniem.

Architektura i Użyte Technologie
Bot działa jako skonteneryzowana usługa webowa z endpointem opartym na Flasku do wyzwalania głównego zadania, dzięki czemu jest idealny do wdrożenia na platformach chmurowych, takich jak Google Cloud Run lub dedykowana maszyna wirtualna.

Język: Python 3.13

Asynchroniczność/HTTP: asyncio, httpx[http2]

Przetwarzanie Danych: feedparser, orjson (do szybkiego JSON), beautifulsoup4 (do scrapowania)

Chmura/Stan: google-cloud-storage (do atomowej persystencji stanu)

Wdrożenie: Docker, Gunicorn, Flask

Projekt jako Element Portfolio (Analiza Techniczna)
Ten projekt prezentuje moje umiejętności w tworzeniu wysoce odpornej i skalowalnej logiki pipeline'ów danych.

Kontrola Współbieżności: Wdrożyłem niestandardowy system asyncio.Semaphore (_sem_for), aby ograniczyć jednoczesne żądania do tego samego hosta źródłowego (np. maks. 2 połączenia na domenę). Zapobiega to problemom z limitami zapytań, zachowując jednocześnie wysoką ogólną szybkość.

Solidna Deduplikacja: Posty są śledzone za pomocą stabilnego GUID z feeda RSS, a nie tylko URL, aby zapobiec ponownemu wysłaniu identycznej treści. Stan jest optymalizowany poprzez usuwanie starych wpisów po 336 godzinach TTL.

Logika Ekstrakcji Treści: Opracowałem funkcję scrape_description, aby inteligentnie znaleźć najbardziej istotny akapit na stronie oferty, elegancko go skrócić przy ostatniej spacji w granicach 200 znaków i użyć jako głównego tekstu wiadomości Telegrama, co znacząco poprawia jakość komunikacji.

Odporność na Błędy: Funkcja sweep_delete_queue elegancko obsługuje błędy API Telegrama (400/403), w szczególności logując wiadomości, które są „za stare” lub „nie znalezione” i usuwając je z kolejki bez ponawiania prób, co przyczynia się do oczyszczania stanu.

Kontakt

Email: patrykmozeluk@gmail.com

Inne Projekty: https://github.com/patrykmozeluk-cloud/Travel-bot/settings
# OSTATECZNA WERSJA - POPRAWIONA
FROM python:3.11

# 1. Ustawienia środowiska
ENV PYTHONUNBUFFERED True
ENV PORT 8080
ENV PYTHONASYNCIODEBUG 1

# 2. Folder roboczy
WORKDIR /app

# 3. Instalacja pakietów systemowych (tylko niezbędne minimum)
# build-essential może być potrzebny do niektórych bibliotek pythonowych,
# ale usuwamy Rusta, chyba że instalacja pip wyrzuci błąd.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 4. Kopiowanie requirements i instalacja zależności
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 5. Kopiowanie reszty plików aplikacji
# WAŻNE: Upewnij się, że plik rss_sources.txt jest w tym samym folderze co Dockerfile!
COPY . .

# 6. Uruchomienie aplikacji przez Gunicorn
# main:app oznacza: plik main.py, obiekt app = Flask(__name__)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 2 --timeout 900 app:app

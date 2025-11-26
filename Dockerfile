# OSTATECZNA WERSJA - 1 PAŹ 2025 v5 (dla pliku main.py)
# 1. Użyj oficjalnego, lekkiego i STABILNEGO obrazu Pythona
FROM python:3.11-slim

# 2. Ustaw zmienną środowiskową, aby logy pojawiały się od razu
ENV PYTHONUNBUFFERED True

# 3. Ustaw folder roboczy wewnątrz kontenera
WORKDIR /app

# 4. Skopiuj plik z bibliotekami
COPY requirements.txt .

# Zainstaluj pakiety systemowe potrzebne do kompilacji niektórych bibliotek Pythona
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# 5. Zainstaluj biblioteki Pythona (z aktualizacją pip)
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# 6. Skopiuj resztę plików aplikacji (w tym main.py)
COPY . .

# 7. Ustaw komendę startową wskazującą na plik main.py
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "300", "main:app"]
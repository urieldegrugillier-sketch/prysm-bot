FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Copier et installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code source
COPY bot.py .

# Lancer le bot (flag -u pour ne pas bufferiser les logs)
CMD ["python", "-u", "bot.py"]

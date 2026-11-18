FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot
COPY --chown=bot:bot bot.py contracts.py company_card.py check_docx.py contracts.json ./
COPY --chown=bot:bot templates/ ./templates/
USER bot
CMD ["python", "bot.py"]

# Single image: FastAPI backend on :8000 + Streamlit frontend on :8501.
# Mongo / Redis / Weaviate stay as separate services in docker-compose.yml
# (they are stateful and need their own official images + volumes).
FROM python:3.11-slim

WORKDIR /srv

# All Python deps (backend + frontend) in one layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app ./app
COPY streamlit_app.py .
COPY .streamlit ./.streamlit

EXPOSE 8000 8501

# Run both processes. Uvicorn in the background, Streamlit in the foreground.
# `wait -n` waits for the first child to exit; if either dies the container
# exits and the orchestrator (docker-compose restart: unless-stopped)
# brings it back. Mixed stdout/stderr is fine for dev; in production you
# would split this into two pods.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 & streamlit run streamlit_app.py --server.address=0.0.0.0 --server.port=8501 & wait -n"]

FROM node:22-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates nginx apache2-utils xvfb x11-utils fluxbox x11vnc novnc websockify && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY package.json ./
RUN npm install --omit=dev && npx playwright install --with-deps chromium
COPY app.py reply_model.py hr_review.py pg_store.py cloud_start.py browser.cjs browser-chats.cjs questionnaire.cjs index.html app.js style.css ./
COPY profile.example.json ./profile.json
COPY supabase-ca.crt ./
ENV HH_DATA_DIR=/tmp/hh-data HH_ASSISTANT_BROWSER_DIR=/tmp/hh-browser PYTHONUNBUFFERED=1
CMD ["python", "cloud_start.py"]

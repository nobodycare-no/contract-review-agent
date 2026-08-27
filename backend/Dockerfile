# ---- 阶段一：前端构建（Vue3/Vite）----
FROM node:20-alpine AS webbuilder
WORKDIR /web
COPY web/package.json ./
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY web/ ./
RUN npm run build

# ---- 阶段二：后端运行时（tesseract OCR / 托管前端构建产物）----
FROM python:3.11-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY backend/app ./app
COPY --from=webbuilder /web/dist ./web/dist
RUN mkdir -p /srv/storage/attachments

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

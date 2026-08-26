FROM python:3.11-slim
WORKDIR /srv
# 中文字体：PNG 扫描件渲染必需（缺失会回退位图字体导致 OCR 噪声）
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY main.py store.py contracts_def.py ./
EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]

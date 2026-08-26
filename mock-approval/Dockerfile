FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY main.py store.py contracts_def.py ./
EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]

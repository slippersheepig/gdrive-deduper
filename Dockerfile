FROM python:alpine
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
WORKDIR /app
RUN apk add tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dedupe.py .
CMD ["python", "dedupe.py"]

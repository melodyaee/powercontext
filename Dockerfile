FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    TMPDIR=/data

WORKDIR /app

# numpy is used purely as a general math library for batch distance arithmetic.
# No storage engine, SQL engine or vector-index library is installed.
RUN pip install --no-cache-dir --retries 3 --timeout 30 \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --extra-index-url https://pypi.org/simple \
    numpy==2.1.3

COPY minivectordb.py /app/minivectordb.py

RUN mkdir -p /data

# The judge starts the container with `docker run -i`, keeps stdin open and
# mounts a writable volume at /data. No ports, no SQL prompt, no network.
CMD ["python3", "-B", "-u", "/app/minivectordb.py", "/data"]

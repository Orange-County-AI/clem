FROM python:3.12-slim

RUN pip install uv

COPY . .

RUN uv sync

CMD ["uv", "run", "clem"]

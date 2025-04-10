FROM python:3.12-bookworm
LABEL authors="hsn"
USER root

# Install Poetry
RUN pip3 install uv

WORKDIR /app


COPY ./pyproject.toml /app/pyproject.toml
COPY ./uv.lock /app/uv.lock

RUN uv venv
RUN uv pip install -e .
RUN uv run playwright install
RUN uv run playwright install-deps

COPY ./src /app/src
COPY ./main.py /app/main.py
ENTRYPOINT ["uv", "run", "python", "-u","start.py"]
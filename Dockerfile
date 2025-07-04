FROM python:3.12-bookworm
LABEL authors="hsn"
USER root

RUN apt-get update
RUN apt-get install -y chromium 

# Install Poetry
RUN pip3 install uv

WORKDIR /app


COPY ./pyproject.toml /app/pyproject.toml
COPY ./uv.lock /app/uv.lock

RUN uv venv
RUN uv sync
# RUN uv run playwright install firefox
# RUN uv run playwright install-deps firefox

COPY ./src /app/src
COPY ./main.py /app/main.py
ENTRYPOINT ["uv", "run", "python", "-u","main.py"]
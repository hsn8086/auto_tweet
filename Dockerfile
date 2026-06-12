FROM python:3.12-bookworm
LABEL authors="hsn"
USER root

RUN apt-get update
RUN apt-get install -y chromium 

# Install uv（qj 构建直连 pypi 会卡死，默认走清华镜像；可 --build-arg 覆盖）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_HTTP_TIMEOUT=120
RUN pip3 install -i ${PIP_INDEX_URL} uv

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
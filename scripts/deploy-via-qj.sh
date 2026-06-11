#!/bin/bash
# auto_tweet 部署脚本（在 sg 上运行）。
#
# 背景：sg 无法访问 Docker Hub，本机 build 拉不动基础镜像。历史做法是
# docker cp + docker commit 热更（快但产生不可复现的雪花镜像）。本脚本
# 提供干净路径：把仓库同步到 qj，在 qj 构建镜像，docker save 回传加载。
#
# 注意：镜像 ~1.5GB，qj→sg 隧道慢，全程可能 10-20 分钟；紧急小改仍可用
# 热更流程（见 docs/architecture.md §8.2），但事后必须尽快跑一次本脚本
# 让镜像回到可复现状态。
set -euo pipefail

QJ="hsn@192.168.13.149"
BUILD_DIR="/tmp/auto_tweet_build"
IMAGE="auto_tweet-auto-twi:latest"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "[1/4] rsync repo -> qj:$BUILD_DIR"
rsync -az --delete \
    --exclude ".git" --exclude ".venv" --exclude "__pycache__" \
    --exclude "data/" --exclude "ss/" --exclude ".env" \
    "$REPO_DIR/" "$QJ:$BUILD_DIR/"

echo "[2/4] build on qj"
ssh "$QJ" "cd $BUILD_DIR && docker build -t $IMAGE ."

echo "[3/4] transfer image qj -> sg (slow link, be patient)"
ssh "$QJ" "docker save $IMAGE | gzip -1" | gunzip | docker load

echo "[4/4] restart compose with new image"
cd "$REPO_DIR"
docker compose up -d --no-build

echo "DONE. verify:"
echo "  docker logs --since 2m auto_tweet-auto-twi-1 | tail"
echo "  curl -s http://127.0.0.1:8000/api/v1/tweet/queue"

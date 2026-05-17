#!/usr/bin/env bash
# 拉 8 个 SWE-Skills docker images (~24 GB)
set -euo pipefail

IMAGES=(
  "zhangyiiiiii/swe-skills-bench-python:latest"
  "zhangyiiiiii/swe-skills-bench-clojure:latest"
  "zhangyiiiiii/swe-skills-bench-bazel:latest"
  "zhangyiiiiii/swe-skills-bench-ruby:latest"
  "zhangyiiiiii/swe-skills-bench-golang:latest"
  "zhangyiiiiii/swe-skills-bench-rust:latest"
  "zhangyiiiiii/swe-skills-bench-jvm:latest"
  "zhangyiiiiii/swe-skills-bench-pytorch:latest"
)

echo "═══ 拉 ${#IMAGES[@]} 个 SWE-Skills docker images (~24 GB) ═══"
for img in "${IMAGES[@]}"; do
  echo "── pulling $img ──"
  docker pull $img &
done
wait

echo
echo "── 检查 ──"
docker images | grep swe-skills | sort

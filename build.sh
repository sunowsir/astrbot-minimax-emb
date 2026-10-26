#!/bin/sh
# 持续从原版镜像生成带附加能力的新镜像。
#
#   ./build.sh                          # 用 soulter/astrbot:latest 造
#   ./build.sh soulter/astrbot:v4.30.0  # 指定原版 tag
#   ./build.sh --tag my-astrbot:1.0     # 自定义输出 tag
#
# 产出镜像包含：
#   - MiniMax Embedding 适配器（embo-01，1536 维）
#   - asyncpg / scikit-learn / cachetools / python-pptx
#   - 以上依赖安装时带核心依赖约束，不会顶掉 numpy/scipy/pandas
#
# 幂等：反复跑安全；换原版 tag 再跑，会在新原版上重新注入。
# 不影响任何正在运行的容器。

set -e

HERE=$(cd "$(dirname "$0")" && pwd)
DATA_DIR=$(dirname "$HERE")          # 构建上下文必须是 data 目录
BASE_IMAGE="soulter/astrbot:latest"
OUT_TAG="astrbot-minimax-emb:latest"

PIP_CHECK_STRICT=1

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) OUT_TAG="$2"; shift 2 ;;
        --base) BASE_IMAGE="$2"; shift 2 ;;
        --lax) PIP_CHECK_STRICT=0; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) BASE_IMAGE="$1"; shift ;;
    esac
done

echo "[build] ===== 开始 ====="
echo "[build] 原版镜像  : $BASE_IMAGE"
echo "[build] 输出 tag  : $OUT_TAG"
echo "[build] 构建上下文: $DATA_DIR"
echo "[build] 依赖校验  : $([ "$PIP_CHECK_STRICT" = 1 ] && echo '严格（pip check 失败即中止）' || echo '宽松')"
echo "[build] ----------------"

cleanup_orphans() {
    # 构建失败时 daemon 不会清理中间容器，会留下一堆 exited 的孤儿。
    # 找出用本项目镜像但已停止的容器并清掉。
    if [ -S /var/run/docker.sock ] && command -v python3 >/dev/null 2>&1; then
        python3 - <<'PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, __import__("os").getcwd())
try:
    from docker_api import api
    _, cs = api("GET", "/containers/json?all=true")
    for c in cs:
        img = (c.get("Image") or "") + " " + " ".join(c.get("Names") or [])
        if c.get("State") == "exited" and "astrbot-minimax-emb" in img:
            api("DELETE", "/containers/%s?force=1" % c["Id"])
            print("[build] 清理孤儿容器:", c["Names"])
except Exception:
    pass
PYEOF
    fi
}

if command -v docker >/dev/null 2>&1; then
    # 有 docker CLI：走标准 build，.dockerignore 自动生效
    docker build \
        --build-arg "BASE_IMAGE=$BASE_IMAGE" \
        --build-arg "PIP_CHECK_STRICT=$PIP_CHECK_STRICT" \
        -t "$OUT_TAG" \
        -f "$HERE/Dockerfile" \
        "$DATA_DIR" || { cleanup_orphans; echo "[build] 构建失败" >&2; exit 1; }
    echo "[build] ===== 完成 ====="
    exit 0
fi

# 没有 CLI 但有 socket：走 Engine API
if [ -S /var/run/docker.sock ]; then
    echo "[build] 无 docker CLI，改用 Engine API"
    cd "$HERE"
    BASE_IMAGE="$BASE_IMAGE" OUT_TAG="$OUT_TAG" \
        PIP_CHECK_STRICT="$PIP_CHECK_STRICT" python3 docker_build.py "$OUT_TAG" \
        || { cleanup_orphans; echo "[build] 构建失败" >&2; exit 1; }
    echo "[build] ===== 完成 ====="
    exit 0
fi

echo "[build] 既没有 docker CLI，也没有 /var/run/docker.sock" >&2
exit 1
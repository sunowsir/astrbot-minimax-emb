# 在官方镜像基础上叠加：MiniMax Embedding 适配器 + 额外 py 依赖
#
# 基础镜像可通过 --build-arg 切换，脚本 build.sh 已封装：
#     docker build --build-arg BASE_IMAGE=soulter/astrbot:v4.30.0 ...
#
# 幂等性：重复构建、或换官方 tag 再构建，注入器都会跳过已存在的部分。
# 上游增删其它 provider 不影响；上游若重命名了锚点里的 provider，
# 注入器会按优先级回退到下一个锚点，全都不行则构建明确失败并提示可操作的修法。
ARG BASE_IMAGE=soulter/astrbot:latest
FROM ${BASE_IMAGE}

USER root

# ARG 必须在 FROM 之后重新声明，RUN 里才拿得到
ARG PIP_CHECK_STRICT=1

# ---------- 1. 额外 py 依赖 ----------
# 复刻 WebUI「安装插件依赖」的行为：pip install <包> -i <源> -c <核心依赖约束>。
# 约束文件由 gen_constraints.py 从当前环境的 AstrBot 已安装依赖生成，
# 等价于 AstrBot 自己的 CoreConstraintsProvider（当前环境为 62 个约束）。
# 作用是装依赖时不会顺带升级/降级 AstrBot 的核心依赖（aiohttp、pandas、faiss-cpu 等）。
#
# numpy/scipy 属于传递依赖、不在约束内，它们不被改动的依据是
# "pip 默认不升级已满足的包"——所以这里刻意不加 --upgrade。
# 生成器只在构建期运行一次，产物不留在镜像里。
COPY astrbot-minimax-emb/gen_constraints.py /tmp/gen_constraints.py
COPY astrbot-minimax-emb/requirements-extra.txt /tmp/requirements-extra.txt

# 四个额外依赖用 requirements-extra.txt 钉死版本：
# 它们不在 AstrBot 的直接依赖里，也就享受不到核心约束文件的保护，
# 不钉版本的话 pip 会拉最新版，构建结果随日期漂移（实测 cachetools
# 7.2.4 → 7.2.5）。核心约束负责"别动 AstrBot 自己的依赖"，
# requirements-extra.txt 负责"这几个装哪一版"，两层各管各的。
RUN set -e; \
    python /tmp/gen_constraints.py /tmp/core_constraints.txt --pins-out /tmp/core_pins.json; \
    pip install --no-cache-dir \
        -r /tmp/requirements-extra.txt \
        -i https://pypi.org/simple/ \
        -c /tmp/core_constraints.txt; \
    python -c "import asyncpg, sklearn, cachetools, pptx; print('[deps] ok')"; \
    python -c "import numpy, scipy, pandas; print('[env] numpy/scipy/pandas:', numpy.__version__, scipy.__version__, pandas.__version__)"; \
    if [ "$PIP_CHECK_STRICT" = "1" ]; then \
        if ! pip check; then \
            echo '[FATAL] pip check 未通过，依赖存在冲突，终止构建。' >&2; \
            echo '        若确认冲突与本次安装无关，可放宽：' >&2; \
            echo '        docker build --build-arg PIP_CHECK_STRICT=0 ...' >&2; \
            exit 1; \
        fi; \
    else \
        echo '[warn] PIP_CHECK_STRICT=0，跳过 pip check 强制中止' >&2; \
        pip check || true; \
    fi; \
    echo '[deps] pip check 通过'; \
    rm -f /tmp/gen_constraints.py /tmp/core_constraints.txt

# ---------- 2. MiniMax Embedding 适配器 ----------
COPY astrbot-minimax-emb/minimax_embedding_source.py /tmp/minimax_embedding_source.py
COPY astrbot-minimax-emb/install_minimax_emb.py /tmp/install_minimax_emb.py
COPY astrbot-minimax-emb/verify.py /tmp/verify.py

RUN set -e; \
    python /tmp/install_minimax_emb.py --source /tmp/minimax_embedding_source.py; \
    python /tmp/verify.py --core-pins /tmp/core_pins.json; \
    mv /tmp/core_pins.json /AstrBot/.emb-core-pins.json; \
    rm -f /tmp/minimax_embedding_source.py /tmp/install_minimax_emb.py /tmp/verify.py /tmp/requirements-extra.txt; \
    echo '[minimax-emb] 构建完成'
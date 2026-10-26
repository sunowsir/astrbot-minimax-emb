"""通过 Docker Engine API 构建镜像，不依赖 docker CLI。

用法：
    python docker_build.py                          # 默认 tag
    python docker_build.py astrbot-minimax-emb:v2

关键点：
- 只读取本目录作为构建上下文，不修改任何文件。
- 从 /AstrBot 读取，适配"人在容器里"和"人在宿主上"两种场景：
  找不到 /AstrBot 时自动退到本脚本所在目录。
- 不使用 docker compose，等价于 `docker compose build`，不会碰运行中的容器。
"""

import io
import json
import os
import sys
import tarfile
import time
from urllib.parse import urlencode

from docker_api import UnixHTTPConnection

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TAG = "astrbot-minimax-emb:latest"
# tar 里的顶层目录名，Dockerfile 里 COPY 路径依赖它
CTX_ROOT = os.path.basename(HERE)
# 宿主 compose 的 context 是 ./data，所以 Dockerfile 路径固定成这样
DOCKERFILE_IN_CTX = f"{CTX_ROOT}/Dockerfile"

def find_context():
    """容器内 /AstrBot/data/... 或宿主 ./data/...，优先前者。"""
    cands = [
        HERE,
        "/AstrBot/data/astrbot-minimax-emb",
        os.path.join(os.getcwd(), "astrbot-minimax-emb"),
    ]
    for c in cands:
        if os.path.isfile(os.path.join(c, "Dockerfile")):
            return c
    raise SystemExit("找不到 Dockerfile，请确认本脚本位于 data/astrbot-minimax-emb/")


def make_tar(src):
    """打包构建上下文。

    刻意排除 data/ 和 __pycache__：
    data/ 是 astrbot 在 CWD 下误生成的假项目根目录（见 README 遗留问题一节），
    绝不能进构建上下文。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in sorted(os.listdir(src)):
            if name == "__pycache__" or name.endswith(".pyc"):
                continue
            if name == "data" and os.path.isdir(os.path.join(src, name)):
                print("[build] 跳过误产物目录 data/")
                continue
            full = os.path.join(src, name)
            if not os.path.isfile(full):
                continue
            info = tf.gettarinfo(full, arcname=f"{CTX_ROOT}/{name}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0  # 提高可复现性
            with open(full, "rb") as fh:
                tf.addfile(info, fh)
    return buf.getvalue()


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TAG
    src = find_context()
    data = make_tar(src)
    base = os.environ.get("BASE_IMAGE", "soulter/astrbot:latest")
    print(f"[build] 上下文 {src} ({len(data)} 字节)", flush=True)
    print(f"[build] 原版镜像 {base} -> tag={tag}", flush=True)

    # buildargs 必须是 JSON 对象字符串，不是 urlencode
    qs = urlencode({
        "dockerfile": DOCKERFILE_IN_CTX,
        "t": tag,
        "rm": "1",
        "buildargs": json.dumps({
            "BASE_IMAGE": base,
            "PIP_CHECK_STRICT": os.environ.get("PIP_CHECK_STRICT", "1"),
        }),
    })
    if os.environ.get("NO_CACHE") == "1":
        qs += "&nocache=1"
        print("[build] NO_CACHE=1，禁用层缓存，从零构建")
    conn = UnixHTTPConnection(timeout=3600)
    conn.request(
        "POST", f"/build?{qs}", body=data,
        headers={"Content-Type": "application/x-tar",
                 "Content-Length": str(len(data))},
    )
    resp = conn.getresponse()
    if resp.status != 200:
        print(f"[build] HTTP {resp.status}: {resp.read().decode(errors='replace')}")
        return 1

    start, errored, buf = time.time(), False, b""
    while True:
        ch = resp.read(1)
        if not ch:
            break
        buf += ch
        if ch != b"\n":
            continue
        line = buf.decode("utf-8", errors="replace").strip()
        buf = b""
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(line, flush=True)
            continue
        if msg.get("stream"):
            print(msg["stream"].rstrip(), flush=True)
        if msg.get("error"):
            errored = True
            print(f"[build] ERROR: {msg.get('errorDetail')}", flush=True)
        if msg.get("aux") and msg["aux"].get("ID"):
            print(f"[build] 输出镜像 {msg['aux']['ID'][:19]}", flush=True)

    print(f"\n[build] 耗时 {time.time() - start:.1f}s，"
          f"结果={'失败' if errored else '成功'}", flush=True)
    return 1 if errored else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""极简 Docker Engine API 客户端（unix socket）。

为什么需要它：容器内部通常没有 docker CLI，但挂了 /var/run/docker.sock，
用标准库直接跟 daemon 对话就够了，省得装东西。

注意：这里的操作会真实影响宿主 daemon，请只对镜像做只读查询或
一次性临时容器的创建/删除，不要用来动正在运行的服务。
"""

import http.client
import json
import socket

SOCK = "/var/run/docker.sock"
TIMEOUT = 120


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, timeout=TIMEOUT):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(SOCK)
        self.sock = s


def api(method, path, body=None, raw=False, timeout=TIMEOUT):
    """返回 (status, parsed_body)。raw=True 时 body 返回 bytes。"""
    conn = UnixHTTPConnection(timeout=timeout)
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=data, headers=hdr)
    resp = conn.getresponse()
    payload = resp.read()
    if raw:
        return resp.status, payload
    try:
        return resp.status, json.loads(payload or b"null")
    except json.JSONDecodeError:
        return resp.status, payload.decode("utf-8", errors="replace")


def inspect_container(name="astrbot"):
    """读取容器状态。只读，用于确认自己有没有误伤运行中的服务。"""
    st, info = api("GET", f"/containers/{name}/json")
    if st != 200:
        return None
    return {
        "id": info["Id"],
        "image": info["Config"]["Image"],
        "running": info["State"]["Running"],
        "started_at": info["State"]["StartedAt"],
    }

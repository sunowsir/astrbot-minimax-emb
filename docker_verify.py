"""在一次性临时容器里跑 verify.py --runtime，验完即删。

用法：
    python docker_verify.py                              # 验默认 tag
    python docker_verify.py astrbot-minimax-emb:latest

这个脚本不会碰任何正在运行的容器：
- NetworkMode=none，物理上出不了网，不会误发 API 请求；
- 不挂载任何卷，不会读写 data；
- 不映射端口；
- 无论成功失败，finally 里强制删除临时容器。

实测踩过的坑，已在此规避：
1. 镜像 Entrypoint 为 None、Cmd 为 [python, main.py]，
   创建临时容器时只设 Cmd、绝不设 Entrypoint，
   否则会叠成 `sh -c sh -c ...` 导致日志为空且退出码 0。
2. 镜像 config 为 null（老 daemon）时 /images/{}/json 会 404，
   此时直接跳过"确认 Entrypoint"这一步。
"""

import base64
import os
import sys

from docker_api import api, inspect_container

DEFAULT_TAG = "astrbot-minimax-emb:latest"
HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify.py")
# 依赖声明文件也要带进容器：verify.py 从它读取期望版本，
# 镜像里没有（构建完就删了），所以这里从宿主的源目录取
REQS = os.path.join(HERE, "requirements-extra.txt")
RUNNING = "astrbot"  # 需要保护的、正在跑的服务

# ---- 启动级测试（--boot）----
BOOT_NOISE = ("SyntaxWarning", "re_han_default", "re_skip_default", "re_skip =")
BOOT_STRONG = ("Traceback", "[ERROR]", "CRITICAL", "Fatal", "fatal error")
BOOT_WEAK = ("[WARNING]", "Exception")
BOOT_MARKS = ("AstrBot started", "WebUI is ready")



def main():
    argv = sys.argv[1:]
    boot = "--boot" in argv
    args = [a for a in argv if a != "--boot"]
    tag = args[0] if args else DEFAULT_TAG

    if boot:
        wait = int(args[1]) if len(args) > 1 else 90
        return run_boot_test(tag, wait)

    if not os.path.isfile(VERIFY):
        raise SystemExit(f"找不到 {VERIFY}")
    if not os.path.isfile(REQS):
        raise SystemExit(f"找不到 {REQS}")

    with open(VERIFY, "rb") as f:
        blob = base64.b64encode(f.read()).decode()
    with open(REQS, "rb") as f:
        rblob = base64.b64encode(f.read()).decode()
    script = (
        f"echo {blob} | base64 -d > /tmp/v.py && "
        f"echo {rblob} | base64 -d > /tmp/requirements-extra.txt && "
        "python /tmp/v.py --runtime; echo EXIT=$?"
    )

    # 动手前记基线，完事后比对，证明没误伤
    before = inspect_container(RUNNING)
    print(f"[verify] 基线 {RUNNING}: {before}")

    body = {
        "Image": tag,
        "Cmd": ["sh", "-c", script],   # 只给 Cmd
        "NetworkMode": "none",
        "AutoRemove": False,
    }
    st, res = api("POST", "/containers/create?name=emb-verify", body)
    if st != 201:
        print(f"[verify] create 失败 HTTP {st}: {res}")
        return 1
    cid = res["Id"]
    print(f"[verify] 临时容器 {cid[:24]}")

    rc = 1
    try:
        api("POST", f"/containers/{cid}/start")
        _s, w = api("POST", f"/containers/{cid}/wait?condition=not-running",
                    timeout=600)
        exit_code = w.get("StatusCode")
        _s, raw = api("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail=500",
                      raw=True)
        print("---- 容器输出 ----")
        print(raw.decode("utf-8", errors="replace"))
        print("---- 结束 ----")
        rc = 0 if exit_code == 0 else 1
        print(f"[verify] 容器退出码 {exit_code}")
    finally:
        print("[verify] 删除临时容器:", api("DELETE", f"/containers/{cid}?force=1")[0])
        after = inspect_container(RUNNING)
        print(f"[verify] 事后 {RUNNING}: {after}")
        if before and after:
            same = before["id"] == after["id"] and \
                before["started_at"] == after["started_at"]
            print(f"[verify] 运行中的服务未被重启: {'是' if same else '否'}")
    return rc




def run_boot_test(tag, wait):
    """临时容器真启动一次 AstrBot，扫启动期异常。"""
    import time

    before = inspect_container(RUNNING)
    print(f"[boot] 基线 {RUNNING}: {before}")
    print(f"[boot] 启动临时容器，等待 {wait}s ...")

    st, res = api("POST", "/containers/create?name=emb-boot", {
        "Image": tag, "NetworkMode": "none", "AutoRemove": False,
    })
    if st != 201:
        print(f"[boot] create 失败 HTTP {st}: {res}")
        return 1
    cid = res["Id"]
    rc = 1
    try:
        api("POST", f"/containers/{cid}/start")
        time.sleep(wait)
        _s, info = api("GET", f"/containers/{cid}/json")
        status = info["State"]["Status"]
        code = info["State"]["ExitCode"]
        print(f"[boot] 容器状态: {status} / 退出码 {code}")

        _s, raw = api("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail=800",
                      raw=True)
        text = raw.decode("utf-8", errors="replace")
        lines = [x for x in text.splitlines() if not any(n in x for n in BOOT_NOISE)]
        strong = [x for x in lines if any(k in x for k in BOOT_STRONG)]
        weak = [x for x in lines if any(k in x for k in BOOT_WEAK)]
        marks = [m for m in BOOT_MARKS if m in text]

        print(f"[boot] 有效日志 {len(lines)} 行 | 强异常 {len(strong)} | 警告/异常 {len(weak)}")
        for k in BOOT_MARKS:
            print(f"[boot]   {'✓' if k in text else '✗'} {k}")
        for label, group in (("强异常", strong), ("警告/异常", weak)):
            if group:
                print(f"[boot] {label}：")
                for x in group[:10]:
                    print("   ", x.strip()[:160])

        ok = status == "running" and not strong and len(marks) == len(BOOT_MARKS)
        rc = 0 if ok else 1
        print(f"[boot] 结果: {'通过' if ok else '未通过'}")
        return rc
    finally:
        print("[boot] 删除临时容器:",
              api("DELETE", f"/containers/{cid}?force=1")[0])
        after = inspect_container(RUNNING)
        if before and after:
            same = (before["id"] == after["id"]
                    and before["started_at"] == after["started_at"])
            print(f"[boot] 运行中的服务未被重启: {'是' if same else '否'}")


if __name__ == "__main__":
    raise SystemExit(main())

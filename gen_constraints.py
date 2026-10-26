"""复刻 astrbot.core.utils.core_constraints._get_core_constraints 的逻辑。

构建镜像时用来给 pip 生成一份"核心依赖版本保护"约束文件，
效果等价于 WebUI 里安装插件依赖时自动带上的那个 -c xxx_constraints.txt。

用法：
    python gen_constraints.py /tmp/core_constraints.txt [--pins-out /tmp/core_pins.json]

--pins-out 会额外写出一份 JSON，记录"关键科学栈"在本次安装**之前**的版本。
这些包名是固定的（AstrBot 的科学栈核心，不会变），但版本号是现场读出来的，
不写死——写死的话官方镜像一升级，校验就会拿着过期的快照误报失败。
verify.py 用它来回答"这次装依赖有没有动到核心包"。
"""

import argparse
import importlib.metadata as importlib_metadata
import json
import re
import sys

from packaging.requirements import Requirement

# 需要额外盯住的包。它们多数不是 AstrBot 的直接依赖（numpy/scipy 是传递依赖），
# 因此不在 pip 的约束文件覆盖范围内，得单独记一份安装前的版本。
CRITICAL = ("numpy", "scipy", "pandas", "faiss-cpu")


def canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip("-").lower()


def main() -> int:
    # 用 argparse，别手搓 flag 解析——手搓过一次，
    # 把 "--pins-out /path/x.json" 里的路径误当成位置参数，
    # 结果 pins 文件压根没生成，校验静默降级成了"存在性检查"。
    ap = argparse.ArgumentParser()
    ap.add_argument("outfile", help="pip 约束文件输出路径")
    ap.add_argument("--pins-out", default=None,
                    help="关键包安装前版本的 JSON 输出路径")
    args = ap.parse_args()
    pins_out = args.pins_out

    try:
        dist = importlib_metadata.distribution("AstrBot")
    except importlib_metadata.PackageNotFoundError:
        print(
            "[constraints] 找不到 AstrBot 分发。若继续构建，pip 将不受约束地\n"
            "              安装依赖，可能顺带升级 numpy/pandas/faiss 并造成\n"
            "              ABI 不兼容。这里直接中止。\n"
            "              若确认不需要版本保护，可自行改本脚本返回 0。",
            file=sys.stderr,
        )
        return 1

    if not dist.requires:
        print("[constraints] AstrBot 没有声明依赖，无法生成约束，已中止。", file=sys.stderr)
        return 1

    # 当前环境里已安装的包及其版本
    installed = {}
    for d in importlib_metadata.distributions():
        name = d.metadata["Name"] if d.metadata else None
        if not name:
            continue
        installed.setdefault(canonicalize(name), d.version)

    constraints = []
    for req_str in dist.requires:
        try:
            req = Requirement(req_str)
            if req.marker and not req.marker.evaluate():
                continue
            name = canonicalize(req.name)
            if name in installed:
                constraints.append(f"{name}=={installed[name]}")
        except Exception:
            continue

    # 去重且保持顺序
    seen = dict.fromkeys(constraints)
    if not seen:
        print(
            "[constraints] 生成了 0 条约束，版本保护形同虚设，已中止构建。",
            file=sys.stderr,
        )
        return 1

    with open(args.outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(seen))

    print(f"[constraints] 已启用核心依赖版本保护 ({len(seen)} 个约束)")

    if pins_out:
        pins = {}
        for name in CRITICAL:
            ver = installed.get(name)
            if ver:
                pins[name] = ver
        with open(pins_out, "w", encoding="utf-8") as f:
            json.dump(pins, f, indent=2, ensure_ascii=False)
        print(f"[constraints] 已记录关键包安装前版本 ({len(pins)} 个) -> {pins_out}")
        for k, v in pins.items():
            print(f"              {k}=={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

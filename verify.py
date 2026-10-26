"""校验 MiniMax Embedding 适配器是否正确安装。

两种模式：

    python verify.py              静态校验（不 import astrbot，构建期用）
    python verify.py --runtime    静态 + 运行时校验（容器内自查用）

静态模式刻意不 import astrbot：astrbot.core.utils.astrbot_path.get_astrbot_root()
在没有 ASTRBOT_ROOT 时会退回 os.getcwd()，import 会在脚下凭空造一个 data/ 目录。
运行时模式会在 import 之前把 ASTRBOT_ROOT 钉死，避免这个问题。

退出码 0 = 全部通过。
"""

import argparse
import ast
import importlib.metadata as importlib_metadata
import os
import sys

ADAPTER = "astrbot/core/provider/sources/minimax_embedding_source.py"
MANAGER = "astrbot/core/provider/manager.py"
DEFAULT_CFG = "astrbot/core/config/default.py"

# 文件 -> 必须出现的字符串片段
PATCH_CHECKS = [
    (ADAPTER, 'register_provider_adapter(\n    "minimax_embedding"'),
    (ADAPTER, "class MiniMaxEmbeddingProvider(EmbeddingProvider)"),
    (MANAGER, 'case "minimax_embedding":'),
    (MANAGER, "from .sources.minimax_embedding_source import ("),
    (DEFAULT_CFG, '"MiniMax Embedding": {'),
    (DEFAULT_CFG, '"type": "minimax_embedding"'),
    (DEFAULT_CFG, '"embedding_type": {'),
]

# 本镜像额外安装的依赖及其期望版本。
# 唯一来源是 requirements-extra.txt —— 声明只有一处，不会两处对不上。
REQ_FILE = os.environ.get("EMB_REQS", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "requirements-extra.txt"))


def _load_expected_deps(path):
    """从 requirements-extra.txt 读出 {包名: 版本}。"""
    out = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        out[name.strip().lower()] = ver.strip()
    return out


EXPECTED_DEPS = _load_expected_deps(REQ_FILE)

# 核心包（numpy/scipy/pandas/faiss-cpu）的期望版本不在这里写死。
# 写死的话，官方镜像一升级就会拿着过期的快照误报失败——
# 实测 10-25 官方镜像 pandas 3.0.6→3.1.0，构建因此失败。
# 正确做法：由 gen_constraints.py 在装包之前把当时的版本记下来，校验时跟它比。
# 包名固定，版本动态。

_failures = []
_passed = 0


def chk(name, cond, extra=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}" + (f" -> {extra}" if extra else ""))
    else:
        _failures.append(name)
        print(f"  FAIL  {name}" + (f" -> {extra}" if extra else ""))


def section(title):
    print(f"\n=== {title} ===")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _dist_version(name):
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def check_static(root, pins=None, pins_path=""):
    section("1. 补丁静态校验")
    for rel in [ADAPTER, MANAGER, DEFAULT_CFG]:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            chk(f"{rel} 存在", False, "文件缺失")
            continue
        if rel.endswith(".py"):
            try:
                ast.parse(_read(p))
                chk(f"{rel} 语法正确", True)
            except SyntaxError as e:
                chk(f"{rel} 语法正确", False, e)

    for rel, needle in PATCH_CHECKS:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        if needle not in _read(p):
            chk(f"{rel} 含 {needle[:40]!r}", False, "内容缺失")
    print(f"  内容锚点 {len(PATCH_CHECKS)} 项已核对")

    section("2. 补丁残留")
    found = []
    for b, _d, files in os.walk(root):
        if "site-packages" in b or "__pycache__" in b:
            continue
        found += [os.path.join(b, f) for f in files
                  if f.endswith((".rej", ".orig"))]
    chk("无 .rej/.orig 残留", not found, found)

    section("3. py 依赖元数据")
    if not EXPECTED_DEPS:
        chk(f"读取到依赖声明 ({REQ_FILE})", False, "文件缺失或为空")
    else:
        print(f"  声明来源: {REQ_FILE}（{len(EXPECTED_DEPS)} 项）")
    for name, want in EXPECTED_DEPS.items():
        got = _dist_version(name)
        if got is None:
            chk(f"{name} 已安装", False, "未安装")
        else:
            chk(f"{name} 版本正确", got == want, f"{got}（期望 {want}）")
    # 核心包：跟"装包之前"记录的版本比，确认这次安装没动它们
    if not pins:
        print("  未提供安装前版本记录，跳过核心包版本比对")
        for name in ("numpy", "scipy", "pandas"):
            got = _dist_version(name)
            chk(f"核心包 {name} 可用", got is not None, got or "未安装")
    else:
        print(f"  核心包期望版本来源: {pins_path}")
        for name, want in pins.items():
            got = _dist_version(name)
            chk(f"核心包 {name} 未被改动", got == want, f"{got}（期望 {want}）")


def check_runtime(root):
    section("4. Provider 注册表")
    M = None
    try:
        import astrbot.core.provider.sources.minimax_embedding_source as M
        from astrbot.core.provider.entities import ProviderType
        from astrbot.core.provider.register import provider_cls_map
        chk("provider_cls_map 含 minimax_embedding",
            "minimax_embedding" in provider_cls_map)
        pm = provider_cls_map.get("minimax_embedding")
        chk("provider_type 为 EMBEDDING",
            bool(pm) and pm.provider_type == ProviderType.EMBEDDING,
            pm.provider_type if pm else None)
    except Exception as e:
        chk("provider 模块可导入", False, e)

    section("5. 配置模板")
    try:
        from astrbot.core.config.default import CONFIG_METADATA_2
        tmpl = CONFIG_METADATA_2["provider_group"]["metadata"]["provider"][
            "config_template"]
        chk("模板含 MiniMax Embedding", "MiniMax Embedding" in tmpl)
        t = tmpl.get("MiniMax Embedding", {})
        chk("默认模型 embo-01", t.get("embedding_model") == "embo-01",
            t.get("embedding_model"))
        chk("默认维度 1536", t.get("embedding_dimensions") == 1536,
            t.get("embedding_dimensions"))
    except Exception as e:
        chk("配置元数据可读取", False, e)

    section("6. 实例化（不发网络请求）")
    if M is not None:
        cfg = {
            "id": "minimax_embedding",
            "embedding_api_key": "dummy-key-for-construction-check",
            "embedding_api_base": "https://api.minimax.chat/v1",
            "embedding_model": "embo-01",
            "embedding_type": "db",
            "embedding_dimensions": 1536,
            "minimax-group-id": "0",
            "timeout": 10,
        }
        try:
            p = M.MiniMaxEmbeddingProvider(cfg, {})
            chk("get_dim()==1536", p.get_dim() == 1536, p.get_dim())
            chk("URL 拼接正确",
                p._build_url() ==
                "https://api.minimax.chat/v1/embeddings?GroupId=0",
                p._build_url())
            bad = dict(cfg)
            bad["embedding_api_key"] = ""
            try:
                M.MiniMaxEmbeddingProvider(bad, {})
                chk("空 Key 应抛 ValueError", False, "未抛异常")
            except ValueError:
                chk("空 Key 应抛 ValueError", True)
        except Exception as e:
            chk("provider 可实例化", False, e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", action="store_true", help="追加运行时校验")
    ap.add_argument("--root", default=os.environ.get("EMB_VERIFY_ROOT", "/AstrBot"))
    ap.add_argument("--core-pins", default=os.environ.get("EMB_CORE_PINS", ""),
                    help="核心包基准版本 JSON。不指定时自动找镜像里的 "
                         "/AstrBot/.emb-core-pins.json")
    args = ap.parse_args()

    root = args.root
    if args.runtime:
        # 必须在 import astrbot 之前设置
        os.environ["ASTRBOT_ROOT"] = root

    pins = None
    pins_path = args.core_pins
    if not pins_path:
        # 镜像里自带的那份（构建时从原版镜像记录）
        default_pins = os.path.join(root, ".emb-core-pins.json")
        if os.path.isfile(default_pins):
            pins_path = default_pins
    if pins_path:
        if not os.path.isfile(pins_path):
            # 显式要求了却拿不到，必须判失败。
            # 静默降级成"存在性检查"等于把校验废了——
            # 上游镜像升级时就会重演"版本快照过期"的老问题。
            print(f"[verify] FATAL: 找不到核心包版本记录 {pins_path}，"
                  "无法校验核心包是否被改动", file=sys.stderr)
            return 2
        import json
        pins = json.load(open(pins_path, encoding="utf-8"))
        if not pins:
            print(f"[verify] FATAL: {pins_path} 内容为空，"
                  "无法校验核心包是否被改动", file=sys.stderr)
            return 2

    print(f"[verify] 目标: {root}  模式: {'静态+运行时' if args.runtime else '静态'}")
    check_static(root, pins, pins_path)
    if args.runtime:
        check_runtime(root)

    total = _passed + len(_failures)
    print()
    if _failures:
        print(f"RESULT: HAS FAILURE ({_passed}/{total} 通过)")
        for f in _failures:
            print(f"  失败项: {f}")
        return 1
    print(f"RESULT: ALL PASS ({_passed}/{total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

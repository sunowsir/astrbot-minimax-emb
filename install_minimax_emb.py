"""幂等注入器：把 MiniMax Embedding 适配器装进 AstrBot 源码树。

为什么不用补丁（.patch）：
    补丁依赖上游文件的具体内容，上游一改版就套不上，脚本随即失效。
    本注入器只依赖几个稳定的锚点字符串（provider 类型名、配置项名），
    上游增删其它 provider 都不影响。反复跑也安全——已注入过就跳过。

改动三处：
    1. 新增 astrbot/core/provider/sources/minimax_embedding_source.py（整文件拷贝）
    2. astrbot/core/provider/manager.py 增加一个 case 分支
    3. astrbot/core/config/default.py 增加配置模板 + 字段元数据

针对"上游重构冲突"的加固：
    A. 注入代码的缩进从锚点行自动继承，不写死。上游改缩进不会插出语法错误。
    B. 判重看 type 值（"type": "minimax_embedding"）而不是显示名。
       上游若自己出了同 type 但换了显示名，我们不会插出第二份。
    C. 适配器源文件带 managed 标记。已存在的文件若无此标记，
       判定为上游自己的实现，保留不覆盖。

用法（在镜像构建阶段执行）：
    python install_minimax_emb.py --source /tmp/minimax_embedding_source.py
"""

import argparse
import os
import shutil
import sys

ROOT = os.environ.get("EMB_INSTALL_ROOT", "/AstrBot")

MANAGER = "astrbot/core/provider/manager.py"
DEFAULT_CFG = "astrbot/core/config/default.py"
TARGET = "astrbot/core/provider/sources/minimax_embedding_source.py"

# 我们的适配器文件里的标记。用于区分"我们注入的"和"上游自带的"。
MARKER = "astrbot-minimax-emb: managed"

# manager.py 里用来定位插入点的 case 分支，按优先级尝试。
# 这些都是 provider 类型名，上游增删别的 provider 不会影响它们。
MANAGER_ANCHORS = [
    "dashscope_embedding",
    "nvidia_embedding",
    "ollama_embedding",
    "gemini_embedding",
    "openai_embedding",
]

# default.py config_template 里用来定位的配置模板名
TEMPLATE_ANCHORS = [
    "DashScope Embedding",
    "NVIDIA Embedding",
    "Ollama Embedding",
    "Gemini Embedding",
    "OpenAI Embedding",
]

# {i} 会被替换成锚点行的缩进
MANAGER_BLOCK_TMPL = """{i}case "minimax_embedding":
{i}    from .sources.minimax_embedding_source import (
{i}        MiniMaxEmbeddingProvider as MiniMaxEmbeddingProvider,
{i}    )
"""

TEMPLATE_BLOCK_TMPL = """{i}"MiniMax Embedding": {{
{i}    "id": "minimax_embedding",
{i}    "type": "minimax_embedding",
{i}    "provider": "minimax",
{i}    "provider_type": "embedding",
{i}    "enable": True,
{i}    "embedding_api_key": "",
{i}    "embedding_api_base": "https://api.minimax.chat/v1",
{i}    "embedding_model": "embo-01",
{i}    "embedding_type": "db",
{i}    "embedding_dimensions": 1536,
{i}    "minimax-group-id": "",
{i}    "timeout": 60,
{i}    "proxy": "",
{i}}},
"""

METADATA_BLOCK_TMPL = """{i}"embedding_type": {{
{i}    "description": "嵌入用途",
{i}    "type": "string",
{i}    "options": ["db", "query"],
{i}    "hint": "MiniMax 专有参数。db 用于文档入库向量化，query 用于检索查询向量化。AstrBot 目前对两者调用同一接口，默认使用 db。",
{i}}},
"""


class InstallError(RuntimeError):
    pass


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _indent_of(line):
    return len(line) - len(line.lstrip())


def _find_case_block_end(lines, start):
    """case 分支的结束位置（返回"插到它后面"的行号）。

    优先用"下一个同缩进的 case"作为边界——case 分支内部结构多变
    （import 多行、赋值……），比匹配右花括号稳得多。
    若该 case 已是 match 里最后一个，则回退到 match 块的末尾。
    """
    indent = _indent_of(lines[start])
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.strip().startswith("case ") and _indent_of(s) == indent:
            return i
    # 最后一个 case：找 match 块的结束（缩进回到 case 层级或更浅）
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if not s.strip():
            continue
        if _indent_of(s) < indent:
            return i
    return len(lines)


def _find_dict_block_end(lines, start):
    """字典条目的结束：同缩进的 '},'。若它是父字典最后一项，回退到父字典末尾。"""
    indent = _indent_of(lines[start])
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.strip() in ("},", "}") and _indent_of(s) == indent:
            return i
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if not s.strip():
            continue
        if _indent_of(s) < indent:
            return i
    return len(lines)


def _render(tmpl, anchor_line):
    """按锚点行的缩进渲染注入块。"""
    indent = " " * _indent_of(anchor_line)
    return tmpl.format(i=indent)


def install_manager_case():
    path = os.path.join(ROOT, MANAGER)
    if not os.path.isfile(path):
        raise InstallError(f"找不到 {MANAGER}（ROOT={ROOT}）")
    text = _read(path)

    if 'case "minimax_embedding":' in text:
        print("[install] manager.py：已存在 minimax_embedding 分支，跳过"
              "（上游若自带同名 provider，说明它已自己实现了这个能力）")
        return False

    lines = text.splitlines(keepends=True)
    available = [s.strip() for s in lines if s.strip().startswith('case "')]

    for anchor in MANAGER_ANCHORS:
        needle = f'case "{anchor}":'
        idx = next((i for i, s in enumerate(lines) if s.strip() == needle), None)
        if idx is None:
            continue
        end = _find_case_block_end(lines, idx)
        if end is None:
            raise InstallError(
                f"在 {MANAGER} 中定位到 {needle}，但找不到它的结束位置。"
                "上游结构可能已变，请检查 MANAGER_ANCHORS。"
            )
        block = _render(MANAGER_BLOCK_TMPL, lines[idx])
        lines[end:end] = [block]
        _write(path, "".join(lines))
        print(f"[install] manager.py：已在 {needle} 之后插入 minimax_embedding 分支"
              f"（缩进 {_indent_of(lines[idx])} 空格）")
        return True

    raise InstallError(
        f"{MANAGER} 里找不到任何可用的锚点分支。\n"
        f"已尝试：{MANAGER_ANCHORS}\n"
        f"该文件现有 case 共 {len(available)} 个，例如：{available[:8]}\n"
        f"请在 install_minimax_emb.py 的 MANAGER_ANCHORS 里补一个。"
    )


def install_config_template():
    path = os.path.join(ROOT, DEFAULT_CFG)
    if not os.path.isfile(path):
        raise InstallError(f"找不到 {DEFAULT_CFG}（ROOT={ROOT}）")
    text = _read(path)

    # 按 type 判重，而不是显示名。上游若自己出了同 type 的 provider
    # 但显示名不同，我们不能插出第二份——那会造成配置冲突。
    if '"type": "minimax_embedding"' in text:
        print("[install] default.py：已存在 type=minimax_embedding 的配置模板，跳过"
              "（按 type 判定，显示名不同也算已存在，避免插出第二份）")
        return False

    lines = text.splitlines(keepends=True)
    for anchor in TEMPLATE_ANCHORS:
        needle = f'"{anchor}": {{'
        idx = next((i for i, s in enumerate(lines) if s.strip() == needle), None)
        if idx is None:
            continue
        end = _find_dict_block_end(lines, idx)
        if end is None:
            raise InstallError(
                f"在 {DEFAULT_CFG} 中定位到 {needle}，但找不到它的结束位置。"
            )
        block = _render(TEMPLATE_BLOCK_TMPL, lines[idx])
        lines[end + 1:end + 1] = [block]
        _write(path, "".join(lines))
        print(f"[install] default.py：已在 {needle} 之后插入 MiniMax Embedding 模板"
              f"（缩进 {_indent_of(lines[idx])} 空格）")
        return True

    raise InstallError(
        f"{DEFAULT_CFG} 里找不到任何可用的锚点配置模板。\n"
        f"已尝试：{TEMPLATE_ANCHORS}\n"
        f"请在 install_minimax_emb.py 的 TEMPLATE_ANCHORS 里补一个。"
    )


def install_metadata():
    path = os.path.join(ROOT, DEFAULT_CFG)
    text = _read(path)
    if '"embedding_type": {' in text:
        print("[install] default.py：已存在 embedding_type 元数据，跳过")
        return False

    lines = text.splitlines(keepends=True)
    needle = '"embedding_model": {'
    idx = next((i for i, s in enumerate(lines) if s.strip() == needle), None)
    if idx is None:
        raise InstallError(
            f"{DEFAULT_CFG} 里找不到 {needle}，无法插入 embedding_type 元数据。"
        )
    end = _find_dict_block_end(lines, idx)
    if end is None:
        raise InstallError(f"在 {DEFAULT_CFG} 中找不到 {needle} 的结束位置。")
    block = _render(METADATA_BLOCK_TMPL, lines[idx])
    lines[end + 1:end + 1] = [block]
    _write(path, "".join(lines))
    print(f"[install] default.py：已插入 embedding_type 字段元数据"
          f"（缩进 {_indent_of(lines[idx])} 空格）")
    return True


def install_source(source_path):
    dst = os.path.join(ROOT, TARGET)
    if not os.path.isfile(source_path):
        raise InstallError(f"找不到适配器源文件: {source_path}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    new_text = _read(source_path)

    if os.path.isfile(dst):
        old_text = _read(dst)
        if old_text == new_text:
            print("[install] 适配器源文件：已存在且内容一致，跳过")
            return False
        if MARKER not in old_text:
            print("[install] 适配器源文件：已存在但无 managed 标记，"
                  "判定为上游自带实现，保留不覆盖")
            print(f"[install]   现有文件首行: {old_text.splitlines()[0][:70]}")
            return False
        print("[install] 适配器源文件：带 managed 标记，更新为本次版本")

    shutil.copyfile(source_path, dst)
    print(f"[install] 适配器源文件：已安装 -> {TARGET}")
    return True


def main():
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="minimax_embedding_source.py 路径")
    ap.add_argument("--root", default=ROOT, help="AstrBot 源码根目录")
    args = ap.parse_args()

    ROOT = args.root
    print("[install] ===== 注入开始 =====")
    print(f"[install] 目标源码根 : {ROOT}")
    print(f"[install] 适配器源文件: {args.source}")

    changed = 0
    try:
        changed += install_source(args.source)
        changed += install_manager_case()
        changed += install_config_template()
        changed += install_metadata()
    except InstallError as e:
        print(f"\n[install] 失败：{e}", file=sys.stderr)
        return 1

    print(f"[install] ===== 注入完成，本次改动 {changed} 处 =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

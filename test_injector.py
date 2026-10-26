"""上游重构冲突测试套件。

目的：证明当官方 AstrBot 改版时，注入器不会跟新版本打架。

做法：拿官方原版的 manager.py / default.py 作底稿，人为模拟上游的各类改法，
再跑注入器，检查结果是否符合预期。

覆盖场景：
  S1 基线            上游没动，注入应成功
  S2 无关增删        上游新增/删除别的 provider，注入应成功
  S3 主锚点消失      上游删掉 dashscope_embedding，应回退到 nvidia
  S4 全部锚点消失    应明确失败并给出可操作提示
  S5 上游自带同名     上游自己出了 minimax_embedding，不应重复注入、不该覆盖其实现
  S6 缩进变化        上游改了 case 块缩进，注入代码应跟着适配
  S7 模板改名        上游改了配置模板显示名，应回退
  S8 幂等            连续注入两次，第二次应零改动

用法：python test_injector.py
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INJECTOR = os.path.join(HERE, "install_minimax_emb.py")
SOURCE = os.path.join(HERE, "minimax_embedding_source.py")

# 官方原版文件（从 soulter/astrbot:latest 里取出来的）
PRISTINE = {
    "manager": "/tmp/pristine_manager.py",
    "default": "/tmp/pristine_default.py",
}

BASE_IMAGE = "soulter/astrbot:latest"


def fetch_pristine():
    """底稿缺失时，自动从本地原版镜像里取。

    底稿放在 /tmp 是临时的，容器一重建就没了——测试套件得能自给自足。
    """
    if all(os.path.isfile(p) for p in PRISTINE.values()):
        return True
    try:
        import io
        import tarfile

        from docker_api import api
    except ImportError:
        return False

    st, res = api("POST", "/containers/create?name=emb-grab-pristine",
                  {"Image": BASE_IMAGE, "Cmd": ["true"], "NetworkMode": "none"})
    if st != 201:
        return False
    cid = res["Id"]
    try:
        for src, dst in [
            ("/AstrBot/astrbot/core/provider/manager.py", PRISTINE["manager"]),
            ("/AstrBot/astrbot/core/config/default.py", PRISTINE["default"]),
        ]:
            _s, raw = api("GET", f"/containers/{cid}/archive?path={src}", raw=True)
            tf = tarfile.open(fileobj=io.BytesIO(raw))
            with open(dst, "w", encoding="utf-8") as f:
                f.write(tf.extractfile(tf.getmembers()[0]).read().decode("utf-8"))
        return True
    except Exception:
        return False
    finally:
        api("DELETE", f"/containers/{cid}?force=1")

_passed = 0
_failed = []


def check(name, cond, extra=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}" + (f" -> {extra}" if extra else ""))
    else:
        _failed.append(name)
        print(f"  FAIL  {name}" + (f" -> {extra}" if extra else ""))


def make_tree(mgr=None, dft=None):
    """造一棵源码树，返回根目录。mgr/dft 为处理函数。"""
    root = tempfile.mkdtemp(prefix="embtest-")
    os.makedirs(os.path.join(root, "astrbot/core/provider/sources"))
    os.makedirs(os.path.join(root, "astrbot/core/config"))
    m = open(PRISTINE["manager"], encoding="utf-8").read()
    d = open(PRISTINE["default"], encoding="utf-8").read()
    if mgr:
        m = mgr(m)
    if dft:
        d = dft(d)
    open(os.path.join(root, "astrbot/core/provider/manager.py"), "w",
         encoding="utf-8").write(m)
    open(os.path.join(root, "astrbot/core/config/default.py"), "w",
         encoding="utf-8").write(d)
    return root


def run_injector(root):
    r = subprocess.run(
        [sys.executable, INJECTOR, "--source", SOURCE, "--root", root],
        capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def read(root, rel):
    return open(os.path.join(root, rel), encoding="utf-8").read()


def parses(root):
    for rel in ["astrbot/core/provider/manager.py", "astrbot/core/config/default.py",
                "astrbot/core/provider/sources/minimax_embedding_source.py"]:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            return False, f"{rel} 不存在"
        try:
            ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError as e:
            return False, f"{rel} 语法错误: {e}"
    return True, ""


# ---------- 上游改法模拟 ----------

def s2_unrelated(m=None, d=None):
    """上游新增一个 provider、删掉一个无关 provider。"""
    def f_mgr(s):
        s = s.replace('            case "volcengine_tts":',
                      '            case "brand_new_provider":\n'
                      '                from .sources.brand_new import (\n'
                      '                    BrandNewProvider as BrandNewProvider,\n'
                      '                )\n'
                      '            case "volcengine_tts":')
        return s.replace('            case "kimi_code_chat_completion":\n'
                         '                from .sources.kimi_code_source import (\n'
                         '                    KimiCodeProvider as KimiCodeProvider,\n'
                         '                )\n', "")
    return f_mgr


def s3_no_dashscope(m=None, d=None):
    def f_mgr(s):
        return re.sub(r'            case "dashscope_embedding":\n(?:                .*\n)+',
                      "", s)
    return f_mgr


def s4_no_anchors(m=None, d=None):
    def f_mgr(s):
        for a in ["dashscope_embedding", "nvidia_embedding", "ollama_embedding",
                  "gemini_embedding", "openai_embedding"]:
            s = re.sub(rf'            case "{a}":\n(?:                .*\n)+', "", s)
        return s
    return f_mgr


def s5_upstream_has_own(m=None, d=None):
    """上游自己实现了 minimax_embedding（显示名还不一样）。"""
    def f_mgr(s):
        return s.replace('            case "volcengine_tts":',
                          '            case "minimax_embedding":\n'
                          '                from .sources.minimax_embedding_source import (\n'
                          '                    UpstreamMiniMaxProvider as UpstreamMiniMaxProvider,\n'
                          '                )\n'
                          '            case "volcengine_tts":')

    def f_dft(s):
        blk = ('                    "MiniMax 自家嵌入": {\n'
               '                        "id": "minimax_embedding",\n'
               '                        "type": "minimax_embedding",\n'
               '                        "provider": "minimax",\n'
               '                        "provider_type": "embedding",\n'
               '                        "enable": True,\n'
               '                    },\n')
        return s.replace('                    "vLLM Rerank": {', blk + '                    "vLLM Rerank": {')
    return f_mgr, f_dft


def s6_indent_change(m=None, d=None):
    """上游把整个 match 块整体缩进减少 4 格。

    注意不能只挪其中几个 case——Python 的 match/case 要求所有 case 同缩进，
    只挪一部分会直接把文件改坏，那测的就不是"注入器能不能适应"了。
    这里做的是标准的"整块 dedent"：找到 match type: 之后所有更深缩进的行一起挪。
    """
    def f_mgr(s):
        lines = s.splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip() == "match type:")
        base = len(lines[start]) - len(lines[start].lstrip())
        # 找到 match 块的结束：其后第一个非空且缩进 <= base 的行
        end = len(lines)
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if not ln.strip():
                continue
            ind = len(ln) - len(ln.lstrip())
            if ind <= base:
                end = i
                break
        out = list(lines[:start])
        # match 行本身 + 块内所有更深缩进的行，一起减 4 格
        for line in lines[start:end]:
            if line.strip() and line.startswith("    "):
                out.append(line[4:])
            else:
                out.append(line)
        out += lines[end:]
        return "".join(out)
    return f_mgr


def s7_template_renamed(m=None, d=None):
    def f_dft(s):
        return s.replace('"DashScope Embedding": {', '"DashScope 文本嵌入": {')
    return f_dft


# ---------- 用例 ----------

def case_s1():
    print("\n[S1] 基线：上游没动")
    root = make_tree()
    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    ok, why = parses(root)
    check("注入后语法正确", ok, why)
    check("manager 有分支", 'case "minimax_embedding":' in read(root, "astrbot/core/provider/manager.py"))
    check("default 有模板", '"MiniMax Embedding": {' in read(root, "astrbot/core/config/default.py"))
    shutil.rmtree(root, ignore_errors=True)


def case_s2():
    print("\n[S2] 上游无关增删：新增 provider、删掉一个无关 case")
    root = make_tree(mgr=s2_unrelated())
    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    ok, why = parses(root)
    check("注入后语法正确", ok, why)
    m = read(root, "astrbot/core/provider/manager.py")
    check("新 provider 未被动", 'case "brand_new_provider":' in m)
    check("注入分支存在", 'case "minimax_embedding":' in m)
    shutil.rmtree(root, ignore_errors=True)


def case_s3():
    print("\n[S3] 上游删掉主锚点 dashscope_embedding → 应回退")
    root = make_tree(mgr=s3_no_dashscope())
    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    check("回退到 nvidia 锚点", "nvidia_embedding" in out, [ln for ln in out.splitlines() if "manager.py" in ln])
    ok, why = parses(root)
    check("注入后语法正确", ok, why)
    shutil.rmtree(root, ignore_errors=True)


def case_s4():
    print("\n[S4] 上游删掉全部锚点 → 应明确失败并给提示")
    root = make_tree(mgr=s4_no_anchors())
    rc, out = run_injector(root)
    check("返回非 0", rc != 0)
    check("报出可操作提示", "MANAGER_ANCHORS" in out and "现有 case" in out)
    shutil.rmtree(root, ignore_errors=True)


def case_s5():
    print("\n[S5] 上游自带 minimax_embedding（显示名不同）→ 不重复注入、不覆盖")
    fm, fd = s5_upstream_has_own()
    root = make_tree(mgr=fm, dft=fd)
    # 放一个上游自己的适配器实现（无 managed 标记）
    upstream_src = ("# upstream own implementation\n"
                    "class UpstreamMiniMaxProvider:\n"
                    "    pass\n")
    open(os.path.join(root, "astrbot/core/provider/sources/minimax_embedding_source.py"),
         "w", encoding="utf-8").write(upstream_src)

    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    m = read(root, "astrbot/core/provider/manager.py")
    n_case = m.count('case "minimax_embedding":')
    check("manager 未出现重复分支", n_case == 1, f"出现 {n_case} 次")
    d = read(root, "astrbot/core/config/default.py")
    n_type = d.count('"type": "minimax_embedding"')
    check("default 未出现重复 type", n_type == 1, f"出现 {n_type} 次")
    now = read(root, "astrbot/core/provider/sources/minimax_embedding_source.py")
    check("上游自己的实现未被覆盖", "upstream own implementation" in now)
    ok, why = parses(root)
    check("最终语法正确", ok, why)
    shutil.rmtree(root, ignore_errors=True)


def case_s6():
    print("\n[S6] 上游改了缩进 → 注入代码应跟着适配")
    root = make_tree(mgr=s6_indent_change())
    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    ok, why = parses(root)
    check("注入后语法正确（关键）", ok, why)
    m = read(root, "astrbot/core/provider/manager.py")
    check("分支缩进与锚点一致",
          re.search(r'\n(\s+)case "minimax_embedding":', m) is not None)
    shutil.rmtree(root, ignore_errors=True)


def case_s7():
    print("\n[S7] 上游改了配置模板显示名 → 应回退")
    root = make_tree(dft=s7_template_renamed())
    rc, out = run_injector(root)
    check("注入成功", rc == 0, out.strip().splitlines()[-1] if out else "")
    check("回退到别的锚点", "NVIDIA Embedding" in out or "Ollama Embedding" in out,
          [ln for ln in out.splitlines() if "default.py" in ln])
    ok, why = parses(root)
    check("注入后语法正确", ok, why)
    shutil.rmtree(root, ignore_errors=True)


def case_s8():
    print("\n[S8] 幂等：连续注入两次")
    root = make_tree()
    run_injector(root)
    before = {rel: read(root, rel) for rel in
              ["astrbot/core/provider/manager.py", "astrbot/core/config/default.py",
               "astrbot/core/provider/sources/minimax_embedding_source.py"]}
    rc, out = run_injector(root)
    check("第二次返回 0", rc == 0)
    check("第二次零改动", "本次改动 0 处" in out,
          [ln for ln in out.splitlines() if "本次改动" in ln])
    after = {rel: read(root, rel) for rel in before}
    check("文件内容逐字节不变", before == after)
    shutil.rmtree(root, ignore_errors=True)


def main():
    missing = [p for p in PRISTINE.values() if not os.path.isfile(p)]
    if missing:
        print(f"缺少官方原版底稿 {missing}，尝试从 {BASE_IMAGE} 自动获取 ...")
        if fetch_pristine():
            print("  已自动获取")
        else:
            print("  自动获取失败。请手动执行：", file=sys.stderr)
            print(f"  docker create --name emb-grab {BASE_IMAGE} true", file=sys.stderr)
            print(f"  docker cp emb-grab:/AstrBot/astrbot/core/provider/manager.py "
                  f"{PRISTINE['manager']}", file=sys.stderr)
            print(f"  docker cp emb-grab:/AstrBot/astrbot/core/config/default.py "
                  f"{PRISTINE['default']}", file=sys.stderr)
            print("  docker rm emb-grab", file=sys.stderr)
            return 2
    for fn in [case_s1, case_s2, case_s3, case_s4, case_s5, case_s6, case_s7, case_s8]:
        fn()
    total = _passed + len(_failed)
    print()
    if _failed:
        print(f"RESULT: HAS FAILURE ({_passed}/{total} 通过)")
        for f in _failed:
            print(f"  失败项: {f}")
        return 1
    print(f"RESULT: ALL PASS ({_passed}/{total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

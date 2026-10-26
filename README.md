# MiniMax Embedding 适配器 · 构建套件

> 本项目完全由AI生成，当前本地环境使用正常，但不做任何质量保证

给 AstrBot 装 MiniMax 文本嵌入模型（embo-01，1536 维），并保证官方镜像升级后不丢失。

当前构建产物已通过完整验证：`astrbot-minimax-emb:latest`。

---

## 注意

1. 不要点"拉取/更新镜像"——自建镜像，Hub 上没有。
2. 不要删 data 目录——知识库和 85 条聊天记录都在里面。
3. 回滚前先备份 `data/cmd_config.json`，否则残留的 provider 配置可能让启动报错。

---

## 一条命令生成镜像

**镜像已经建好了**（`astrbot-minimax-emb:latest`，sha `141d1ab955ae`，2067 MB），
选镜像时直接挑它即可。

重建镜像，终端执行：

```bash
docker build -t astrbot-minimax-emb:latest \
  -f /path/to/Dockerfile \
  path/to/data
```

context 必须是 `data` 目录（不是 `astrbot-minimax-emb`），
因为 Dockerfile 里的 `COPY astrbot-minimax-emb/xxx` 是相对 context 根的。
`data/.dockerignore` 会自动生效，上下文只有几十 KB。

没有 docker CLI、只有 socket 的时候，用本目录的脚本：

```bash
cd /path/to/astrbot-minimax-emb
python3 docker_build.py
```

构建日志里这三行齐全才算成功：

```
[constraints] 已启用核心依赖版本保护 (62 个约束)
   2.5.4 1.18.1 3.0.6                    # numpy/scipy/pandas
[verify] 全部通过（7 锚点 + 4 依赖 + 3 核心版本）
```

⚠ 面板上的"拉取/更新镜像"不要点——Docker Hub 上没有这个自建镜像。

---

## 快速上手

镜像**已经构建好并留在宿主 daemon 里**了，所以最省事的路径是三步：

```bash
# 1. 改宿主根目录的 compose.yml，把 image 换成自建镜像
#    image: soulter/astrbot:latest   →   image: astrbot-minimax-emb:latest
#    （先不用管 build 段，镜像已经在了）

# 2. 重建容器
docker compose up -d

# 3. 确认新容器里的适配器和依赖都在
docker exec astrbot python /AstrBot/data/astrbot-minimax-emb/verify.py --runtime
```

第 3 步应当输出 `RESULT: ALL PASS (22/22)`。
`data` 卷不动，配置、知识库、聊天记录全部保留。

### 以后升级官方 AstrBot 镜像时

compose 里补上 `build:` 段（见 `docker-compose.yml`），然后：

```bash
# 改 Dockerfile 第一行的 FROM，换成新的官方 tag
docker compose up -d --build
```

补丁会在每次构建时重新套用，装依赖时仍带核心依赖约束。

### 日常命令速查

| 目的 | 命令（在 `data/astrbot-minimax-emb/` 下执行） |
|---|---|
| 重建镜像 | `python docker_build.py` |
| 验镜像（19 项） | `python docker_verify.py` |
| 验启动（真起一次） | `python docker_boot_test.py` |
| 容器内自查 | `docker exec astrbot python /AstrBot/data/astrbot-minimax-emb/verify.py --runtime` |
| 回滚 | compose 改回 `soulter/astrbot:latest`，删掉 `build:` 段，再 `up -d` |

### 回滚前务必注意

退回官方镜像后，`cmd_config.json` 里的 `minimax_embedding` provider 配置还留着，
但适配器没了，可能导致启动报错。先备份：

```bash
cp data/cmd_config.json data/cmd_config.json.bak
```

---

## 一、文件清单

| 文件 | 作用 | 何时用 |
|---|---|---|
| `build.sh` | **入口**：从原版镜像生成新镜像 | 日常主命令 |
| `Dockerfile` | 叠加层定义（`ARG BASE_IMAGE` 换原版） | 构建时 |
| `install_minimax_emb.py` | **幂等注入器**，把适配器装进源码树 | 构建时 |
| `minimax_embedding_source.py` | 适配器源码（整文件） | 构建时 |
| `gen_constraints.py` | 生成核心依赖约束（复刻官方 `CoreConstraintsProvider`） | 构建时 |
| `requirements-extra.txt` | 四个额外依赖的版本钉死（唯一版本声明处） | 升级依赖时 |
| `verify.py` | 校验，`--runtime` 追加运行时检查 | 构建时 / 自查 |
| `test_injector.py` | **上游改版冲突测试套件**（8 场景 27 项） | 改注入器后 |
| `docker_api.py` | 极简 Engine API 客户端 | 被下面两个引用 |
| `docker_build.py` | 无 docker CLI 时的构建实现 | 可选 |
| `docker_verify.py` | 临时容器跑校验，`--boot` 追加启动测试 | 可选 |
| `docker-compose.yml` | compose 参考（切容器时用） | 切换容器时 |
| `README.md` | 本文件 | — |

配套还有宿主侧 `../.dockerignore`，把构建上下文压到只剩本目录。

---

### 不会跟新版本镜像冲突

三条保障，逐层递进：

1. **不靠补丁，靠锚点**。早期版本用 `.patch`，依赖上游文件的具体内容，
   上游一改版就套不上。现在只认 provider 类型名、配置项名这类稳定字符串。
2. **锚点会回退**。`dashscope_embedding → nvidia_embedding → ollama_embedding
   → gemini_embedding → openai_embedding`，上游删掉前者就自动挂后者。
   全都不行时构建明确失败，并打印该文件现有的 case 列表，照着补一个即可。
3. **注入结果逐项校验**。构建末尾跑 `verify.py`，任何一项不过就让构建失败。

此外还有三处针对重构的专门加固：

| 风险 | 加固方式 |
|---|---|
| 上游改缩进 | 注入代码的缩进从锚点行**自动继承**，不写死 |
| 上游自己出了 minimax_embedding | 按 **type 值**判重（不是显示名），不插第二份；其源码无 managed 标记，**不覆盖** |
| 适配器源码被误改 | 我们的文件带 `astrbot-minimax-emb: managed` 标记，只有带标记的才允许覆盖 |

`test_injector.py` 拿官方原版文件作底稿，模拟 8 种上游改法逐一验证：

```
S1 基线            上游没动，注入应成功
S2 无关增删        上游新增/删除别的 provider，应成功
S3 主锚点消失      删掉 dashscope，应回退到 nvidia
S4 全部锚点消失    应明确失败并给可操作提示
S5 上游自带同名     不重复注入、不覆盖其实现
S6 缩进变化        整块 dedent，注入代码应跟着适配
S7 模板改名        改了配置模板显示名，应回退
S8 幂等            连续注入两次，第二次应零改动
```

跑法：`python test_injector.py`（需要官方原版的 manager.py / default.py 作底稿）

---

## 三、构建

```bash
cd /AstrBot/data/astrbot-minimax-emb      # 容器内
python docker_build.py                    # 或宿主有 CLI：docker compose build
```

关键日志（三行齐全才算成功）：

```
[constraints] 已启用核心依赖版本保护 (62 个约束)
   2.5.4 1.18.1 3.0.6                      # numpy/scipy/pandas
[verify] 全部通过（7 锚点 + 4 依赖 + 3 核心版本）
```

构建期任一校验失败会让构建直接失败，而不是打条警告放行。

---


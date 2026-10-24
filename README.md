<div align="center">

<img src="assets/hero.png" alt="ask-gpt" width="560">

# ask-gpt

**让 GPT 加入当前对话的工作流 —— 任何支持 skill 与 shell 的 AI agent 都能调用你自己的 OpenAI 账号**

问答 · 读图 · 生图 ｜ 订阅额度或 API Key 双路径 ｜ 模型按账号实时拉取

[![Agent Skill](https://img.shields.io/badge/Agent-Skill-8A63D2)](SKILL.md)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-3776AB)](https://www.python.org/)
[![Zero dependencies](https://img.shields.io/badge/依赖-仅标准库-success)](scripts/ask_gpt.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

</div>

---

## 为什么需要它

你在用的 AI agent —— Claude Code、WorkBuddy，或任何能装 skill、能跑 shell 的助手 —— 很强，但有几类事一个模型干不好：

| 场景 | 问题 |
|---|---|
| **事实核查** | 单一模型的幻觉自己查不出来，需要另一个模型**独立作答**再比对 |
| **论文润色** | 不同模型的语感差异很大，对照两版再定稿，质量明显高于单版 |
| **识图 / 生图** | 有时就是想听 GPT 对一张图的判断，或者想要它的出图风格 |

常见做法是**切到另一个窗口**问 GPT，再把结果复制回来 —— 上下文断了，也说不清哪段是谁写的。

ask-gpt 让 GPT **直接在当前对话里**参与进来：宿主 agent 负责对话和决策，把某一步交给 GPT，结果回到同一个对话继续往下走。

而且不用再掏一份 API 费：很多人已经在付 ChatGPT 包月订阅，那份额度本来就能用。

```
已有 ChatGPT 订阅  → 走 Codex CLI，消耗包月额度，不产生任何额外账单
只有 API Key      → 走 OpenAI API，按 token 计费，生图尺寸可控
两条都有          → 停下来问你走哪条（费用差异大，不替你决定）
```

---

## 让 GPT 参与工作流

一个典型的例子：写论文讨论部分，起草和修改由宿主 agent 做，审稿交给 GPT，你只做决定。

```mermaid
sequenceDiagram
    autonumber
    actor U as 你
    box rgba(0,121,107,0.10) 你的电脑
        participant A as 宿主 Agent
        participant S as ask_gpt.py
    end
    box rgba(255,179,0,0.12) OpenAI
        participant G as GPT
    end

    U->>A: 写讨论部分，再让 GPT 审一遍
    A->>A: 起草段落
    rect rgba(94,53,177,0.10)
        Note over A,G: 交给 GPT 的一步
        A->>S: --json -f draft.md
        S->>G: 订阅额度 或 API
        G-->>S: 审稿意见与修改版
        S-->>A: ok / model / answer
    end
    A->>U: 原文与 GPT 修改版逐段对照
    U->>A: 采纳第 2、4 条
    A->>A: 按意见修改
    A->>U: 定稿，标明哪些出自 GPT
```

整个过程在**一个对话**里完成，每一步是谁做的都清清楚楚。

技能内置五种协作方式，宿主 agent 会按场景选：

| 方式 | 做法 |
|---|---|
| **直接提问** | 你的问题原样转发，不加不减，回答标明出自 GPT |
| **第二意见（盲评）** | 只发原始问题让 GPT 独立作答，**不附上宿主 agent 的结论**，避免锚定；GPT 也看不到你的个人配置（见下节）；再汇报一致点、分歧点、综合判断 |
| **审稿 / 对照润色** | GPT 修改版与原文逐段对照，交你定稿，**不擅自合并两版** |
| **识图** | 图和问题一起发；复杂影像先问清你想让 GPT 看什么 |
| **流水线中的一步** | 宿主起草 → GPT 审 → 宿主修改 → 你定稿；或 GPT 出图 → 宿主嵌入文档 |

---

## 盲评上下文隔离

"盲评"要求 GPT 只看到问题本身。但 codex 默认会把你的**个人配置**一起交给模型。开发时用 `codex debug prompt-input` 把模型实际收到的输入导出来，发现了这些：

| 混进上下文的东西 | 隔离前 | 隔离后 |
|---|---|---|
| 你的全局 `AGENTS.md` | ⚠️ 在（每次调用都会执行里面的 `say` 指令） | ✅ 不在 |
| 你装的技能清单（12 个） | ⚠️ 10.6K 字 | ✅ 不在 |
| 桌面控制工具 `mcp__cua_repl` | ⚠️ GPT 回答问题时手里有它 | ✅ 不在 |
| 浏览器插件说明 | ⚠️ 34 处 | ✅ 不在 |
| 推荐插件广告 | ⚠️ 在 | ✅ 不在 |
| 总长度 | 73.8K 字符 | 61.3K 字符 |

剩下的是 codex 的基础提示词和沙箱说明，**对所有用户都一样，不含个人内容**。API 路径本来就只发送问题本身。

### 怎么做到的

codex 的全局 `AGENTS.md` 放在 `CODEX_HOME` 里，**没有任何开关能单独关掉**（试过 `project_doc_max_bytes=0` 等，都只管项目目录里的 AGENTS.md）。所以：

1. 每次调用都新建一个**临时的干净 `CODEX_HOME`**
2. 只把登录凭据 `auth.json` **链接**过去，配置里只保留你的模型和推理强度
3. 调用结束即删除

为什么用链接而不是复制：codex 会定期刷新 token。如果写进副本，副本一删，新 token 就丢了，可能把你的主 codex 登出。实测 codex 会**顺着链接原地写回**原文件，所以链接是安全的。Windows 上没有权限建符号链接时，会自动退回硬链接。

### 自己核实

```bash
python3 scripts/ask_gpt.py --show-context "这段讨论的结论成立吗？"
```

```
GPT 实际会看到的上下文（共 61281 字符，未调用模型）：
  [tools    ]  22309 字 | functions, clock, collaboration
  [developer]  21544 字 | You are Codex, an agent based on GPT-6. …
  [developer]   3882 字 | <permissions instructions> Filesystem sandboxing …
  …
  [user     ]     11 字 | 这段讨论的结论成立吗？

你的 AGENTS.md：✅ 不在
技能清单：    ✅ 不在
结论：✅ 干净，适合盲评
```

只渲染上下文，**不调用模型、不耗额度**。每次调用的 `--json` 结果里也有 `clean_context` 字段。

- 想让 GPT 读到你的 AGENTS.md：设 `ASK_GPT_ISOLATE=0`
- 无法隔离时（比如凭据存在系统钥匙串里、或链接都建不了），会照常调用，但 `clean_context` 为 `false` 并附原因，**不会假装是干净的**

---

## 适用于哪些 agent

ask-gpt 本体是一个**零依赖的 Python 命令行脚本**，不绑定任何一家的 agent。宿主满足下面四条就能用：

| 要求 | 说明 |
|---|---|
| 能执行 shell 命令 | 只需要 `python3`（3.8+，标准库） |
| 能向用户提问 | 选择卡片、按钮或对话里直接问都行 |
| 沙箱放行网络 | 订阅路径访问 `chatgpt.com`；API 路径访问 `api.openai.com` |
| 沙箱能读到凭据 | 订阅路径读 `~/.codex/`；API 路径读环境变量 `OPENAI_API_KEY` |

- **支持 skill 的 agent**（Claude Code、WorkBuddy 等）：把整个目录放进它的 skills 目录，`SKILL.md` 会告诉它何时触发、怎么问你、怎么解析结果
- **不支持 skill 的 agent**：只要能跑 shell，直接调脚本；把 `SKILL.md` 的内容作为说明交给它即可

沙箱里的 home 目录或 PATH 和你的机器不一样时，用两个环境变量告诉它去哪找：

```bash
CODEX_HOME=/path/to/.codex  ASK_GPT_CODEX=/path/to/codex  python3 scripts/ask_gpt.py --probe
```

已在模拟沙箱（独立 HOME、PATH 只有 `/usr/bin:/bin`）里验证：设好后问答正常、署名为真实模型名；没设时 `--probe` 会直接提示该设哪个变量。

> **Kimi Code** 的 skill 格式和本仓库完全一致（`name` / `description` frontmatter + `scripts/`），可直接放进 `~/.kimi-code/skills/`；不想安装也能临时试用：`kimi --skills-dir <含 ask-gpt 的目录>`。格式已核对，尚未实际运行。

---

## 核心设计：探测在前，追问在后

每次都问"你有订阅还是 API"很烦；完全不问又会在费用上替你做决定。所以按探测结果分流，**只在真正需要决策时才开口**：

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 30, "rankSpacing": 40}}}%%
flowchart TD
    A(["⚡ 技能被触发"]) --> B("🔍 probe 探测<br/>只拉模型目录，不推理")
    B --> C{"已配置？"}
    C ==>|是| Z(["✅ 直接调用"])
    C -->|否| D{"可用路径？"}
    D -->|只有订阅| E("用订阅<br/>一句话告知")
    D -->|只有 API| F("用 API<br/>按 token 计费")
    D -->|两条都有| G("🙋 问你选哪条<br/>费用差异大")
    D -->|都没有| H("🙋 给安装指引<br/>问你走哪条")
    E & F & G --> J("💾 写入<br/>config.json")
    J --> Z
    H -.->|配置好后| B

    classDef entry  fill:#3949ab,stroke:#1a237e,color:#fff,stroke-width:2px
    classDef step   fill:#eceff1,stroke:#90a4ae,color:#263238
    classDef decide fill:#ede7f6,stroke:#7e57c2,color:#311b92,stroke-width:2px
    classDef sub    fill:#00796b,stroke:#004d40,color:#fff
    classDef api    fill:#ffb300,stroke:#ff8f00,color:#3e2723
    classDef user   fill:#c2185b,stroke:#880e4f,color:#fff,stroke-width:2px
    classDef done   fill:#2e7d32,stroke:#1b5e20,color:#fff,stroke-width:2px

    class A entry
    class B,J step
    class C,D decide
    class E sub
    class F api
    class G,H user
    class Z done
```

<sub>玫红色节点需要你参与，其余全自动 ｜ 青色 = 订阅路径，琥珀色 = API 路径</sub>

### 脚本绝不等 stdin

技能脚本跑在 agent 的工具调用里，**没有交互式终端**。脚本里只要有一处 `input()`，整个调用就会挂死。所以分工是：

- **脚本**：全程非交互，只负责探测（`--probe`）、落盘（`--set-path`）、执行
- **宿主 agent**：用自己的方式在对话里问你，拿到答案再调脚本落盘

---

## 架构：一套命令，两条路径

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 28, "rankSpacing": 36}}}%%
flowchart TB
    AG(["🤖 任意宿主 Agent"])

    subgraph CMD["统一命令面"]
        direction LR
        Q("💬 问答") ~~~ V("👁️ 读图<br/>--see") ~~~ P("🎨 生图<br/>--gen")
    end

    R{{"🔀 路由<br/>config.json"}}

    subgraph SUBP["订阅路径 · 零额外账单"]
        direction TB
        S1("codex exec<br/>只读沙箱") ~~~ S2("-i 多图输入") ~~~ S3("内置 image_gen")
    end

    subgraph APIP["API 路径 · 按 token 计费"]
        direction TB
        A1("POST<br/>/v1/responses") ~~~ A2("input_image<br/>base64") ~~~ A3("gpt-image-2")
    end

    M(["✨ GPT<br/>按账号拉取模型"])

    AG --> CMD --> R
    R -->|codex| SUBP
    R -->|api| APIP
    SUBP --> M
    APIP --> M

    classDef agent  fill:#3949ab,stroke:#1a237e,color:#fff,stroke-width:2px
    classDef cmd    fill:#eceff1,stroke:#90a4ae,color:#263238
    classDef router fill:#5e35b1,stroke:#311b92,color:#fff,stroke-width:2px
    classDef sub    fill:#00796b,stroke:#004d40,color:#fff
    classDef api    fill:#ffb300,stroke:#ff8f00,color:#3e2723
    classDef gpt    fill:#263238,stroke:#1de9b6,color:#fff,stroke-width:3px

    class AG agent
    class Q,V,P cmd
    class R router
    class S1,S2,S3 sub
    class A1,A2,A3 api
    class M gpt
    style CMD  fill:#90a4ae1f,stroke:#90a4ae,stroke-dasharray:4 3
    style SUBP fill:#00796b1f,stroke:#00796b,stroke-width:2px
    style APIP fill:#ffb3001f,stroke:#ff8f00,stroke-width:2px
```

| 能力 | 订阅路径 `codex` | API 路径 `api` |
|---|---|---|
| **计费** | ChatGPT 账号登录：包月额度，**零额外账单**<br/>⚠️ codex 用 API Key 登录：同样按 token 计费 | **按 token 计费** |
| **模型** | 按账号实时拉取（`codex debug models`） | 按账号实时拉取（`GET /v1/models`） |
| **问答** | `codex exec` 只读沙箱 | `POST /v1/responses` |
| **读图** | `codex exec -i`（客户端读文件后上传，可多张） | `input_image` base64 data URL |
| **生图** | 内置 `image_gen`，只读沙箱，从 codex 自己的目录取图 | `gpt-image-2` |
| **生图尺寸** | 不可指定 | `--size` 精确控制 |
| **额度耗尽** | `usage limit`，等订阅周期重置 | `429` 限速或余额不足 |

---

## 模型：按账号实时拉取，不写死

模型在不断更新。脚本**不维护任何模型白名单**，每次都从你的账号拉当前可用的目录：

```bash
python3 scripts/ask_gpt.py --list-models
```

```
路径 codex 当前可用模型 —— 来源：账号实时目录（codex debug models）
 ★ gpt-6.1-sol          Latest workhorse model for coding and everyday work.  [ctx 500000, 支持图像输入]
   gpt-6-astra          Frontier intelligence for the most demanding work.  [ctx 272000, 支持图像输入]
   gpt-6-sol            Previous generation workhorse model.  [ctx 272000, 支持图像输入]
   gpt-6-luna           Fast and affordable model for easier tasks.  [ctx 272000, 支持图像输入]
   gpt-5.6-sol          Older generation workhorse model.  [ctx 272000, 支持图像输入]
   …

★ = 当前默认（你的 Codex 配置）
```

<sub>以上是 2026-10-24 某个 ChatGPT 账号的快照，你看到的会随账号权益和 OpenAI 上线情况变化。</sub>

- **默认模型**：订阅路径直接用你 `~/.codex/config.toml` 里的 `model`，所以你在 Codex 里换了模型，这里自动跟着换
- **换模型**：`--set-model <模型名>` 持久，`-m <模型名>` 仅本次
- **描述来自 OpenAI 官方目录**，不是脚本自己编的

> **为什么坚持不写死？** 本项目上一版写死了三个模型并配了"最强 / 均衡 / 最便宜"的说明。不到两个月，OpenAI 官方目录里这三个的描述就全变成了 **"Older …"**，新的一代已经上线，而写死的清单把它们挡在了外面。

---

## 安装

### 1. 装技能

```bash
git clone https://github.com/Stefansong/ask-gpt.git

# Claude Code
cp -r ask-gpt ~/.claude/skills/ask-gpt

# Kimi Code
cp -r ask-gpt ~/.kimi-code/skills/ask-gpt

# 其他支持 skill 的 agent：放进它的 skills 目录（参照对应文档）
# 不支持 skill 的 agent：直接调用 ask-gpt/scripts/ask_gpt.py 即可
```

> ⚠️ 如果这是你账号里的**云端同步技能**，直接改本地文件**不会持久** —— 下次同步会被覆盖。要改版本请改云端。

### 2. 配认证（二选一，也可以都配）

<table>
<tr><th>订阅路径（推荐，不额外花钱）</th><th>API 路径（按量计费）</th></tr>
<tr valign="top"><td>

```bash
brew install --cask codex
# 或 npm install -g @openai/codex

codex login
# 无浏览器环境：
# codex login --device-auth
```

需 ChatGPT Plus / Pro / Business 等付费账号

</td><td>

```bash
echo 'export OPENAI_API_KEY=sk-...' >> ~/.zshrc
source ~/.zshrc
```

在 [platform.openai.com](https://platform.openai.com) 创建 Key

</td></tr>
</table>

### 3. 探测

```bash
python3 scripts/ask_gpt.py --probe
```

```
订阅(codex): 可用 — Logged in using ChatGPT
  使用: /opt/homebrew/bin/codex (0.164.0) | 默认模型: gpt-6.1-sol
  另有旧副本: ~/.nvm/versions/node/v20.19.5/bin/codex (0.151.0) —— 已自动跳过
  账号可用模型: gpt-6.1-sol, gpt-6-astra, gpt-6-sol, gpt-6-luna, …
API Key:     不可用 — OPENAI_API_KEY 未设置
```

之后平时直接说「**问问 GPT**」「**让 GPT 看看这张图**」，技能会自动触发。

---

## 使用

### 问答

```bash
python3 scripts/ask_gpt.py "2 的 10 次方是多少？"
python3 scripts/ask_gpt.py -f prompt.md          # 长内容走文件，避免 shell 转义
```

```
2 的 10 次方是 1024。
[路径: ChatGPT 订阅 via codex exec，模型 gpt-6.1-sol]
```

### 读图

```bash
python3 scripts/ask_gpt.py --see 图.png "这张图说明了什么？"
python3 scripts/ask_gpt.py --see 图1.png --see 图2.png -f q.md   # 多张重复 --see
```

<table>
<tr><td width="140"><img src="assets/demo-see-colors.png" width="120"></td><td>

```
$ ask_gpt.py --see demo-see-colors.png \
    "左右两半各是什么颜色？只答两个颜色词。"

红色 蓝色
[路径: ChatGPT 订阅 via codex exec，模型 gpt-6.1-sol，1 张图]
```

</td></tr>
</table>

### 生图

```bash
python3 scripts/ask_gpt.py --gen out.png "一只橘色的猫坐在窗台上，简洁扁平插画风格"
python3 scripts/ask_gpt.py --gen out.png --size 1536x1024 -f desc.txt   # --size 仅 API 路径
```

<table>
<tr><td width="320"><img src="assets/demo-gen-cat.png" width="300"></td><td>

```
saved: /abs/path/out.png
[生图路径: ChatGPT 订阅 via codex exec，
 image_gen（Codex 内置）]
```

**不会毁掉已有文件**：先校验是完整
图片（查文件头和文件尾），再原子
替换；失败时原文件保持不变。

这张图本身就是测试产物，随后又用
`--see` 反向识别为"橘白相间的猫坐在
窗台上" —— 生图与读图闭环验证通过。

</td></tr>
</table>

### 给 agent 用：`--json`

任何调用加 `--json` 都返回稳定的结构，比解析文本可靠：

```bash
python3 scripts/ask_gpt.py --json "Reply with exactly: OK"
```

```json
{
  "ok": true,
  "mode": "ask",
  "path": "codex",
  "billing": "subscription",
  "model": "gpt-6.1-sol",
  "answer": "OK",
  "images_in": []
}
```

| 字段 | 含义 |
|---|---|
| `ok` | 是否成功 |
| `mode` | `ask` / `see` / `gen` |
| `path` | `codex` 或 `api` |
| `billing` | `subscription`（包月额度）/ `per-token`（按 token 计费）/ `unknown`（无法确认）—— **判断费用看它，不看路径名** |
| `model` | **实际使用的模型名**，署名就用它 |
| `clean_context` | 这次 GPT 是否只看到了问题本身（没有你的 AGENTS.md / 技能 / 插件）；盲评前确认它为 `true` |
| `answer` / `image` | 文本回答 / 生成图片的绝对路径 |

**所有失败都是同一种结构**，包括参数错误。agent 按 `error_code` 分支即可，不必解析错误文本：

```json
{"ok": false, "error_code": "bad_input", "error": "图片不存在：/path/x.png"}
```

| `error_code` | 含义 | 退出码 |
|---|---|---|
| `call_failed` / `timeout` | 调用本身失败或超时 | `1` |
| `bad_args` / `bad_input` | 参数或输入有误 | `2` |
| `invalid_config` | 配置文件损坏或字段不合法 | `2` |
| `no_auth` / `path_unavailable` / `path_not_chosen` / `no_models` | 认证或路径问题 | `2` |

**成功判定是严格的**：codex 要求退出码 0、这一轮正常结束、回答非空；API 要求 `status=completed` 且有文本。被截断、拒答、半截输出都会报错，**不会当成 GPT 的回答返回**。

`--probe` 会请求模型目录（`codex debug models` / `GET /v1/models`），但**不做任何模型推理**，不消耗推理额度。

### 配置管理

```bash
python3 scripts/ask_gpt.py --set-path codex           # 选路径
python3 scripts/ask_gpt.py --set-model <模型名>        # 选模型
python3 scripts/ask_gpt.py --path api -m <模型名> "问题"   # 本次临时覆盖，不改默认
python3 scripts/ask_gpt.py --show-config
python3 scripts/ask_gpt.py --reset-config
```

---

## 必守规则

技能里写进了几条硬约束，目的是不让便利性盖过可靠性：

- 🏷️ **署名** —— 凡出自 GPT 的内容都标明「GPT（模型名，订阅/API 路径）」，与宿主 agent 自己的观点分开，绝不静默混用
- 💰 **费用** —— API 路径按 token 计费。批量调用、发大图或连续生图前，先说明大致用量
- 🔒 **隐私** —— 发出前确认不含患者可识别信息（PHI）、密钥、未发表数据；医学影像先去标识化
- ❌ **失败就是失败** —— 如实报错并给修复命令，**绝不编一个"GPT 的回答"顶替**，也不在你不知情时悄悄换模型重试

### ⚠️ 代理会改写流量

环境里设了 `HTTPS_PROXY` 或 `OPENAI_BASE_URL` 时，请求可能**经第三方网关转发而非直连 OpenAI**。`--probe --json` 会把这几个变量的状态一并返回：

```json
"proxy": {"HTTP_PROXY": true, "HTTPS_PROXY": true, "OPENAI_BASE_URL": false}
```

**"数据只发给 OpenAI"在有代理的机器上并不成立。** 开发本项目时就实测到请求被转发到了一个第三方 LLM 网关，而 CLI 自身对此毫不知情。

---

## 报错速查

<details>
<summary><b>订阅路径</b></summary>

| 报错 | 原因 / 处理 |
|---|---|
| `usage limit ... try again at HH:MM` | 包月额度用尽，等重置；若也有 API Key 可临时 `--path api` |
| `model_not_found` / `not supported when using Codex` | 多半是**客户端太旧、不认识新模型**。看 `--probe` 用的是哪份 codex；若都旧就升级 |
| `Model metadata for … not found` | 同上，旧客户端的内置目录里没有这个模型 |
| `未找到 codex 可执行文件` | 沙箱里没有 codex 或 PATH 不同，用 `ASK_GPT_CODEX` 指定 |
| 读图时 GPT 说"看不到图" | 检查图片路径；脚本已自动把相对路径转绝对路径 |

</details>

<details>
<summary><b>API 路径</b></summary>

| 报错 | 原因 / 处理 |
|---|---|
| `401` | Key 无效或过期 |
| `429` | 限速或余额不足 |
| `model_not_found` | 用 `--list-models` 看当前可用模型 |
| `连不上 OpenAI` | 检查沙箱网络与 `HTTPS_PROXY` |

</details>

---

## 实测状态

| 测试 | 订阅路径 | API 路径 |
|---|---|---|
| 问答（`gpt-6.1-sol`） | ✅ | ⚠️ 仅验证请求构造与错误处理 |
| 读图（含相对路径） | ✅ 左红右蓝 → "红色 蓝色" | ⚠️ 未经真机验证 |
| 生图 | ✅ PNG 落盘 | ⚠️ 未经真机验证 |
| 生图 ↔ 读图闭环 | ✅ 生成的鲸鱼被识别为"一只蓝色的鲸鱼" | — |
| 临时换模型（`-m`） | ✅ | — |
| 账号模型实时拉取 | ✅ | ⚠️ 未经真机验证 |
| 多份 codex 时选最高版本 | ✅ 跳过 npm 的 0.151.0，用 brew 的 0.164.0 | — |
| `--json` 成功 / 失败结构 | ✅ | ✅ |
| 模拟沙箱（独立 HOME + 最小 PATH + `CODEX_HOME`） | ✅ 问答成功，署名 `gpt-6.1-sol` | — |
| 两条都可用时拒绝自动选 | ✅ 退出码 2 | |
| codex 用 API Key 登录时识别为按 token 计费 | ✅ | — |
| 生图异常返回时原文件不被破坏（含写了一半的图） | ✅ 单元测试 | ✅ 单元测试 |
| 只读沙箱生图（不需要写工作目录） | ✅ macOS 实测，图片 md5 与 codex 目录一致 | — |
| 不把失败 / 截断 / 拒答当成功 | ✅ 单元测试 | ✅ 单元测试 |
| `--json` 下所有错误为统一结构 | ✅ 11 种错误场景 | ✅ |
| 配置文件损坏或字段非法时明确报错 | ✅ | ✅ |
| 相对路径的 `CODEX_HOME` / `ASK_GPT_CODEX` | ✅ 真实调用 | — |
| 盲评上下文隔离（不含 AGENTS.md / 技能 / 插件） | ✅ `prompt-input` 前后对比 + 真实调用中 codex 未执行任何命令 | ✅ 只发送问题 |
| 隔离的边界情况（硬链接回退、无法隔离时如实标记、用后清理） | ✅ 16 项单元测试 | — |

- **API 路径的真实响应未经真机验证** —— 开发环境没有 API Key
- **宿主 agent**：在 Claude Code 中实测通过；Kimi Code 已核对 skill 格式兼容，尚未实际运行；**WorkBuddy（Windows）**实测发现订阅生图因沙箱禁止写工作目录而失败，v4.2 已改为只读沙箱方案，修复后**尚未在 Windows 上复测**。如果你在别的 agent 里跑通了（或踩到坑），欢迎开 issue

---

## 踩过的坑

这些都是开发过程中真实遇到的，写在这里免得你再踩一遍。

<details>
<summary><b>1. 机器上有好几份 codex，PATH 里排第一的是旧的</b></summary>

```
~/.nvm/versions/node/v20.19.5/bin/codex   0.151.0   ← npm 装的，PATH 里排在前面
/opt/homebrew/bin/codex                   0.164.0   ← brew 装的，排在最后
```

`shutil.which("codex")` 永远拿到 npm 的旧版。旧客户端调新模型时，服务端返回 `model_not_found`，看起来像"订阅账号不支持这个模型" —— 其实只是客户端太旧。更隐蔽的是：**测试时只要顺手改了 PATH 就会成功**，于是很容易误判成间歇性的网络问题。

现在脚本会找出机器上所有 codex，**取版本最高的那份**，并在 `--probe` 里把其余副本列出来。

</details>

<details>
<summary><b>2. 写死的模型清单两个月就过期</b></summary>

上一版写死了三个模型。不到两个月，OpenAI 官方目录里这三个的描述就全变成了 "Older …"，新一代已经上线。更糟的是，那一版的"动态发现"其实是拿硬编码字典过滤缓存 —— 看着动态，新模型却永远进不来。

现在：订阅路径用 `codex debug models` 从服务端拉账号目录，只按 `visibility` 滤掉内部模型；API 路径查 `/v1/models`；默认模型跟随你的 Codex 配置。

</details>

<details>
<summary><b>3. 隔离运行目录导致相对路径失效</b></summary>

为了不让 GPT 碰到你的项目文件，订阅路径在**临时空目录**里运行 codex：

```python
cmd = [exe, "exec", "-", "--skip-git-repo-check", "--ephemeral",
       "-s", "read-only", "-C", workdir]
```

副作用是 `--see assets/x.png` 这种相对路径在那个目录里解析不到 —— codex 不报错，GPT 只会说"请重新上传图片"。现在所有图片路径都先转成绝对路径。

</details>

<details>
<summary><b>4. argparse 的 nargs="+" 吞掉了问题文本</b></summary>

```python
ap.add_argument("--see", nargs="+")          # ✗ 会把后面的问题当成第二张图
ap.add_argument("--see", action="append")    # ✓ 多图就重复 --see，和 codex 的 -i 一致
```

</details>

<details>
<summary><b>5. codex 能用 ≠ 走订阅额度</b></summary>

早期版本只要 `codex login status` 返回 0 就标成"订阅、零额外账单"。但 codex 也可以用 API Key 登录：

```
$ codex login status
API key configured - sk-…        ← 退出码同样是 0
```

这时实际按 token 计费。用户看到"零额外账单"放心批量调用，最后收到账单。

现在按登录方式判断：`Logged in using ChatGPT` → `subscription`，`API key` → `per-token`，认不出 → `unknown`（不假定免费）。

</details>

<details>
<summary><b>6. 先截断再解码：一次异常返回就毁掉原图</b></summary>

```python
open(out_path, "wb").write(base64.b64decode(resp["data"][0]["b64_json"]))
```

Python 先执行 `open(…, "wb")`，**文件立刻被清空**，然后才去解析返回数据。返回里没有 `b64_json`、`data` 为空、base64 非法 —— 任何一种都会让用户原来的图变成 0 字节。最糟的是返回空字符串时**不报错**，还会报告生图成功。

现在：先确认拿到的是完整图片（文件头 + 文件尾），再写同目录临时文件，`os.replace` 原子替换。

</details>

<details>
<summary><b>7. 把错误响应当成 GPT 的回答</b></summary>

```python
return _extract_text(resp) or json.dumps(resp)[:2000]
```

API 返回 `status: incomplete`（被截断）、`failed` 或拒答时，提取不到文本，于是**原始 JSON 被当成回答返回**，`ok: true`。宿主 agent 会把这串 JSON 当成"GPT 说的话"展示给用户。codex 路径也有类似问题：退出码非 0 但写了半截回答，照样算成功。

现在两条路径都严格判定，任何不完整的结果都报错。

</details>

<details>
<summary><b>8. 沙箱不许写工作目录，生成的图拿不回来</b></summary>

订阅路径原本让 codex 在 `workspace-write` 沙箱里把图写进工作目录再取回。WorkBuddy 在 Windows 上实测时，沙箱不允许写工作目录，这一步就断了。

其实 codex 自己会把生成的图存到 `$CODEX_HOME/generated_images/<会话 ID>/`，这一步不经过沙箱。现在生图改用**只读沙箱**，从 `--json` 事件流的 `thread.started` 拿会话 ID，直接去那里取图 —— 不再需要任何写权限。

</details>

<details>
<summary><b>9. GPT 的"独立作答"先读了你的个人指令</b></summary>

用户的全局 `~/.codex/AGENTS.md` 里写着"完成任务后执行 `say Mission Completed`"。结果每次用 ask-gpt 问 GPT 一个问题，电脑都会念一句"Mission Completed"。

这个现象本身无害，但它暴露了一件更要紧的事：**codex 把你的个人指令、技能清单，甚至桌面控制工具都交给了 GPT**。所谓"盲评"并不盲，个人指令里写的偏好、语气、规则都会影响 GPT 的"第二意见"。

现在每次调用都在临时的干净 `CODEX_HOME` 里运行（见「盲评上下文隔离」）。

</details>

---

## 致谢与声明

- 两条路径分别基于 [OpenAI Codex CLI](https://github.com/openai/codex) 与 [OpenAI Platform API](https://platform.openai.com)
- 本仓库的插图由本技能自身的生图功能产出（订阅路径，`image_gen`）
- 与 OpenAI、Anthropic 及任何 agent 厂商均无隶属关系；使用时请遵守各自服务条款
- 医学示意图若用于论文或汇报，请在图注注明"AI 生成，仅供参考"

## License

[MIT](LICENSE)

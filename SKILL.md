---
name: ask-gpt
description: '让 GPT 加入当前对话的工作流：在任何支持 skill 与 shell 的 AI agent（Claude Code、WorkBuddy 等）里调用用户自己的 OpenAI 账号。订阅路径用 ChatGPT 包月额度（经 Codex CLI，不额外计费），API 路径用 OPENAI_API_KEY（按 token 计费）；两条路径都支持问答、读图、生图，模型按账号实时拉取。用于让 GPT 回答问题、给第二意见、交叉验证结论、对照润色论文、识别图片、生成插图。用户说"问问GPT / 让GPT看看 / GPT怎么说 / 用OpenAI / 双模型对比 / 交叉验证 / second opinion / ask GPT / 让GPT看这张图 / 用GPT生成图片"或要求对比两个模型的答案时，都应使用本技能，即使用户没有点名任何技能。'
---

# ask-gpt：让 GPT 加入当前对话的工作流

一个零依赖的命令行脚本，任何能跑 shell 的 agent 都能调用。宿主 agent 负责对话与决策，
GPT 作为另一位参与者处理被交给它的那一步 —— 写好的段落交它审、拿不准的结论让它独立答、
图交给它看、插图让它画，结果回到同一个对话里继续往下走。

脚本：`SCRIPT="<本技能目录>/scripts/ask_gpt.py"`（用加载技能时给出的目录；路径含空格要加引号）

## 对宿主 agent 的要求

| 要求 | 说明 |
|---|---|
| 能执行 shell 命令 | 只需要 `python3`（3.8+，标准库） |
| 能向用户提问 | 选择卡片、按钮或直接在对话里问都行，用你所在 agent 的方式 |
| 沙箱放行网络 | 订阅路径需访问 `chatgpt.com`；API 路径需访问 `api.openai.com`（或 `OPENAI_BASE_URL`） |
| 沙箱能读到凭据 | 订阅路径需读 `~/.codex/`（登录态与模型目录）；API 路径需环境变量 `OPENAI_API_KEY` |

沙箱里的 home 目录或 PATH 跟用户机器不同时：用 `CODEX_HOME` 指向 Codex 数据目录，
用 `ASK_GPT_CODEX` 指定 codex 可执行文件。

**脚本全程非交互，绝不读 stdin 等用户输入** —— 所有选择由你在对话里问用户，
再用 `--set-path` / `--set-model` 落盘。不要写会等待输入的命令，工具调用里会直接卡死。

## 第 0 步：探测 + 按需询问（每个会话只做一次，记住结果）

```bash
python3 "$SCRIPT" --probe --json
```

`--probe` **会请求模型目录**（`codex debug models` / `GET /v1/models`），但**不做任何模型推理**，不消耗推理额度。

返回 `codex`（可用性、`auth_mode`、`billing`、`context_isolation`、所用客户端及版本、其他副本、默认模型、账号可用模型）、
`api`、`configured`、`config`、`config_error`、`proxy`。

**判断费用看 `codex.billing`，不要看路径名。** codex 用 ChatGPT 账号登录才是包月额度（`subscription`）；
用 API Key 登录（`codex login --with-api-key`）同样按 token 计费（`per-token`）；认不出登录方式时是 `unknown`，
不要假定免费。

| 探测结果 | 你该做什么 |
|---|---|
| `config_error` 非空 | 把原因告诉用户，用 `--set-path` 重新选或 `--reset-config` 清掉 |
| `configured: true` | **直接用，不要再问** |
| 只有 codex 可用 | 用 codex。`billing` 为 `subscription` 时一句话告知；`per-token` 时**提醒按 token 计费**；`unknown` 时如实说无法确认是否计费 |
| 只有 API 可用 | 用 API 路径，**提醒按 token 计费** |
| 两条都可用 | **必须问用户选哪条** —— 费用可能不同，不要替用户决定 |
| 两条都不可用 | 把「首次配置」两条都给用户，问他走哪条，配置好再继续 |

需要问路径时给两个选项：
- **订阅路径**：用 ChatGPT 包月额度，不产生额外账单（推荐）
- **API 路径**：按 token 计费，生图尺寸可控，不受订阅额度限制

```bash
python3 "$SCRIPT" --set-path codex
```

## 模型：按账号实时拉取，不写死

模型会不断更新，**不要照搬任何文档（包括本文件）里的模型名**，以运行时结果为准：

```bash
python3 "$SCRIPT" --list-models
```

- 订阅路径：`codex debug models` 从服务端拉当前 ChatGPT 账号可用的目录，只滤掉内部模型
- API 路径：`GET /v1/models`
- 默认模型：订阅路径用用户 `~/.codex/config.toml` 里的 `model`（列表中 ★ 标出），API 路径取配置或目录第一项

用户想换模型时把列表给他看，让他挑，再落盘：

```bash
python3 "$SCRIPT" --set-model <模型名>        # 持久
python3 "$SCRIPT" -m <模型名> "问题"           # 仅本次
```

## 用法

```bash
python3 "$SCRIPT" "你的问题"                                  # 问答
python3 "$SCRIPT" -f /tmp/askgpt/prompt.md                    # 长内容：先写文件，避免转义问题
python3 "$SCRIPT" --see 图1.png --see 图2.png "这两张图有什么差别？"   # 读图，多张重复 --see
python3 "$SCRIPT" --gen /tmp/askgpt/out.png "图像描述"         # 生图
python3 "$SCRIPT" --context brief.md --context draft.md "任务"  # 带上讨论背景与原始材料（见下节）
python3 "$SCRIPT" --dry-run --context brief.md "任务"           # 只看最终发给 GPT 的内容，不调用
python3 "$SCRIPT" --blind --context facts.md "问题"            # 盲评：要求上下文干净，不允许会话
python3 "$SCRIPT" --session draft1 --context brief.md "任务"    # 多轮会话：之后同名调用会续接
```

**加 `--json` 拿结构化结果**（推荐，比解析文本稳）：

```json
{"ok": true, "mode": "ask", "path": "codex", "billing": "subscription",
 "model": "<实际使用的模型>", "answer": "…", "images_in": [], "clean_context": true}
```

`billing` 取值：`subscription`（包月额度）/ `per-token`（按 token 计费）/ `unknown`（无法确认）。
生图返回 `"image": "<绝对路径>"`。

**所有失败都是同一种结构**（包括参数错误），按 `error_code` 处理，不要解析 `error` 文本：

```json
{"ok": false, "error_code": "call_failed", "error": "…", "path": "codex", "billing": "subscription"}
```

| `error_code` | 含义 | 退出码 |
|---|---|---|
| `call_failed` / `timeout` | 调用本身失败或超时 | 1 |
| `bad_args` / `bad_input` | 参数或输入有误（空 prompt、图片不存在、输出目录不存在等） | 2 |
| `invalid_config` | 配置文件损坏或字段不合法 | 2 |
| `no_auth` / `path_unavailable` / `path_not_chosen` / `no_models` | 认证或路径问题 | 2 |
| `not_clean` | `--blind` 但本次无法隔离上下文（已拒绝调用） | 2 |
| `session_unavailable` | 无法隔离，因此不开会话 | 2 |

**成功判定是严格的**：codex 要求退出码 0、这一轮正常结束、回答非空；API 要求 `status=completed` 且有文本。
被截断、拒答、半截输出都会报错，不会当成 GPT 的回答返回。

**生图不会毁掉已有文件**：脚本先校验拿到的是完整图片（查文件头和文件尾），再原子替换目标文件；
任何一步失败，目标路径上原有的文件保持不变。订阅路径用只读沙箱运行，从 codex 自己的
`generated_images` 目录取图，**不需要沙箱有写权限**（Windows 等禁止写工作目录的环境也适用）。
生成后把图展示给用户；医学示意图在图注注明"AI 生成，仅供参考"。

## 把讨论背景交给 GPT

**GPT 每次都从零开始**：看不到你和用户之前的对话，读不到用户的文件（调用在隔离的空目录里），
也不记得上一轮问过什么。它只知道你这一次发过去的内容。不给背景时它不会说"不知道"，
而是会**自信地回答另一件事**（实测：问一个只在讨论里出现过的研究代号，它答成了一项同名的无关试验）。

所以要让 GPT 了解讨论和进展，**由你写一份背景简报**，用 `--context` 附上；待审原文、代码、数据片段
也直接作为 `--context` 文件附上。问题本身单独写，说清楚要它做什么、输出什么格式。

```markdown
## 背景：我们在做什么（1–3 句）
## 已确定的事实与材料：数据、约束、关键参数、已读文献
## 进展：做过什么、结果如何（上一轮 GPT 的回答也写在这里，注明出自 GPT）
## 当前卡点 / 未决问题
```

写简报的原则：

- **按需摘要，不要照搬对话**：只放完成这次任务需要的信息；寒暄、走过的弯路、已推翻的方案不放（除非任务就是评估它们）
- **原文给原文**：待审段落、代码、数据、报错信息逐字附上，不要转述 —— 转述会混进你的理解
- **盲评时只给事实，不给判断**：保留背景、数据、约束；删掉你或用户已经形成的结论与倾向
  （"我们认为……""Claude 建议……"），否则 GPT 会被锚定
- **隐私**：简报里的病例数据先去标识化
- **告诉用户你带了什么**：发出前一句话说明附了哪些背景；内容多或敏感时，先用 `--dry-run` 让用户看完整内容
- **追问**：不开会话时，GPT 不记得上一轮。追问时把上一轮的问题和 GPT 的回答**原文**（不是摘要）
  写进简报的「进展」，再提新问题。需要反复多轮时，改用下面的会话

`--json` 结果里的 `prompt_chars` 与 `context_files` 记录了这次实际发了多少、带了哪些文件。

## 多轮会话（可选）

默认每次调用都从零开始。`--session 名字` 让 GPT 记得同一会话里的前几轮：同名第一次调用开新会话，
之后再用这个名字就接着往下聊。

| 什么时候用 | 什么时候**不要**用 |
|---|---|
| 和 GPT 反复打磨同一份东西（改稿、逐步完善论证） | 盲评、第二意见、交叉验证 —— 独立判断才有价值 |
| 追问它自己上一轮的推理（"你第 3 步的依据是什么"） | 单次就能问完的问题 |

- **盲评一律加 `--blind`**：脚本会拒绝和 `--session` 同用；本次若无法隔离上下文，直接报 `not_clean` 不调用
- 第一轮用 `--context` 带上完整背景；之后每轮只发新的指令和新材料，不必重发简报
- 会话里路径和模型固定；要换就换个会话名
- `--list-sessions` 查看，`--end-session 名字` 结束并删除记录。**用完就结束**
- 存储：codex 路径存在本地 `~/.config/ask-gpt/sessions/`（仅本人可读，不进用户自己的 codex 历史）；
  API 路径存在 OpenAI 服务端，结束会话时一并删除。不开会话的 API 调用不在服务端保存
- 生图不支持会话：要改的地方写进描述，重新生成即可
- 结果 JSON 里的 `session` 字段给出会话名、第几轮、是否新开

## 盲评上下文隔离

codex 默认会把用户的**全局 AGENTS.md、已装技能、插件、MCP、hooks、桌面控制工具**带进模型上下文 ——
GPT 的"独立作答"其实先读了用户的个人指令。脚本默认隔离这些：每次调用都换一个临时的干净
`CODEX_HOME`，只把登录凭据 `auth.json` 链接过去，配置里只保留用户的模型与推理强度，调用结束即删。

- 结果里的 **`clean_context`** 告诉你这一次是否干净：`true` = 已隔离；`false` 时附 `clean_context_reason`
- **做盲评 / 交叉验证前确认 `clean_context` 为 `true`**；为 `false` 时先告诉用户原因，不要把结果当成干净盲评呈现
- 想核实 GPT 到底看到了什么：`python3 "$SCRIPT" --show-context "问题"`（只渲染上下文，不调用模型、不耗额度）
- 用户明确希望 GPT 读到 AGENTS.md 时：设环境变量 `ASK_GPT_ISOLATE=0`
- 剩下的 codex 基础提示词对所有用户都一样，不含个人内容；API 路径本来就只发送问题本身

## 让 GPT 参与工作流的几种方式

1. **直接提问**：用户的问题原样转发，不加不减，回答标明出自 GPT。
2. **第二意见 / 交叉验证（盲评）**：只发原始问题让 GPT 独立作答，**不要附上你已有的结论**，
   避免锚定；调用时加 `--blind`（脚本保证上下文干净、不进会话）。拿到回答后由你对比，向用户汇报：一致点、分歧点、综合判断（分歧处给依据或文献线索）。
3. **审稿 / 对照润色**：背景简报和待审原文用 `--context` 附上，问题写「请指出错误、遗漏与可改进处，并给出修改版」。
   把 GPT 修改版与原文逐段对照（表格或 diff），交用户定稿 —— **不要擅自把两版合并**。
4. **识图**：图和问题一起发；复杂影像先问清用户想让 GPT 看什么。
5. **流水线中的一步**：你起草 → GPT 审 → 你按意见修改 → 用户定稿；或 GPT 出图 → 你嵌入文档。
   每次交给 GPT 前都更新简报的「进展」，它才知道流水线走到了哪一步。
   每一步的产出都在同一个对话里，用户随时能看到是谁做了什么。

## 报错速查

| 报错 | 处理 |
|---|---|
| `usage limit … try again at HH:MM` | 订阅额度用尽，告诉用户重置时间；若也有 API Key，问是否临时 `--path api` |
| 订阅生图报"没有产出有效图片" | 当前账号或平台没有 `image_gen` 工具（Windows/WSL 已知问题）；如实告诉用户，问是否改走 API 路径 |
| `model_not_found` / `not supported when using Codex` | 多半是**客户端太旧、不认识新模型**。看 `--probe` 里用的哪份 codex 及版本；脚本会自动选机器上版本最高的那份，若都旧就请用户升级（`brew upgrade --cask codex` 或 `npm i -g @openai/codex@latest`） |
| `未找到 codex 可执行文件` | 沙箱里没有 codex，或 PATH 不同 —— 用 `ASK_GPT_CODEX` 指定 |
| 读图时 GPT 说"看不到图" | 确认图片路径存在；脚本已把相对路径转绝对路径 |
| API `401` / `429` | Key 无效或过期 / 限速或余额不足 |
| `连不上 OpenAI` | 检查沙箱网络与 `HTTPS_PROXY` |

把原始报错如实转告用户，不要猜原因。

## 必守规则

- **署名**：凡出自 GPT 的内容都标明「GPT（模型名，订阅/API 路径）」—— 模型名取 `--json` 返回的 `model`，
  与你自己的观点分开呈现，绝不静默混用。
- **费用**：以 `billing` 字段为准 —— `per-token` 或 `unknown` 时，批量/循环调用、发大图或连续生图前，先说明大致用量。
- **隐私**：发出前确认不含患者可识别信息（PHI）、密钥、未发表的敏感数据；医学影像和数据先去标识化，
  拿不准就先问用户。**`--probe` 显示 `HTTPS_PROXY` 或 `OPENAI_BASE_URL` 已设置时，请求可能经第三方网关转发，
  涉及敏感数据必须先告知用户。**
- **失败就是失败**：任何一步失败都如实告知并给出修复方法，绝不能编一个"GPT 的回答"顶替；
  也不要在用户不知情时悄悄换成别的模型重试。
- 会话内复用第 0 步的探测结果，不要每次调用都重测。

## 首次配置（告诉用户执行，二选一）

**订阅路径**（推荐，需 ChatGPT Plus / Pro / Business 等付费账号）：

```bash
brew install --cask codex      # 或 npm install -g @openai/codex
codex login                    # 无浏览器环境：codex login --device-auth
```

**API 路径**：

```bash
echo 'export OPENAI_API_KEY=sk-...' >> ~/.zshrc && source ~/.zshrc
```

## 验证

```bash
python3 "$SCRIPT" --json "Reply with exactly: OK"
python3 "$SCRIPT" --see <任意图片> "一句话描述这张图"
python3 "$SCRIPT" --gen /tmp/askgpt/test.png "一只橘猫坐在窗台上，扁平插画风" && ls -la /tmp/askgpt/test.png
```

告诉用户当前走哪条路径、用的哪个模型（取自 `--json` 的 `model` 字段）。

---
name: ask-gpt
description: '在 Claude / Claude Code 中调用用户自己的 OpenAI 账号：订阅路径（ChatGPT 包月额度，经 Codex CLI，不额外计费）或 API 路径（OPENAI_API_KEY，按 token 计费），两条路径都支持问答、读图、生图。用于让 GPT-5.6 回答问题、提供第二意见、交叉验证 Claude 的结论、对照润色论文段落、识别图片影像、生成插图。只要用户说"问问GPT / 让GPT看看 / GPT怎么说 / 用OpenAI / 双模型对比 / 交叉验证 / second opinion / ask GPT / gpt-5 / 让GPT看这张图 / 用GPT生成图片"，或要求对比 Claude 与 GPT 的答案时，都应使用本技能，即使用户没有点名任何技能。'
---

# ask-gpt v4：双路径调 GPT（问答 / 读图 / 生图）

两条路径，功能对等，命令完全相同 —— 路径差异由脚本内部消化：

| 能力 | 订阅路径 `codex` | API 路径 `api` |
|---|---|---|
| 计费 | ChatGPT 包月额度，**不产生额外账单** | **按 token 计费** |
| 文本模型 | `gpt-5.6-sol` / `terra` / `luna` | 同样三款 |
| 问答 | `codex exec` 只读沙箱 | `POST /v1/responses` |
| 读图 | `codex exec -i`（客户端读文件后上传，可多张） | `input_image` base64 data URL |
| 生图 | 内置 `image_gen` 工具 | `gpt-image-2` → `/v1/images/generations` |
| 生图尺寸 | 不可指定（由工具决定） | `--size` 精确控制 |
| 额度耗尽表现 | `usage limit`，需等订阅周期重置 | `429` 限速或余额不足 |
| 已知缺陷 | `image_gen` 偶尔只给预览不落盘；Windows/WSL 可能拿不到此工具 | — |

脚本：`SCRIPT="<本技能目录>/scripts/ask_gpt.py"`（用加载时给出的技能目录，路径含空格要加引号）

**脚本全程非交互，绝不读 stdin 等用户输入** —— 所有选择由你用 AskUserQuestion 在对话里问，
再用 `--set-path` / `--set-model` 落盘。不要写会等待输入的命令，工具调用里会直接卡死。

## 第 0 步：探测 + 按需询问（每会话只做一次，记住结果）

```bash
python3 "$SCRIPT" --probe --json
```

返回 `codex.available` / `api.available` / `configured` / `config` / `text_models` / `proxy`。
按五种分支处理：

| 探测结果 | 你该做什么 |
|---|---|
| `configured: true` | **直接用，不要再问**。配置已存在 `~/.config/ask-gpt/config.json` |
| 只有 codex 可用 | 用订阅路径，一句话告知用户即可，不必问 |
| 只有 api 可用 | 用 API 路径，**提醒按 token 计费** |
| 两条都可用 | **必须问用户选哪条** —— 费用差异大，不要替用户决定 |
| 两条都不可用 | 把下面「首次配置」两条都给用户，问他走哪条，等配置好再继续 |

需要问路径时，用 AskUserQuestion 给两个选项：
- **订阅路径**：用 ChatGPT 包月额度，不产生额外账单（推荐）
- **API 路径**：按 token 计费，尺寸可控、不受订阅额度限制

路径定了再问模型（三款都两条路径通用）：

| 模型 | 适用 |
|---|---|
| `gpt-5.6-sol` | 最强，推理 / 长文 / 疑难问题（默认） |
| `gpt-5.6-terra` | 均衡，日常问答与润色，速度快 |
| `gpt-5.6-luna` | 最便宜最快，简单任务、批量处理 |

落盘（一次搞定，之后不再追问）：

```bash
python3 "$SCRIPT" --set-path codex -M gpt-5.6-sol
```

## 用法 1：问答 / 第二意见

```bash
python3 "$SCRIPT" "你的问题"                    # 短问题
python3 "$SCRIPT" -f /tmp/askgpt/prompt.md      # 长内容：先写文件，避免 shell 转义问题
```

回答走 stdout，路径与模型标注走 stderr（`[路径: ... 模型 ...]`）。

## 用法 2：读图（把图片发给 GPT 看）

```bash
python3 "$SCRIPT" --see 图1.png "这张图说明了什么？"
python3 "$SCRIPT" --see 图1.png --see 图2.png -f /tmp/askgpt/prompt.md   # 多张就重复 --see
```

支持 png / jpg / webp / gif。**多图必须重复 `--see`**，不要写成 `--see a.png b.png`。
复杂影像先问清用户想让 GPT 看什么，再组织问题。

## 用法 3：生图

```bash
python3 "$SCRIPT" --gen /tmp/askgpt/out.png "图像描述"
python3 "$SCRIPT" --gen out.png --size 1536x1024 -f desc.txt   # --size 仅 API 路径生效
```

**生完必须核对文件真的存在**（`ls -la`）—— 订阅路径的 `image_gen` 有偶尔只给预览不落盘的已知问题；
文件不在就如实说明，并询问用户是否改走 API 路径。生成后把图片展示/发送给用户。
医学示意图在图注注明"AI 生成，仅供参考"。

## 临时覆盖

```bash
python3 "$SCRIPT" --path api -m gpt-5.6-luna "问题"   # 本次换路径/模型，不改默认配置
python3 "$SCRIPT" --show-config                       # 看当前配置
python3 "$SCRIPT" --reset-config                      # 重新选路径时先清掉
```

## 报错速查

**订阅路径**
- `usage limit ... try again at HH:MM` → 包月额度用尽，告诉用户重置时间；若 API Key 也在，问是否临时改走 API
- `未找到 codex 可执行文件` → 脚本已自动找过 PATH、`/opt/homebrew/bin`、`/usr/local/bin`、`~/.local/bin`、ChatGPT.app 内置路径；都没有就是真没装
- `codex exec 未返回回答` → 看脚本打印的末尾输出，别猜原因
- 403 `team not allowed to access model` → 该账号/网关不允许这个模型，换 `terra` / `luna` 试

**API 路径**
- `401` → Key 无效或过期；`429` → 限速或余额不足
- `model_not_found` → 模型名已更新，查 `GET /v1/models` 后用 `-m` 指定
- `连不上 OpenAI` → 检查 `HTTPS_PROXY` 是否可用

## 使用模式（怎么组织 prompt）

1. **直接提问**：用户的问题原样转发，不加不减；回答时明确标注这是 GPT 的回答。
2. **第二意见 / 交叉验证（盲评，推荐用于事实核查）**：只发原始问题让 GPT 独立作答，
   **不要附上 Claude 已有的结论**，避免锚定效应。拿到回答后由你对比，向用户汇报三部分：
   一致点、分歧点、你的综合判断（分歧处必须给出依据或文献线索，不能只说"我觉得"）。
3. **审稿 / 对照润色（论文段落、草稿）**：prompt = 背景 + 待审文本 +
   「请指出错误、遗漏与可改进处，并给出修改版」。把 GPT 修改版与原文逐段做成对照
   （表格或 diff 形式），交由用户定稿 —— **不要擅自把两版合并成一版**。
4. **识图**：图片和问题一起发（用法 2）。

## 必守规则

- **署名**：凡出自 GPT 的内容都标明「GPT（模型名，订阅/API 路径）」，与你自己的观点分开呈现，绝不静默混用。
- **费用**：API 路径按 token 计费。单次问答直接跑；批量/循环调用、发大图或连续生图前，先向用户说明大致用量再动手。
- **隐私**：发出前确认内容不含患者可识别信息（PHI）、密钥、未发表的敏感数据；医学影像和数据先去标识化，拿不准就先问用户。
  **若 `--probe` 显示 `HTTPS_PROXY` 或 `OPENAI_BASE_URL` 已设置，请求会经第三方网关转发而非直连 OpenAI —— 涉及敏感数据时必须先告知用户。**
- **失败就是失败**：任何一步失败都如实告知并给出修复命令，绝不能自己编一个"GPT 的回答"顶替。
- 会话内复用第 0 步的探测结果，不要每次调用都重测。

## 首次配置（告诉用户执行，二选一）

**订阅路径**（推荐，用 ChatGPT 包月额度，需 Plus/Pro/Business 等付费账号）：

```bash
brew install codex          # 或 npm install -g @openai/codex
codex login                 # 浏览器登录；无浏览器环境用 codex login --device-auth
```

**API 路径**（按 token 计费，尺寸可控、不受订阅额度限制）：

```bash
echo 'export OPENAI_API_KEY=sk-...' >> ~/.zshrc && source ~/.zshrc
```

## 验证

配好后自测三项，并告诉用户当前走哪条路径、用什么模型：

```bash
python3 "$SCRIPT" "Reply with exactly: OK"
python3 "$SCRIPT" --see <任意图片> "一句话描述这张图"
python3 "$SCRIPT" --gen /tmp/askgpt/test.png "一只橘猫坐在窗台上，扁平插画风" && ls -la /tmp/askgpt/test.png
```

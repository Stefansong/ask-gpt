#!/usr/bin/env python3
"""ask_gpt.py v4 — 在 Claude / Claude Code 里调用你的 OpenAI 订阅或 API Key。

两条路径，功能对等（问答 / 读图 / 生图）：
  订阅路径 codex : ChatGPT 包月额度，经官方 Codex CLI，不产生 API 账单
  API 路径  api  : OPENAI_API_KEY 直连 OpenAI，按 token 计费

路径与模型选定后写入 ~/.config/ask-gpt/config.json，之后不再追问。

本脚本全程非交互（绝不读 stdin 做问答）——选择由 Claude 在对话里问用户，
再用 --set-path / --set-model 落盘。这样在工具调用里不会卡住。

常用：
  python3 ask_gpt.py --probe --json              # 探测路径、登录方式与模型目录（会拉取模型目录，不做推理）
  python3 ask_gpt.py --set-path codex -M gpt-5.6-sol
  python3 ask_gpt.py "你的问题"                   # 问答
  python3 ask_gpt.py -f prompt.md                # 长 prompt 从文件读
  python3 ask_gpt.py --see 图.png "这是什么？"     # 读图；多张就重复 --see
  python3 ask_gpt.py --gen out.png -f desc.txt   # 生图
  python3 ask_gpt.py --path api "问题"            # 本次强制某路径

退出码：0 成功；1 调用失败（error_code: call_failed / timeout）；
        2 环境、配置或输入问题（bad_args / bad_input / invalid_config / no_auth /
          path_not_chosen / path_unavailable / no_models）。
加 --json 时，成功和失败都输出 JSON；失败统一为 {ok: false, error_code, error, ...}。
"""
import argparse
import base64
import contextlib
import json
import mimetypes
import re
import shutil
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/ask-gpt/config.json")
API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# 不维护模型白名单 —— 模型会不断更新，任何硬编码的清单都会过期。
# 文本模型一律在运行时发现：订阅路径读 Codex 本地缓存，API 路径查 /v1/models。
# 下面两个只是「发现失败时」的最后兜底，可被 config.json 或环境变量覆盖。
FALLBACK_API_MODEL = "gpt-5.6-sol"
FALLBACK_IMAGE_MODEL = "gpt-image-2"


def codex_home():
    """Codex 数据目录（登录态、config.toml、模型缓存）。沙箱里 home 不同时用 CODEX_HOME 指定，
    和 codex 自己的约定一致。"""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")

CODEX_CANDIDATES = [
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    os.path.expanduser("~/.local/bin/codex"),
    "/Applications/ChatGPT.app/Contents/Resources/codex",
]

SETUP_HINT = """两条路径，二选一：
  [订阅] brew install codex  （或 npm i -g @openai/codex）然后 codex login
         用 ChatGPT 包月额度，不产生额外账单。无浏览器环境用 codex login --device-auth
  [API ] 在 platform.openai.com 创建 Key，然后
         echo 'export OPENAI_API_KEY=sk-...' >> ~/.zshrc && source ~/.zshrc
         按 token 计费。"""


# ---------- 配置 ----------

class Fail(Exception):
    """可预期的失败，带稳定的 error_code；--json 时原样交给 agent 解析。
    status：2 = 环境 / 配置 / 输入问题，1 = 调用本身失败。"""
    def __init__(self, code, message, status=2, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


CONFIG_KEYS = ("path", "model", "image_model")


def _config_problems(cfg):
    if not isinstance(cfg, dict):
        return ["顶层必须是 JSON 对象"]
    probs = []
    if "path" in cfg and cfg["path"] not in ("codex", "api"):
        probs.append('path 只能是 "codex" 或 "api"，现在是 %r' % (cfg["path"],))
    for k in ("model", "image_model"):
        if k in cfg and not (isinstance(cfg[k], str) and cfg[k].strip()):
            probs.append("%s 必须是非空字符串" % k)
    unknown = sorted(set(cfg) - set(CONFIG_KEYS))
    if unknown:
        probs.append("未知字段 %s" % ", ".join(unknown))
    return probs


def load_config():
    """配置文件不存在 → 空配置；存在但损坏或字段不合法 → 明确报错，不静默当成空配置。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        raise Fail("invalid_config", "配置文件 %s 无法解析（%s）。运行 --reset-config 后重新选择路径"
                   % (CONFIG_PATH, e))
    probs = _config_problems(cfg)
    if probs:
        raise Fail("invalid_config", "配置文件 %s 有误：%s。用 --set-path / --set-model 改正，"
                   "或 --reset-config 重来" % (CONFIG_PATH, "；".join(probs)))
    return cfg


def save_config(cfg):
    """写同目录临时文件再原子替换，中途出错不会留下半截配置。"""
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------- 探测 ----------

_CODEX_CACHE = {}


def codex_installs():
    """列出机器上所有 codex：PATH 里每一份 + 常见安装位置，按真实路径去重。
    一台机器上常同时有 npm / brew / app 自带的好几份，版本可能差很多。"""
    if "installs" in _CODEX_CACHE:
        return _CODEX_CACHE["installs"]
    cands = [os.path.join(d, "codex") for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    cands += CODEX_CANDIDATES
    seen, out = set(), []
    for c in cands:
        if not (os.path.isfile(c) and os.access(c, os.X_OK)):
            continue
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        out.append({"path": c, "version": codex_version(c)})
    _CODEX_CACHE["installs"] = out
    return out


def _version_key(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", v or "")
    if not m:
        return (-1, -1, -1, -1)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1)


def find_codex():
    """新模型要求新客户端 —— 旧客户端会被服务端以 model_not_found 拒绝。
    所以取版本最高的那份，而不是 PATH 里排第一的。ASK_GPT_CODEX 可强制指定。"""
    forced = os.environ.get("ASK_GPT_CODEX")
    if forced:
        return forced if os.access(forced, os.X_OK) else None
    installs = codex_installs()
    if not installs:
        return None
    return max(installs, key=lambda i: _version_key(i["version"]))["path"]


CODEX_BILLING = {"chatgpt": "subscription", "apikey": "per-token", "unknown": "unknown"}


def codex_auth_mode(status_text):
    """codex 能用不等于走订阅：用 ChatGPT 账号登录才消耗包月额度，
    用 API Key 登录（codex login --with-api-key）同样按 token 计费。
    以 `codex login status` 的输出为准，auth.json 的 auth_mode 兜底；
    都认不出就标 unknown —— 宁可说不确定，也不默认当成免费。"""
    t = (status_text or "").lower()
    if "api key" in t:            # 先判 API Key：两者都沾边时按更贵的算
        return "apikey"
    if "chatgpt" in t:
        return "chatgpt"
    try:
        with open(os.path.join(codex_home(), "auth.json"), encoding="utf-8") as f:
            mode = (json.load(f).get("auth_mode") or "").lower()
        if mode in ("chatgpt", "apikey"):
            return mode
    except Exception:
        pass
    return "unknown"


def codex_state():
    ch = os.environ.get("CODEX_HOME")
    if ch and not os.path.isdir(ch):
        return {"available": False, "exe": find_codex(), "auth_mode": None, "billing": None,
                "detail": "CODEX_HOME=%s 不存在" % ch}
    exe = find_codex()
    if not exe:
        forced = os.environ.get("ASK_GPT_CODEX")
        if forced:
            return {"available": False, "exe": None,
                    "detail": "ASK_GPT_CODEX=%s 不存在或不可执行" % forced}
        return {"available": False, "exe": None, "detail": "未找到 codex 可执行文件"}
    try:
        r = subprocess.run([exe, "login", "status"],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"available": False, "exe": exe, "detail": "codex login status 执行失败: %s" % e}
    detail = (r.stdout or r.stderr or "").strip().splitlines()
    mode = codex_auth_mode("\n".join(detail)) if r.returncode == 0 else None
    return {"available": r.returncode == 0, "exe": exe,
            "auth_mode": mode,
            "billing": CODEX_BILLING[mode] if mode else None,
            "version": codex_version(exe),
            "default_model": codex_default_model(),
            "detail": (detail[0] if detail else "退出码 %s" % r.returncode) + (
                "" if r.returncode == 0 or os.environ.get("CODEX_HOME") else
                "（若在沙箱/容器里运行，可能读不到 ~/.codex，用 CODEX_HOME 指定）")}


def _parse_catalog(data):
    """把 Codex 模型目录转成统一格式。只按 visibility 过滤内部模型，不做名称白名单。"""
    items = data.get("models", []) if isinstance(data, dict) else (data or [])
    out = []
    for m in items:
        if m.get("visibility") != "list":      # hide = 内部/保留模型
            continue
        mods = m.get("input_modalities") or []
        out.append({
            "id": m.get("slug"),
            "label": m.get("display_name") or m.get("slug"),
            "desc": (m.get("description") or "").strip(),
            "vision": ("image" in mods) if mods else None,
            "context": m.get("context_window"),
            "_prio": m.get("priority", 10 ** 6),
        })
    out.sort(key=lambda x: x["_prio"])         # priority 越小越靠前
    return [{k: v for k, v in m.items() if k != "_prio"} for m in out]


def codex_models(live=True, exe=None):
    """订阅路径的模型 = 当前 ChatGPT 账号可用的模型目录。
    live=True：`codex debug models` 从服务端实时拉取（官方接口，随账号权益与新模型上线变化）。
    拉取失败或 live=False：退回 Codex 自己写下的本地缓存。"""
    if live:
        exe = exe or find_codex()
        if exe:
            try:
                r = subprocess.run([exe, "debug", "models"],
                                   capture_output=True, text=True, timeout=90)
                if r.returncode == 0 and r.stdout.strip():
                    return _parse_catalog(json.loads(r.stdout))
            except Exception:
                pass
    try:
        with open(os.path.join(codex_home(), "models_cache.json"), encoding="utf-8") as f:
            return _parse_catalog(json.load(f))
    except Exception:
        return []


def codex_version(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=15).stdout
        m = re.search(r"(\d+\.\d+\.\d+[\w.-]*)", out)
        return m.group(1) if m else out.strip()
    except Exception:
        return None


def _codex_config_value(key):
    """读用户 <CODEX_HOME>/config.toml 顶层的 key = "value"。只读，不修改。"""
    try:
        with open(os.path.join(codex_home(), "config.toml"), encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("["):  # 进入子表就停，只看顶层
                    break
                m = re.match(r'\s*%s\s*=\s*"([^"]+)"' % re.escape(key), line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def codex_default_model():
    return _codex_config_value("model")


# ---------- 盲评上下文隔离 ----------
#
# codex 会把用户的全局 AGENTS.md（就在 CODEX_HOME 里）、已装的技能、插件、MCP、hooks
# 都带进模型上下文，没有开关能单独关掉全局 AGENTS.md。所以每次调用都换一个临时的干净
# CODEX_HOME，只把登录凭据 auth.json 链接过去，配置里只保留用户的模型与推理强度。

ISOLATED_FEATURES = """
[features]
recommended_plugins = false
plugins = false
apps = false
browser_use = false
computer_use = false
hooks = false

[skills]
include_instructions = false
"""

LAST_CONTEXT = {"clean": None, "reason": None}


def isolation_status():
    if os.environ.get("ASK_GPT_ISOLATE", "1") == "0":
        return False, "已用 ASK_GPT_ISOLATE=0 关闭隔离"
    auth = os.path.join(codex_home(), "auth.json")
    if not os.path.isfile(auth):
        return False, "登录凭据不在 %s（可能存于系统钥匙串），无法隔离" % auth
    return True, None


@contextlib.contextmanager
def clean_codex_home():
    """产出一个临时的干净 CODEX_HOME（或 None = 本次未隔离），用完即删。
    auth.json 用链接指向真实文件：codex 刷新 token 时会顺着链接原地写回（已验证），
    不会写进副本、随临时目录一起丢掉，从而把用户的主登录弄失效。"""
    enabled, reason = isolation_status()
    if not enabled:
        LAST_CONTEXT.update(clean=False, reason=reason)
        yield None
        return
    iso = tempfile.mkdtemp(prefix="askgpt_home_")
    try:
        real_auth, link = os.path.join(codex_home(), "auth.json"), os.path.join(iso, "auth.json")
        try:
            os.symlink(real_auth, link)
        except OSError:
            try:
                os.link(real_auth, link)          # Windows 无权限建符号链接时退回硬链接
            except OSError:
                LAST_CONTEXT.update(clean=False, reason="无法链接登录凭据（符号链接与硬链接都不可用），本次未隔离")
                yield None
                return
        top = ""
        for key in ("model", "model_reasoning_effort"):   # 只沿用「用哪个模型、想多深」
            v = _codex_config_value(key)
            if v:
                top += "%s = %s\n" % (key, json.dumps(v))
        with open(os.path.join(iso, "config.toml"), "w", encoding="utf-8") as f:
            f.write("# ask-gpt 生成的隔离配置，调用结束即删除\n" + top + ISOLATED_FEATURES)
        LAST_CONTEXT.update(clean=True, reason=None)
        yield iso
    finally:
        shutil.rmtree(iso, ignore_errors=True)    # 只删链接本身，不碰真实的 auth.json


def api_models(timeout=30):
    """查 /v1/models —— 权威且总是最新。不做过滤，由用户自己挑。"""
    if not os.environ.get("OPENAI_API_KEY"):
        return []
    req = urllib.request.Request(
        API_BASE + "/models",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
    try:
        data = json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception:
        return []
    out = [{"id": m.get("id"), "label": m.get("id"), "desc": "",
            "vision": None, "context": None}
           for m in data.get("data", []) if m.get("id")]
    out.sort(key=lambda x: x["id"])
    return out


def models_for(path):
    return codex_models() if path == "codex" else api_models()


def resolve_model(path, args, cfg):
    """显式指定 > 配置 > 环境变量 > （订阅路径）交给 Codex 自己的默认。"""
    m = args.model or cfg.get("model") or os.environ.get("OPENAI_MODEL")
    if m:
        return m
    if path == "codex":
        return None          # 不传 -m，用用户 Codex 配置里的默认模型，天然跟随更新
    disc = api_models()
    return disc[0]["id"] if disc else FALLBACK_API_MODEL


def api_state():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"available": False, "detail": "OPENAI_API_KEY 未设置"}
    return {"available": True, "detail": "OPENAI_API_KEY 已设置（%s…%s）"
            % (key[:6], key[-4:]) if len(key) > 12 else "OPENAI_API_KEY 已设置"}


def probe():
    """探测会请求模型目录（codex debug models / GET /v1/models），但不做任何模型推理。
    配置文件损坏时照样完成探测，把问题放进 config_error。"""
    try:
        cfg, cfg_err = load_config(), None
    except Fail as e:
        cfg, cfg_err = {}, e.message
    cx, ap = codex_state(), api_state()
    iso_on, iso_reason = isolation_status()
    cx["context_isolation"] = {"enabled": iso_on, "reason": iso_reason}
    cx["installs"] = codex_installs()
    cx["models"] = codex_models() if cx["available"] else []
    ap["models"] = api_models() if ap["available"] else []
    return {
        "codex": cx,
        "api": ap,
        "config": cfg,
        "config_path": CONFIG_PATH,
        "config_error": cfg_err,
        "configured": bool(cfg.get("path")),
        "both_available": cx["available"] and ap["available"],
        "proxy": {k: bool(os.environ.get(k))
                  for k in ("HTTP_PROXY", "HTTPS_PROXY", "OPENAI_BASE_URL")},
    }


# ---------- 订阅路径 ----------

def _codex_base(exe, workdir, sandbox, model, images):
    cmd = [exe, "exec", "-", "--skip-git-repo-check", "--ephemeral",
           "-s", sandbox, "-C", workdir]
    if model:
        cmd += ["-m", model]
    for img in images or []:
        cmd += ["-i", img]
    return cmd


def _codex_events(stdout):
    """解析 codex exec --json 的事件流（每行一个 JSON）。"""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def _codex_failure(events):
    """事件流里的失败信号：turn.failed / error。"""
    for ev in events:
        if ev.get("type") in ("turn.failed", "error"):
            err = ev.get("error") or ev.get("message") or ev
            return err.get("message", json.dumps(err, ensure_ascii=False)) \
                if isinstance(err, dict) else str(err)
    return None


def codex_ask(exe, prompt, model, images, timeout):
    """空目录 + 只读沙箱：把 Codex 当纯聊天模型，碰不到任何项目文件。
    图片由 CLI 在客户端读取后随 prompt 上传，不受沙箱限制。
    成功须同时满足：退出码 0、事件流无失败信号且这一轮正常结束、回答非空。"""
    workdir = tempfile.mkdtemp(prefix="askgpt_")
    out = os.path.join(workdir, "answer.md")
    cmd = _codex_base(exe, workdir, "read-only", model, images) + ["--json", "-o", out]
    with clean_codex_home() as home:
        env = dict(os.environ, CODEX_HOME=home) if home else None
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=workdir, env=env)
    events = _codex_events(r.stdout)
    tail = (r.stderr or "").strip()[-900:]
    failure = _codex_failure(events)
    if r.returncode != 0 or failure:
        raise RuntimeError("codex exec 失败（退出码 %s）%s。末尾输出：\n%s"
                           % (r.returncode, "：" + failure if failure else "", tail))
    if events and not any(ev.get("type") == "turn.completed" for ev in events):
        raise RuntimeError("codex exec 没有正常结束这一轮（缺少 turn.completed）。末尾输出：\n%s" % tail)
    ans = ""
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            ans = f.read().strip()
    if not ans:   # -o 没写出来时，从事件流里取最后一条非空回答
        for ev in reversed(events):
            it = ev.get("item") or {}
            if ev.get("type") == "item.completed" and it.get("type") == "agent_message" \
                    and (it.get("text") or "").strip():
                ans = it["text"].strip()
                break
    if not ans:
        raise RuntimeError("codex exec 正常结束但没有给出回答。末尾输出：\n%s" % tail)
    return ans


def _image_problem(data):
    """None = 完整图片；否则返回原因。只看文件头会放过写了一半的文件，所以也查结尾。"""
    if not data:
        return "空文件"
    if len(data) < 64:
        return "只有 %d 字节" % len(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None if b"IEND" in data[-16:] else "PNG 不完整（缺少 IEND 结尾）"
    if data.startswith(b"\xff\xd8\xff"):
        return None if data.rstrip(b"\x00").endswith(b"\xff\xd9") else "JPEG 不完整（缺少结束标记）"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return None if data.rstrip(b"\x00").endswith(b";") else "GIF 不完整"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return None if len(data) >= int.from_bytes(data[4:8], "little") + 8 else "WEBP 不完整"
    return "不是 PNG / JPEG / GIF / WEBP"


def render_context(exe, prompt, model):
    """渲染 GPT 实际会看到的上下文（codex debug prompt-input，不调用模型、不耗额度），
    并检查其中有没有用户的 AGENTS.md 与技能清单。"""
    workdir = tempfile.mkdtemp(prefix="askgpt_ctx_")
    cmd = [exe, "debug", "prompt-input"]
    if model:
        cmd += ["-c", "model=%s" % json.dumps(model)]
    with clean_codex_home() as home:
        env = dict(os.environ, CODEX_HOME=home) if home else None
        r = subprocess.run(cmd + [prompt], capture_output=True, text=True,
                           timeout=120, cwd=workdir, env=env)
    if r.returncode != 0:
        raise RuntimeError("codex debug prompt-input 失败：%s" % (r.stderr or r.stdout).strip()[-300:])
    raw = r.stdout
    data = json.loads(raw)
    items = data if isinstance(data, list) else (data.get("input") or [])
    segments = []
    for it in items:
        if it.get("tools") is not None:
            segments.append({"role": "tools", "chars": len(json.dumps(it, ensure_ascii=False)),
                             "summary": ", ".join(t.get("name") or t.get("type") or "?"
                                                  for t in it["tools"])})
            continue
        for c in it.get("content") or []:
            t = (c.get("text") or "").strip()
            if t:
                segments.append({"role": it.get("role"), "chars": len(t),
                                 "summary": t[:80].replace("\n", " ")})
    user_agents = ""
    try:
        with open(os.path.join(codex_home(), "AGENTS.md"), encoding="utf-8") as f:
            user_agents = f.read().strip()
    except OSError:
        pass
    return {
        "clean_context": LAST_CONTEXT["clean"],
        "clean_context_reason": LAST_CONTEXT["reason"],
        "agents_md_present": "AGENTS.md instructions" in raw
                             or bool(user_agents and user_agents[:40] in raw),
        "skills_list_present": "<skills_instructions>" in raw,
        "total_chars": len(raw),
        "segments": segments,
    }


def _looks_like_image(data):
    return _image_problem(data) is None


def _write_image_atomic(data, out_path):
    """先确认是真图片，再写同目录临时文件并原子替换。
    任何一步失败，目标路径上原有的文件都不会被动。"""
    problem = _image_problem(data)
    if problem:
        raise RuntimeError("拿到的不是有效图片（%s），目标文件未改动" % problem)
    d = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(prefix=".askgpt_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, out_path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def codex_gen_image(exe, prompt, out_path, model, timeout):
    """订阅生图：Codex 内置 image_gen 工具。
    不让 codex 往工作目录写文件 —— 有的平台沙箱不允许（如 Windows 上的 WorkBuddy）。
    codex 自己会把生成的图存进 <CODEX_HOME>/generated_images/<会话 ID>/，这一步不经过沙箱，
    所以用只读沙箱运行，从 --json 事件流拿会话 ID，再去那里取图。"""
    workdir = tempfile.mkdtemp(prefix="askgpt_img_")
    task = ("用你内置的图像生成工具画：\n%s\n\n"
            "生成即可，不要保存、复制或移动任何文件。" % prompt)
    cmd = _codex_base(exe, workdir, "read-only", model, None) + ["--json"]
    with clean_codex_home() as home:
        env = dict(os.environ, CODEX_HOME=home) if home else None
        r = subprocess.run(cmd, input=task, capture_output=True, text=True,
                           timeout=timeout, cwd=workdir, env=env)
        _pick_generated_image(r, home or codex_home(), workdir, out_path)


def _pick_generated_image(r, home, workdir, out_path):
    events = _codex_events(r.stdout)
    tail = (r.stderr or "").strip()[-600:]
    failure = _codex_failure(events)
    if r.returncode != 0 or failure:
        raise RuntimeError("Codex 生图失败（退出码 %s）%s，目标文件未改动。末尾输出：\n%s"
                           % (r.returncode, "：" + failure if failure else "", tail))
    thread_id = next((ev.get("thread_id") for ev in events
                      if ev.get("type") == "thread.started"), None)
    candidates = []
    if thread_id:
        gen_dir = os.path.join(home, "generated_images", thread_id)
        if os.path.isdir(gen_dir):
            candidates += [os.path.join(gen_dir, f) for f in os.listdir(gen_dir)]
    # 旧版 codex 可能把图直接落在工作目录
    candidates += [os.path.join(workdir, f) for f in os.listdir(workdir)]
    problems = []
    for c in sorted((c for c in candidates if os.path.isfile(c)),
                    key=os.path.getmtime, reverse=True):          # 最新的优先
        with open(c, "rb") as f:
            data = f.read()
        problem = _image_problem(data)
        if problem is None:
            _write_image_atomic(data, out_path)
            return
        problems.append("%s: %s" % (os.path.basename(c), problem))
    raise RuntimeError(
        "Codex 本次没有产出有效图片%s，目标文件未改动。可能原因：当前账号或平台没有 image_gen 工具"
        "（Windows/WSL 已知问题 openai/codex#19133）。末尾输出：\n%s"
        % ("（%s）" % "；".join(problems) if problems else "", tail))


# ---------- API 路径 ----------

def _api_post(path, payload, timeout):
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("OpenAI API 错误 %s: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:600]))
    except urllib.error.URLError as e:
        raise RuntimeError("连不上 OpenAI（%s）。若设了 HTTPS_PROXY，请确认代理可用" % e.reason)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("OpenAI 返回的不是 JSON（可能被代理或网关拦截）：%s"
                           % body[:200].decode("utf-8", "replace"))


def _extract_text(resp):
    return "".join(c.get("text", "") for o in (resp.get("output") or [])
                   if o.get("type") == "message" for c in (o.get("content") or [])
                   if c.get("type") == "output_text")


def _extract_refusal(resp):
    return "".join(c.get("refusal", "") for o in (resp.get("output") or [])
                   if o.get("type") == "message" for c in (o.get("content") or [])
                   if c.get("type") == "refusal")


def api_ask(prompt, model, images, timeout):
    if images:
        content = [{"type": "input_text", "text": prompt}]
        for p in images:
            mime = mimetypes.guess_type(p)[0] or "image/png"
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            content.append({"type": "input_image",
                            "image_url": "data:%s;base64,%s" % (mime, b64),
                            "detail": "auto"})
        payload = {"model": model, "input": [{"role": "user", "content": content}]}
    else:
        payload = {"model": model, "input": prompt}
    resp = _api_post("/responses", payload, timeout)
    # 成功须同时满足：没有 error、status 为 completed、有文本回答。
    # 任何一项不满足都报错 —— 绝不把原始响应当成「GPT 的回答」返回。
    if resp.get("error"):
        raise RuntimeError("OpenAI 返回错误：%s" % json.dumps(resp["error"], ensure_ascii=False)[:400])
    status = resp.get("status")
    if status and status != "completed":
        detail = resp.get("incomplete_details")
        raise RuntimeError("OpenAI 响应未完成（status=%s）%s"
                           % (status, "：" + json.dumps(detail, ensure_ascii=False) if detail else ""))
    text = _extract_text(resp)
    if text.strip():
        return text
    refusal = _extract_refusal(resp)
    if refusal:
        raise RuntimeError("GPT 拒绝回答：%s" % refusal)
    raise RuntimeError("OpenAI 响应里没有文本回答")


def api_gen_image(prompt, out_path, image_model, size, timeout):
    resp = _api_post("/images/generations",
                     {"model": image_model, "prompt": prompt, "size": size}, timeout)
    items = resp.get("data") or []
    if not items:
        raise RuntimeError("图像 API 没有返回图片，目标文件未改动：%s"
                           % json.dumps(resp, ensure_ascii=False)[:300])
    item = items[0]
    if item.get("b64_json"):
        try:
            data = base64.b64decode("".join(item["b64_json"].split()), validate=True)
        except Exception as e:
            raise RuntimeError("图像 API 返回的 base64 无法解码，目标文件未改动：%s" % e)
    elif item.get("url"):                       # dall-e 系列默认返回 url
        data = urllib.request.urlopen(item["url"], timeout=timeout).read()
    else:
        raise RuntimeError("图像 API 返回格式无法识别，目标文件未改动：%s"
                           % json.dumps(item, ensure_ascii=False)[:300])
    _write_image_atomic(data, out_path)


# ---------- 主流程 ----------

def read_prompt(args):
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise Fail("bad_input", "读不到 prompt 文件 %s：%s" % (args.file, e.strerror or e))
    elif args.prompt is not None:
        text = args.prompt
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise Fail("bad_input", "缺少 prompt：请给出参数、-f 文件或 stdin 输入")
    if not text.strip():
        raise Fail("bad_input", "prompt 为空")
    return text


CONTEXT_CHAR_LIMIT = 1500000


def read_context_files(paths):
    """读 --context 附上的背景资料（讨论简报、待审原文、代码、数据片段），须为 UTF-8 文本。"""
    out = []
    for p in paths or []:
        full = os.path.abspath(os.path.expanduser(p))
        try:
            with open(full, encoding="utf-8") as f:
                out.append((full, f.read()))
        except UnicodeDecodeError:
            raise Fail("bad_input", "%s 不是 UTF-8 文本文件（图片请用 --see）" % p)
        except OSError as e:
            raise Fail("bad_input", "读不到背景文件 %s：%s" % (p, e.strerror or e))
    return out


def assemble_prompt(task, contexts):
    """GPT 看不到宿主 agent 与用户之前的对话，它知道的只有这里拼出来的内容。
    背景与任务用标签分开，避免 GPT 把背景资料当成要回答的问题。"""
    if not contexts:
        return task
    files = "\n\n".join('<file name="%s">\n%s\n</file>' % (os.path.basename(p), t.strip())
                         for p, t in contexts)
    return ("The <background> section is reference material for the task below. "
            "It is not itself a question.\n\n"
            "<background>\n%s\n</background>\n\n<task>\n%s\n</task>" % (files, task.strip()))


class _Parser(argparse.ArgumentParser):
    """参数错误时，--json 模式也输出统一的 JSON 错误，而不是 argparse 的纯文本用法说明。"""
    def error(self, message):
        if "--json" in sys.argv[1:]:
            print(json.dumps({"ok": False, "error_code": "bad_args", "error": message},
                             ensure_ascii=False, indent=2))
            sys.exit(2)
        super().error(message)


def _normalize_env():
    """CODEX_HOME / ASK_GPT_CODEX 统一转成绝对路径并写回环境变量。
    codex 是在临时目录里运行的，相对路径在那里会解析到别处。"""
    for var in ("CODEX_HOME", "ASK_GPT_CODEX"):
        v = os.environ.get(var)
        if v:
            os.environ[var] = os.path.abspath(os.path.expanduser(v))


BILLING_TEXT = {
    "subscription": "ChatGPT 包月订阅额度，不额外计费",
    "per-token": "⚠️ 按 token 计费 —— codex 用的是 API Key 登录，不是订阅额度",
    "unknown": "⚠️ 无法确认 —— 认不出 codex 的登录方式，不要假定免费",
}


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    try:
        return _run(as_json)
    except Fail as f:
        out = {"ok": False, "error_code": f.code, "error": f.message}
        out.update(f.extra)
        if as_json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f.message, file=sys.stderr)
        return f.status


def _run(as_json):
    _normalize_env()
    ap = _Parser(description="调用你的 OpenAI 订阅 / API 问 GPT（问答 / 读图 / 生图）")
    ap.add_argument("prompt", nargs="?", help="问题文本；缺省时从 -f 或 stdin 读")
    ap.add_argument("-f", "--file", help="从文件读完整 prompt（长内容推荐）")
    ap.add_argument("--context", action="append", metavar="FILE",
                    help="背景资料：讨论简报、待审原文、代码、数据片段（UTF-8 文本，可重复）。"
                         "GPT 看不到你们之前的对话，需要它知道的都放进来")
    ap.add_argument("--dry-run", action="store_true",
                    help="只拼出最终发给 GPT 的完整内容并输出，不调用 GPT")
    ap.add_argument("--see", action="append", metavar="IMG",
                    help="读图：把图片随问题发给 GPT；多张就重复 --see")
    ap.add_argument("--gen", metavar="OUT.png", help="生图：图片存到此路径")
    ap.add_argument("--size", default=None, help="生图尺寸，默认 1024x1024（仅 API 路径生效）")
    ap.add_argument("-m", "--model", help="本次覆盖文本模型")
    ap.add_argument("--path", choices=("codex", "api"), help="本次覆盖路径")
    ap.add_argument("--timeout", type=int, default=600, help="超时秒数（默认 600）")
    # 状态 / 配置
    ap.add_argument("--probe", action="store_true",
                    help="探测路径、登录方式与可用模型。会请求模型目录（codex debug models / "
                         "GET /v1/models），但不做任何模型推理，不消耗推理额度")
    ap.add_argument("--show-context", action="store_true",
                    help="渲染 GPT 实际会看到的上下文并检查是否干净（不调用模型、不耗额度）")
    ap.add_argument("--list-models", action="store_true",
                    help="列出当前路径实际可用的模型（运行时从账号拉取，非硬编码）")
    ap.add_argument("--json", action="store_true",
                    help="所有结果与错误都输出 JSON，便于任意 agent 解析")
    ap.add_argument("--set-path", choices=("codex", "api"), help="写入默认路径")
    ap.add_argument("-M", "--set-model", help="写入默认文本模型")
    ap.add_argument("--set-image-model", help="写入默认生图模型（API 路径）")
    ap.add_argument("--show-config", action="store_true")
    ap.add_argument("--reset-config", action="store_true")
    args = ap.parse_args()
    if args.timeout <= 0:
        raise Fail("bad_args", "--timeout 必须是正整数")

    def done(payload, text):
        if as_json:
            print(json.dumps(dict({"ok": True}, **payload), ensure_ascii=False, indent=2))
        else:
            print(text)
        return 0

    # ---------- 配置类操作 ----------
    if args.reset_config:
        existed = os.path.exists(CONFIG_PATH)
        if existed:
            os.remove(CONFIG_PATH)
        return done({"action": "reset", "config_path": CONFIG_PATH},
                    "已清除 %s" % CONFIG_PATH if existed else "没有配置文件，无需清除")

    if args.set_path or args.set_model is not None or args.set_image_model is not None:
        warnings = []
        try:
            cfg = load_config()
        except Fail as e:
            cfg = {}
            warnings.append("原配置无效，已重建：%s" % e.message)
        if args.set_path:
            cfg["path"] = args.set_path
        if args.set_model is not None:
            if not args.set_model.strip():
                raise Fail("bad_args", "--set-model 不能为空")
            # 不做白名单拦截：发现列表可能滞后于 OpenAI 的实际上线情况
            known = [m["id"] for m in models_for(cfg.get("path") or "codex")]
            if known and args.set_model.strip() not in known:
                warnings.append("%s 不在当前发现到的模型里（%s）。仍然写入 —— 新模型可能尚未进入本地缓存"
                                % (args.set_model.strip(), ", ".join(known)))
            cfg["model"] = args.set_model.strip()
        if args.set_image_model is not None:
            if not args.set_image_model.strip():
                raise Fail("bad_args", "--set-image-model 不能为空")
            cfg["image_model"] = args.set_image_model.strip()
        save_config(cfg)
        if not as_json:
            for w in warnings:
                print("提示：" + w, file=sys.stderr)
        return done({"action": "set", "config": cfg, "config_path": CONFIG_PATH,
                     "warnings": warnings},
                    "已写入 %s:\n%s" % (CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2)))

    if args.show_config:
        cfg = load_config()
        return done({"config": cfg, "config_path": CONFIG_PATH},
                    json.dumps(cfg, ensure_ascii=False, indent=2))

    if args.list_models:
        cfg = load_config()
        path = args.path or cfg.get("path") or ("codex" if codex_state()["available"] else "api")
        ms = models_for(path)
        if not ms:
            raise Fail("no_models", "未能发现模型（路径 %s）。订阅路径需要 codex 已登录（数据目录 %s）；"
                       "API 路径需要 OPENAI_API_KEY" % (path, codex_home()), path=path)
        dflt = cfg.get("model") or (codex_default_model() if path == "codex" else None)
        src = "账号实时目录（codex debug models）" if path == "codex" else "GET /v1/models"
        lines = ["路径 %s 当前可用模型 —— 来源：%s" % (path, src)]
        for m in ms:
            bits = []
            if m.get("context"):
                bits.append("ctx %s" % m["context"])
            if m.get("vision") is True:
                bits.append("支持图像输入")
            extra = ("  [%s]" % ", ".join(bits)) if bits else ""
            lines.append(" %s %-20s %s%s" % ("★" if m["id"] == dflt else " ", m["id"],
                                             m.get("desc") or m.get("label") or "", extra))
        if dflt:
            lines.append("\n★ = 当前默认（%s）" % ("ask-gpt 配置" if cfg.get("model") else "你的 Codex 配置"))
        lines.append("换模型：%s --set-model <模型名>" % os.path.basename(__file__))
        return done({"path": path, "source": src, "default": dflt, "models": ms}, "\n".join(lines))

    if args.probe:
        st = probe()
        avail = st["codex"]["available"] or st["api"]["available"]
        if as_json:
            out = dict({"ok": avail}, **st)
            if not avail:
                out.update(error_code="no_auth", error="订阅与 API 两条路径都不可用")
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            cx = st["codex"]
            print("Codex CLI:   %s — %s" % ("可用" if cx["available"] else "不可用", cx["detail"]))
            if cx["available"]:
                print("  计费: %s" % BILLING_TEXT[cx["billing"]])
                ci = cx["context_isolation"]
                print("  上下文隔离: %s" % ("✅ 开启 —— GPT 看不到你的 AGENTS.md / 技能 / 插件 / MCP"
                                            if ci["enabled"] else "⚠️ 关闭 —— " + ci["reason"]))
            if cx.get("version"):
                print("  使用: %s (%s) | 默认模型: %s"
                      % (cx["exe"], cx["version"], cx.get("default_model") or "未设置"))
            for i in [i for i in cx.get("installs", []) if i["path"] != cx["exe"]]:
                print("  另有副本: %s (%s) —— 已自动跳过" % (i["path"], i["version"]))
            if cx["models"]:
                print("  账号可用模型: %s" % ", ".join(m["id"] for m in cx["models"]))
            print("API Key:     %s — %s" % ("可用" if st["api"]["available"] else "不可用",
                                             st["api"]["detail"]))
            if st["api"]["available"] and st["api"].get("models"):
                print("  可用模型: %d 个（--list-models 查看）" % len(st["api"]["models"]))
            if st["config_error"]:
                print("配置: ⚠️ %s" % st["config_error"])
            else:
                print("已配置: %s" % (json.dumps(st["config"], ensure_ascii=False)
                                      if st["configured"] else "否（需先选路径）"))
        return 0 if avail else 2

    if args.show_context:
        cfg = load_config()
        cx = codex_state()
        path = args.path or cfg.get("path") or ("codex" if cx["available"] else "api")
        prompt = assemble_prompt(args.prompt or "（你的问题）", read_context_files(args.context))
        if path == "api":
            return done({"path": "api", "clean_context": True, "agents_md_present": False,
                         "skills_list_present": False,
                         "segments": [{"role": "user", "chars": len(prompt), "summary": prompt[:80]}]},
                        "API 路径只发送你的问题（和图片），没有任何其他上下文。")
        if not cx["available"]:
            raise Fail("path_unavailable", "Codex 不可用：%s" % cx["detail"], path="codex")
        model = args.model or cfg.get("model")
        try:
            ctx = render_context(cx["exe"], prompt, model)
        except Exception as e:
            raise Fail("call_failed", str(e), status=1)
        lines = ["GPT 实际会看到的上下文（共 %d 字符，未调用模型）：" % ctx["total_chars"]]
        for seg in ctx["segments"]:
            lines.append("  [%-9s] %6d 字 | %s" % (seg["role"], seg["chars"], seg["summary"]))
        clean = ctx["clean_context"] and not ctx["agents_md_present"] and not ctx["skills_list_present"]
        lines.append("")
        lines.append("你的 AGENTS.md：%s" % ("⚠️ 在上下文里" if ctx["agents_md_present"] else "✅ 不在"))
        lines.append("技能清单：    %s" % ("⚠️ 在上下文里" if ctx["skills_list_present"] else "✅ 不在"))
        lines.append("结论：%s" % ("✅ 干净，适合盲评" if clean else
                                   "⚠️ 不干净" + ("（%s）" % ctx["clean_context_reason"]
                                                  if ctx["clean_context_reason"] else "")))
        return done(dict(ctx, path="codex", clean=clean), "\n".join(lines))

    # ---------- 调用 ----------
    if args.gen and args.see:
        raise Fail("bad_args", "--gen 与 --see 不能同时使用")
    if args.gen and args.context:
        raise Fail("bad_args", "--gen 不支持 --context（把要画的内容直接写进描述）")

    # 订阅路径会在临时空目录里运行 codex（隔离项目文件），相对路径在那里解析不到，
    # 所以一律先转成绝对路径。
    see = [os.path.abspath(os.path.expanduser(p)) for p in (args.see or [])]
    missing = [p for p in see if not os.path.isfile(p)]
    if missing:
        raise Fail("bad_input", "图片不存在：%s" % ", ".join(missing))
    gen_out = os.path.abspath(os.path.expanduser(args.gen)) if args.gen else None
    if gen_out and not os.path.isdir(os.path.dirname(gen_out)):
        raise Fail("bad_input", "生图输出目录不存在：%s" % os.path.dirname(gen_out))
    contexts = read_context_files(args.context)
    prompt = assemble_prompt(read_prompt(args), contexts)
    if len(prompt) > CONTEXT_CHAR_LIMIT:
        raise Fail("bad_input", "发给 GPT 的内容共 %d 字符，超过上限 %d。请先摘要背景资料"
                   % (len(prompt), CONTEXT_CHAR_LIMIT))
    sent = {"prompt_chars": len(prompt), "context_files": [p for p, _ in contexts]}

    # --dry-run 在选路径之前返回：只是拼内容给人看，不需要登录
    if args.dry_run:
        if as_json:
            print(json.dumps(dict({"ok": True, "dry_run": True, "prompt": prompt,
                                   "images_in": see}, **sent), ensure_ascii=False, indent=2))
        else:
            print(prompt)
            print("\n[未调用 GPT：共 %d 字符，背景文件 %d 个%s]"
                  % (len(prompt), len(contexts), "，图片 %d 张" % len(see) if see else ""),
                  file=sys.stderr)
        return 0

    cfg = load_config()
    cx, apst = codex_state(), api_state()
    path = args.path or cfg.get("path")
    if not path:
        if cx["available"] and not apst["available"]:
            path = "codex"
        elif apst["available"] and not cx["available"]:
            path = "api"
        elif cx["available"] and apst["available"]:
            raise Fail("path_not_chosen",
                       "两条路径都可用，但尚未选定默认路径。先选一条：\n"
                       "  --set-path codex   （%s）\n  --set-path api     （按 token 计费）"
                       % BILLING_TEXT[cx["billing"] or "unknown"].lstrip("⚠️ "),
                       codex_billing=cx["billing"])
        else:
            raise Fail("no_auth", "未检测到可用的 OpenAI 认证。\n  订阅: %s\n  API:  %s\n\n%s"
                       % (cx["detail"], apst["detail"], SETUP_HINT),
                       codex_detail=cx["detail"], api_detail=apst["detail"])
    if path == "codex" and not cx["available"]:
        raise Fail("path_unavailable", "当前路径为 codex，但 Codex 不可用：%s" % cx["detail"],
                   path="codex")
    if path == "api" and not apst["available"]:
        raise Fail("path_unavailable", "当前路径为 api，但%s" % apst["detail"], path="api")

    model = resolve_model(path, args, cfg)
    image_model = (cfg.get("image_model") or os.environ.get("OPENAI_IMAGE_MODEL")
                   or FALLBACK_IMAGE_MODEL)
    model_tag = model or (codex_default_model() if path == "codex" else None) \
        or "Codex 默认模型"
    size = args.size or os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

    # 读图前对模型做能力提示（只提示不拦截，元数据可能滞后）
    if see and model and not as_json:
        for m in (codex_models(live=False) if path == "codex" else []):
            if m["id"] == model and m.get("vision") is False:
                print("提示：%s 的元数据未标注图像输入能力，读图可能失败。" % model, file=sys.stderr)
                break

    LAST_CONTEXT.update(clean=None, reason=None)
    billing = (cx.get("billing") or "unknown") if path == "codex" else "per-token"
    path_label = {
        ("codex", "subscription"): "ChatGPT 订阅 via codex exec",
        ("codex", "per-token"):    "Codex CLI（API Key 登录，按 token 计费）",
        ("codex", "unknown"):      "Codex CLI（登录方式未知，无法确认是否计费）",
    }.get((path, billing), "OpenAI API（按 token 计费）")
    try:
        if gen_out:
            if path == "codex":
                codex_gen_image(cx["exe"], prompt, gen_out, model, args.timeout)
                used = "image_gen（Codex 内置）"
            else:
                api_gen_image(prompt, gen_out, image_model, size, args.timeout)
                used = "%s，%s" % (image_model, size)
            result = {"ok": True, "mode": "gen", "path": path, "billing": billing,
                      "model": used, "image": gen_out}
        else:
            if path == "codex":
                answer = codex_ask(cx["exe"], prompt, model, see or None, args.timeout)
            else:
                answer = api_ask(prompt, model, see or None, args.timeout)
            result = {"ok": True, "mode": "see" if see else "ask", "path": path,
                      "billing": billing, "model": model_tag, "answer": answer,
                      "images_in": see, **sent}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error_code": "timeout", "path": path, "billing": billing,
                  "model": model_tag, "error": "超过 %d 秒未完成（--timeout 可调大）" % args.timeout}
    except Exception as e:
        result = {"ok": False, "error_code": "call_failed", "path": path, "billing": billing,
                  "model": model_tag, "error": str(e)}

    # API 路径只发送用户的问题；codex 路径取决于本次是否成功隔离
    result["clean_context"] = True if path == "api" else LAST_CONTEXT["clean"]
    if path == "codex" and LAST_CONTEXT["clean"] is False:
        result["clean_context_reason"] = LAST_CONTEXT["reason"]
        if not as_json:
            print("提示：本次未隔离上下文（%s），GPT 可能读到你的 AGENTS.md 等个人配置。"
                  % LAST_CONTEXT["reason"], file=sys.stderr)

    # --json：给任意 agent 解析用的稳定结构；否则沿用人类可读格式
    #（回答走 stdout，路径/模型标注走 stderr，方便管道里只取回答）
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result["ok"]:
        print("调用失败：%s" % result["error"], file=sys.stderr)
    elif result["mode"] == "gen":
        print("saved: %s" % result["image"])
        print("[生图路径: %s，%s]" % (path_label, result["model"]), file=sys.stderr)
    else:
        print(result["answer"])
        n = len(result["images_in"])
        print("\n[路径: %s，模型 %s%s]" % (path_label, result["model"],
                                           "，%d 张图" % n if n else ""), file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

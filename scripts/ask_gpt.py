#!/usr/bin/env python3
"""ask_gpt.py v4 — 在 Claude / Claude Code 里调用你的 OpenAI 订阅或 API Key。

两条路径，功能对等（问答 / 读图 / 生图）：
  订阅路径 codex : ChatGPT 包月额度，经官方 Codex CLI，不产生 API 账单
  API 路径  api  : OPENAI_API_KEY 直连 OpenAI，按 token 计费

路径与模型选定后写入 ~/.config/ask-gpt/config.json，之后不再追问。

本脚本全程非交互（绝不读 stdin 做问答）——选择由 Claude 在对话里问用户，
再用 --set-path / --set-model 落盘。这样在工具调用里不会卡住。

常用：
  python3 ask_gpt.py --probe --json              # 探测两条路径+当前配置（不发任何请求）
  python3 ask_gpt.py --set-path codex -M gpt-5.6-sol
  python3 ask_gpt.py "你的问题"                   # 问答
  python3 ask_gpt.py -f prompt.md                # 长 prompt 从文件读
  python3 ask_gpt.py --see 图.png "这是什么？"     # 读图；多张就重复 --see
  python3 ask_gpt.py --gen out.png -f desc.txt   # 生图
  python3 ask_gpt.py --path api "问题"            # 本次强制某路径

退出码：0 成功；2 无可用认证 / 未配置；1 调用失败。
"""
import argparse
import base64
import json
import mimetypes
import re
import os
import shutil
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

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def codex_state():
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
    return {"available": r.returncode == 0, "exe": exe,
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


def codex_default_model():
    """读 <CODEX_HOME>/config.toml 顶层的 model =，仅用于署名，不修改它。"""
    try:
        with open(os.path.join(codex_home(), "config.toml"), encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("["):  # 进入子表就停，只看顶层
                    break
                m = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


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
    cfg = load_config()
    cx, ap = codex_state(), api_state()
    cx["installs"] = codex_installs()
    cx["models"] = codex_models() if cx["available"] else []
    ap["models"] = api_models() if ap["available"] else []
    return {
        "codex": cx,
        "api": ap,
        "config": cfg,
        "config_path": CONFIG_PATH,
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


def codex_ask(exe, prompt, model, images, timeout):
    """空目录 + 只读沙箱：把 Codex 当纯聊天模型，碰不到任何项目文件。
    图片由 CLI 在客户端读取后随 prompt 上传，不受沙箱限制。"""
    workdir = tempfile.mkdtemp(prefix="askgpt_")
    out = os.path.join(workdir, "answer.md")
    cmd = _codex_base(exe, workdir, "read-only", model, images) + ["-o", out]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=timeout, cwd=workdir)
    if os.path.exists(out):
        ans = open(out, encoding="utf-8").read().strip()
        if ans:
            return ans
    tail = (r.stderr or r.stdout or "").strip()[-900:]
    raise RuntimeError("codex exec 未返回回答（退出码 %s）。末尾输出：\n%s" % (r.returncode, tail))


def codex_gen_image(exe, prompt, out_path, model, timeout):
    """订阅生图：Codex 内置 image_gen 工具（feature image_generation 默认 stable/true）。"""
    workdir = tempfile.mkdtemp(prefix="askgpt_img_")
    task = ("用你内置的图像生成工具画：\n%s\n\n"
            "把最终图片保存为当前工作目录下的 image.png。"
            "如果工具只返回预览而没落盘，就自行把图像字节写入 image.png。"
            "完成后用 ls 确认文件确实存在。" % prompt)
    cmd = _codex_base(exe, workdir, "workspace-write", model, None)
    r = subprocess.run(cmd, input=task, capture_output=True, text=True,
                       timeout=timeout, cwd=workdir)
    imgs = [f for f in os.listdir(workdir)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    if not imgs:
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        raise RuntimeError("Codex 未产出图像文件（已知问题：image_gen 偶尔只给预览不落盘；"
                           "Windows/WSL 可能无此工具）。末尾输出：\n%s" % tail)
    src = "image.png" if "image.png" in imgs else imgs[0]
    shutil.copyfile(os.path.join(workdir, src), out_path)


# ---------- API 路径 ----------

def _api_post(path, payload, timeout):
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise RuntimeError("OpenAI API 错误 %s: %s" % (e.code, e.read().decode()[:600]))
    except urllib.error.URLError as e:
        raise RuntimeError("连不上 OpenAI（%s）。若设了 HTTPS_PROXY，请确认代理可用" % e.reason)


def _extract_text(resp):
    return "".join(c.get("text", "") for o in resp.get("output", [])
                   if o.get("type") == "message" for c in o.get("content", [])
                   if c.get("type") == "output_text")


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
    return _extract_text(resp) or json.dumps(resp, ensure_ascii=False)[:2000]


def api_gen_image(prompt, out_path, image_model, size, timeout):
    resp = _api_post("/images/generations",
                     {"model": image_model, "prompt": prompt, "size": size}, timeout)
    open(out_path, "wb").write(base64.b64decode(resp["data"][0]["b64_json"]))


# ---------- 主流程 ----------

def read_prompt(args, ap):
    if args.file:
        return open(args.file, encoding="utf-8").read()
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    ap.error("缺少 prompt：请给出参数、-f 文件或 stdin 输入")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="调用你的 OpenAI 订阅 / API 问 GPT（问答 / 读图 / 生图）")
    ap.add_argument("prompt", nargs="?", help="问题文本；缺省时从 -f 或 stdin 读")
    ap.add_argument("-f", "--file", help="从文件读完整 prompt（长内容推荐）")
    ap.add_argument("--see", action="append", metavar="IMG",
                    help="读图：把图片随问题发给 GPT；多张就重复 --see")
    ap.add_argument("--gen", metavar="OUT.png", help="生图：图片存到此路径")
    ap.add_argument("--size", default=None, help="生图尺寸，默认 1024x1024（仅 API 路径生效）")
    ap.add_argument("-m", "--model", help="本次覆盖文本模型")
    ap.add_argument("--path", choices=("codex", "api"), help="本次覆盖路径")
    ap.add_argument("--timeout", type=int, default=600, help="超时秒数（默认 600）")
    # 状态 / 配置
    ap.add_argument("--probe", action="store_true", help="探测路径与配置，不发请求")
    ap.add_argument("--list-models", action="store_true",
                    help="列出当前路径实际可用的模型（运行时发现，非硬编码）")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON（问答/读图/生图/--probe 均适用），便于任意 agent 解析")
    ap.add_argument("--set-path", choices=("codex", "api"), help="写入默认路径")
    ap.add_argument("-M", "--set-model", help="写入默认文本模型")
    ap.add_argument("--set-image-model", help="写入默认生图模型（API 路径）")
    ap.add_argument("--show-config", action="store_true")
    ap.add_argument("--reset-config", action="store_true")
    args = ap.parse_args()

    # 配置类操作
    if args.reset_config:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        print("已清除 %s" % CONFIG_PATH)
        return 0
    if args.set_path or args.set_model or args.set_image_model:
        cfg = load_config()
        if args.set_path:
            cfg["path"] = args.set_path
        if args.set_model:
            # 不做白名单拦截：发现列表可能滞后于 OpenAI 的实际上线情况
            known = [m["id"] for m in models_for(args.set_path or cfg.get("path") or "codex")]
            if known and args.set_model not in known:
                print("提示：%s 不在当前发现到的模型里（%s）。仍然写入 —— "
                      "新模型可能尚未进入本地缓存。" % (args.set_model, ", ".join(known)),
                      file=sys.stderr)
            cfg["model"] = args.set_model
        if args.set_image_model:
            cfg["image_model"] = args.set_image_model
        save_config(cfg)
        print("已写入 %s:\n%s" % (CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2)))
        return 0
    if args.show_config:
        print(json.dumps(load_config(), ensure_ascii=False, indent=2) or "{}")
        return 0
    if args.list_models:
        cfg = load_config()
        path = args.path or cfg.get("path")
        if not path:
            path = "codex" if codex_state()["available"] else "api"
        ms = models_for(path)
        if not ms:
            print("未能发现模型（路径 %s）。订阅路径依赖 %s；API 路径需要 OPENAI_API_KEY。"
                  % (path, codex_home()), file=sys.stderr)
            return 2
        dflt = cfg.get("model") or (codex_default_model() if path == "codex" else None)
        src = "账号实时目录（codex debug models）" if path == "codex" else "GET /v1/models"
        print("路径 %s 当前可用模型 —— 来源：%s" % (path, src))
        for m in ms:
            bits = []
            if m.get("context"):
                bits.append("ctx %s" % m["context"])
            if m.get("vision") is True:
                bits.append("支持图像输入")
            extra = ("  [%s]" % ", ".join(bits)) if bits else ""
            star = "★" if m["id"] == dflt else " "
            print(" %s %-20s %s%s" % (star, m["id"], m.get("desc") or m.get("label") or "", extra))
        if dflt:
            print("\n★ = 当前默认（%s）" % ("ask-gpt 配置" if cfg.get("model") else "你的 Codex 配置"))
        print("换模型：%s --set-model <模型名>" % os.path.basename(__file__))
        return 0

    if args.probe:
        st = probe()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print("订阅(codex): %s — %s" % ("可用" if st["codex"]["available"] else "不可用",
                                            st["codex"]["detail"]))
            if st["codex"].get("version"):
                print("  使用: %s (%s) | 默认模型: %s"
                      % (st["codex"]["exe"], st["codex"]["version"],
                         st["codex"].get("default_model") or "未设置"))
            others = [i for i in st["codex"].get("installs", []) if i["path"] != st["codex"]["exe"]]
            for i in others:
                print("  另有旧副本: %s (%s) —— 已自动跳过" % (i["path"], i["version"]))
            if st["codex"]["models"]:
                print("  账号可用模型: %s" % ", ".join(m["id"] for m in st["codex"]["models"]))
            print("API Key:     %s — %s" % ("可用" if st["api"]["available"] else "不可用",
                                             st["api"]["detail"]))
            if st["api"]["available"] and st["api"].get("models"):
                print("  可用模型: %d 个（--list-models 查看）" % len(st["api"]["models"]))
            print("已配置: %s" % (json.dumps(st["config"], ensure_ascii=False)
                                  if st["configured"] else "否（需先选路径）"))
        return 0 if (st["codex"]["available"] or st["api"]["available"]) else 2

    # 调用
    cfg = load_config()
    cx, apst = codex_state(), api_state()
    path = args.path or cfg.get("path")
    if not path:
        if cx["available"] and not apst["available"]:
            path = "codex"
        elif apst["available"] and not cx["available"]:
            path = "api"
        elif cx["available"] and apst["available"]:
            print("两条路径都可用，但尚未选定默认路径。\n"
                  "请先选一条：ask_gpt.py --set-path codex   (订阅，不额外计费)\n"
                  "            ask_gpt.py --set-path api     (API，按 token 计费)",
                  file=sys.stderr)
            return 2
        else:
            print("未检测到可用的 OpenAI 认证。\n  订阅: %s\n  API:  %s\n\n%s"
                  % (cx["detail"], apst["detail"], SETUP_HINT), file=sys.stderr)
            return 2

    if path == "codex" and not cx["available"]:
        print("配置为订阅路径，但 Codex 不可用：%s\n%s" % (cx["detail"], SETUP_HINT),
              file=sys.stderr)
        return 2
    if path == "api" and not apst["available"]:
        print("配置为 API 路径，但 %s\n%s" % (apst["detail"], SETUP_HINT), file=sys.stderr)
        return 2

    model = resolve_model(path, args, cfg)
    image_model = (cfg.get("image_model") or os.environ.get("OPENAI_IMAGE_MODEL")
                   or FALLBACK_IMAGE_MODEL)
    model_tag = model or (codex_default_model() if path == "codex" else None) \
        or "Codex 默认模型"

    # 读图前对模型做能力提示（只提示不拦截，元数据可能滞后）
    if args.see and model:
        for m in (codex_models(live=False) if path == "codex" else []):
            if m["id"] == model and m.get("vision") is False:
                print("提示：%s 的元数据未标注图像输入能力，读图可能失败。" % model,
                      file=sys.stderr)
                break
    size = args.size or os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

    # 订阅路径会在临时空目录里运行 codex（隔离项目文件），相对路径在那里解析不到，
    # 所以一律先转成绝对路径。
    args.see = [os.path.abspath(img) for img in (args.see or [])] or None
    for img in (args.see or []):
        if not os.path.isfile(img):
            print("图片不存在：%s" % img, file=sys.stderr)
            return 1

    billing = "subscription" if path == "codex" else "per-token"
    path_label = "ChatGPT 订阅 via codex exec" if path == "codex" else "OpenAI API（按 token 计费）"
    try:
        if args.gen:
            prompt = read_prompt(args, ap)
            out = os.path.abspath(args.gen)
            if path == "codex":
                codex_gen_image(cx["exe"], prompt, out, model, args.timeout)
                used = "image_gen（Codex 内置）"
            else:
                api_gen_image(prompt, out, image_model, size, args.timeout)
                used = "%s，%s" % (image_model, size)
            result = {"ok": True, "mode": "gen", "path": path, "billing": billing,
                      "model": used, "image": out}
        else:
            prompt = read_prompt(args, ap)
            if not prompt.strip():
                ap.error("prompt 为空")
            if path == "codex":
                answer = codex_ask(cx["exe"], prompt, model, args.see, args.timeout)
            else:
                answer = api_ask(prompt, model, args.see, args.timeout)
            result = {"ok": True, "mode": "see" if args.see else "ask", "path": path,
                      "billing": billing, "model": model_tag, "answer": answer,
                      "images_in": args.see or []}
    except Exception as e:
        result = {"ok": False, "path": path, "billing": billing, "model": model_tag,
                  "error": str(e)}

    # --json：给任意 agent 解析用的稳定结构；否则沿用人类可读格式
    #（回答走 stdout，路径/模型标注走 stderr，方便管道里只取回答）
    if args.json:
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

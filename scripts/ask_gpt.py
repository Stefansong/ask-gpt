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
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/ask-gpt/config.json")
API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

TEXT_MODELS = {
    "gpt-5.6-sol":   "最强，推理/长文/疑难问题（两条路径都默认用它）",
    "gpt-5.6-terra": "均衡，日常问答与润色，速度快",
    "gpt-5.6-luna":  "最便宜最快，简单任务、批量处理",
}
DEFAULT_TEXT_MODEL = "gpt-5.6-sol"
DEFAULT_IMAGE_MODEL = "gpt-image-2"

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

def find_codex():
    p = shutil.which("codex")
    if p:
        return p
    for c in CODEX_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def codex_state():
    exe = find_codex()
    if not exe:
        return {"available": False, "exe": None, "detail": "未找到 codex 可执行文件"}
    try:
        r = subprocess.run([exe, "login", "status"],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"available": False, "exe": exe, "detail": "codex login status 执行失败: %s" % e}
    detail = (r.stdout or r.stderr or "").strip().splitlines()
    return {"available": r.returncode == 0, "exe": exe,
            "detail": detail[0] if detail else "退出码 %s" % r.returncode}


def codex_models():
    """从 Codex 本地模型缓存里取面向用户的 gpt-5.6-* 列表。"""
    try:
        with open(os.path.expanduser("~/.codex/models_cache.json"), encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return []
    return sorted(m for m in TEXT_MODELS if '"%s"' % m in raw or m in raw)


def api_state():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"available": False, "detail": "OPENAI_API_KEY 未设置"}
    return {"available": True, "detail": "OPENAI_API_KEY 已设置（%s…%s）"
            % (key[:6], key[-4:]) if len(key) > 12 else "OPENAI_API_KEY 已设置"}


def probe():
    cfg = load_config()
    cx, ap = codex_state(), api_state()
    cx["models"] = codex_models() if cx["available"] else []
    return {
        "codex": cx,
        "api": ap,
        "text_models": TEXT_MODELS,
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
    ap.add_argument("--json", action="store_true", help="--probe 输出 JSON")
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
            if args.set_model not in TEXT_MODELS:
                print("警告：%s 不在已知模型列表 %s 中，仍然写入。"
                      % (args.set_model, list(TEXT_MODELS)), file=sys.stderr)
            cfg["model"] = args.set_model
        if args.set_image_model:
            cfg["image_model"] = args.set_image_model
        save_config(cfg)
        print("已写入 %s:\n%s" % (CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2)))
        return 0
    if args.show_config:
        print(json.dumps(load_config(), ensure_ascii=False, indent=2) or "{}")
        return 0
    if args.probe:
        st = probe()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print("订阅(codex): %s — %s" % ("可用" if st["codex"]["available"] else "不可用",
                                            st["codex"]["detail"]))
            if st["codex"]["models"]:
                print("  可用模型: %s" % ", ".join(st["codex"]["models"]))
            print("API Key:     %s — %s" % ("可用" if st["api"]["available"] else "不可用",
                                             st["api"]["detail"]))
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
            print("未检测到可用的 OpenAI 认证。\n" + SETUP_HINT, file=sys.stderr)
            return 2

    if path == "codex" and not cx["available"]:
        print("配置为订阅路径，但 Codex 不可用：%s\n%s" % (cx["detail"], SETUP_HINT),
              file=sys.stderr)
        return 2
    if path == "api" and not apst["available"]:
        print("配置为 API 路径，但 %s\n%s" % (apst["detail"], SETUP_HINT), file=sys.stderr)
        return 2

    model = args.model or cfg.get("model") or DEFAULT_TEXT_MODEL
    image_model = cfg.get("image_model") or DEFAULT_IMAGE_MODEL
    size = args.size or os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

    for img in (args.see or []):
        if not os.path.isfile(img):
            print("图片不存在：%s" % img, file=sys.stderr)
            return 1

    try:
        if args.gen:
            prompt = read_prompt(args, ap)
            if path == "codex":
                codex_gen_image(cx["exe"], prompt, args.gen, model, args.timeout)
                print("saved: %s\n[生图路径: ChatGPT 订阅 via Codex image_gen]" % args.gen)
            else:
                api_gen_image(prompt, args.gen, image_model, size, args.timeout)
                print("saved: %s\n[生图路径: OpenAI API，模型 %s，%s，按 token 计费]"
                      % (args.gen, image_model, size))
            return 0

        prompt = read_prompt(args, ap)
        if not prompt.strip():
            ap.error("prompt 为空")
        tag_imgs = ("，%d 张图" % len(args.see)) if args.see else ""
        if path == "codex":
            print(codex_ask(cx["exe"], prompt, model, args.see, args.timeout))
            print("\n[路径: ChatGPT 订阅 via codex exec，模型 %s%s]" % (model, tag_imgs),
                  file=sys.stderr)
        else:
            print(api_ask(prompt, model, args.see, args.timeout))
            print("\n[路径: OpenAI API（按 token 计费），模型 %s%s]" % (model, tag_imgs),
                  file=sys.stderr)
        return 0
    except Exception as e:
        print("调用失败：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

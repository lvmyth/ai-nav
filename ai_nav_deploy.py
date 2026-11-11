#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 导航站 · 基于 GitHub REST API 的同步/部署脚本
================================================
替代原本依赖 git 协议的 `git fetch/rebase` 与 `git push`
（在当前沙箱网络下 git 协议被限流、间歇返回 403，导致每日自动任务失败）。

改用 GitHub git-tree / contents API 完成：
  - pull : 把远程 main 的最新 index.html、images/logos/*.png 及脚本/状态文件
           同步到本地工作区（等价于 fetch+rebase 到最新）
  - push : 对比本地文件与远程 main 的 git blob sha，只把真正不同的文件
           （index.html + 新增/改动的 images/logos/*.png + 脚本）逐个 PUT 上去
           天然基于最新版、无改动则不推送、失败不阻塞。

特性：
- index.html 已 540KB+，Contents API 的 base64 响应约 748KB，在本沙箱常被网关
  中途截断（curl 18 partial file）。故 pull 优先走 GitHub Pages 直出原始字节，
  再用 git blob sha 与 main 的 tree 校验一致，绝不用 CDN 旧版覆盖本地。
- api() 每次 curl 都带 connect-timeout / max-time，最多尝试 5 个通道，
  且校验 HTTP 状态码与 JSON 有效性（网关 301/400 HTML 页不再被误判为成功）。
  避免某个 IP 挂起把整个每日任务拖死（曾出现 pull 卡 16 分钟无任何输出）。
- push 前 preflight 自检：关键结构、TOOLS 条目、id 唯一性、防条目倒退，
  本地文件损坏或旧镜像覆盖时坚决阻断，保护线上。
- 幂等可重复运行：pull 始终对齐远程；push 只推送差异。
- 失败不中断：逐个文件推送，最后汇报成功/失败数量。
- 认证从环境变量 GITHUB_TOKEN 或 /root/.github_token 读取，不硬编码进仓库。

用法：
    python3 ai_nav_deploy.py pull
    python3 ai_nav_deploy.py push
"""
import os
import sys
import json
import re
import base64
import hashlib
import subprocess
import tempfile
import time

REPO = "lvmyth/ai-nav"
BRANCH = "main"
# 注意：token 不在此文件硬编码（否则 GitHub secret scanning 会拒绝提交）。
# 改为运行时从环境变量 GITHUB_TOKEN 或 /root/.github_token 读取；
# 自动任务 prompt 会在调用前 export GITHUB_TOKEN（从文件或任务内置 PAT 注入）。
API_HOSTS = [
    "140.82.112.6", "20.205.243.165", "140.82.112.3", "140.82.112.4",
    "20.205.243.166", "20.205.243.167", "20.205.243.164", "192.30.255.112",
    "140.82.121.5", "140.82.121.6", "140.82.113.3", "140.82.113.4",
    "20.205.243.168", "140.82.114.3", "140.82.114.4", "20.205.243.169",
]

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

# 每次 pull 都强制覆盖对齐远程的核心文件
SYNC_FILES = ["index.html", "ai_nav_deploy.py", "fetch_missing_logos.py",
              "dedup_tools.py", "write_status.py", "status.json",
              "tag_taxonomy.json"]  # 词表真值：每日任务据此给新术语归类
LOGO_DIR = "images/logos"

# index.html 走 Pages 直出（原始字节、无需 token、不会被 base64 膨胀拖慢）
PAGES_INDEX = "https://lvmyth.github.io/ai-nav/"
PAGES_HOSTS = ["185.199.108.153", "185.199.109.153",
               "185.199.110.153", "185.199.111.153"]


def find_api_ip():
    for ip in API_HOSTS:
        try:
            code = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--resolve", f"api.github.com:443:{ip}", "https://api.github.com",
                 "-m", "10"],
                capture_output=True, text=True,
            ).stdout.strip()
        except Exception:
            continue
        if code == "200":
            return ip
    return None


def load_token():
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    for p in ["/root/.github_token", os.path.expanduser("~/.github_token")]:
        try:
            if os.path.exists(p):
                return open(p, encoding="utf-8").read().strip()
        except Exception:
            pass
    return None


API_IP = None  # 延迟选择：api() 内部优先系统 DNS 直连，失败再轮换 --resolve 候选 IP
LAST_API_ERRORS = []  # 最近一次 api() 各通道的失败原因，便于诊断自动化失败
TOKEN = load_token()
if not TOKEN:
    print("ERROR: 未找到 GitHub token（需设置环境变量 GITHUB_TOKEN 或存在 /root/.github_token）")
    sys.exit(2)


def api(method, path, data=None, timeout=25):
    """执行一次 GitHub API 调用。

    连接策略：优先系统 DNS 直连 api.github.com；若失败，自动轮换
    --resolve 候选 IP（应对沙箱 DNS 不通 / GitHub IP 漂移）。
    每次尝试都带 connect-timeout / max-time，最多 5 个通道，
    并校验 HTTP 状态码与 JSON 有效性：
      网关会把 api.github.com 301 到 github.com 网页、或返回 "Bad request" HTML 页，
      这些响应 curl 退出码仍为 0，若不校验就会被误判成成功、不再重试。
    返回 (returncode, stdout)，成功且 stdout 为合法 JSON 时 returncode=0。
    """
    global API_IP
    tmp = None
    if data is not None:
        tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, tf)
        tf.close()
        tmp = tf.name
    try:
        def one_try(ip):
            cmd = ["curl", "-sS", "-X", method,
                   "--connect-timeout", "8", "-m", str(timeout),
                   "-w", "\n__AINAV_HTTP__%{http_code}",
                   "-H", f"Authorization: Bearer {TOKEN}",
                   "-H", "Accept: application/vnd.github+json"]
            if ip:
                cmd += ["--resolve", f"api.github.com:443:{ip}"]
            if tmp:
                cmd += ["-H", "Content-Type: application/json", "-d", f"@{tmp}"]
            cmd.append("https://api.github.com" + path)
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return False, (r.stderr or "curl 退出码 %s" % r.returncode).strip()[:160]
            body, sep, code = r.stdout.rpartition("\n__AINAV_HTTP__")
            code = code.strip()
            if not sep or not code.startswith("2"):
                return False, "HTTP %s" % (code or "?")
            if not body.strip():
                return False, "HTTP %s 但响应体为空" % code
            try:
                json.loads(body)
            except Exception:
                return False, "HTTP %s 但响应非 JSON（网关拦截页）" % code
            return True, body
        # 连接顺序：直连 → 当前已知 IP → 其余候选（最多 5 个通道，防止无限累积）
        order = [None]
        if API_IP:
            order.append(API_IP)
        for ip in API_HOSTS:
            if ip not in order and len(order) < 5:
                order.append(ip)
        reasons = []
        for ip in order:
            ok, out = one_try(ip)
            if ok:
                if ip:
                    API_IP = ip
                return 0, out
            reasons.append("%s -> %s" % (ip or "直连", out))
        LAST_API_ERRORS[:] = reasons
        return 1, ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def get_tree():
    """返回远程 main 的完整文件树 path -> git blob sha。

    tree 响应约 112KB，沙箱到 GitHub 偶发拥塞时 25s 会收到 half body；
    这里放宽单次超时并自动重试（api() 每轮还会轮换候选 IP），
    避免把一次网络抖动变成整天的自动化失败。
    """
    last = "未知"
    for attempt in range(3):
        rc, out = api("GET", f"/repos/{REPO}/git/trees/{BRANCH}?recursive=1",
                      timeout=60)
        if rc == 0 and out:
            try:
                j = json.loads(out)
                return {t["path"]: t["sha"] for t in j.get("tree", [])
                        if t.get("type") == "blob"}
            except Exception:
                last = "解析远程 git tree 失败"
        else:
            last = "; ".join(LAST_API_ERRORS[:3])
        print(f"[get_tree] 第 {attempt+1} 次未成功，重试中：{last[:140]}")
    raise RuntimeError("获取远程 git tree 失败（已重试 3 次）：" + last)


def remote_file(path, timeout=25):
    rc, out = api("GET", f"/repos/{REPO}/contents/{path}?ref={BRANCH}",
                  timeout=timeout)
    if rc != 0 or not out:
        return None
    try:
        j = json.loads(out)
        content = j.get("content", "")
        if not content or j.get("encoding") != "base64":
            # 超大文件 / 网关异常时 content 可能为空，宁可跳过也绝不写 0 字节文件
            return None
        return base64.b64decode(content) or None
    except Exception:
        return None


def blob_sha(data):
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode() + b"\x00" + data)
    return h.hexdigest()


def fetch_pages_index(timeout=90):
    """从 GitHub Pages 拉取 index.html 原始字节（无需 token，避开 API 大响应截断）。

    只有内容同时含 `const TOOLS` 与 `</html>` 才视为有效，防止拿到 404/网关页。
    返回 bytes 或 None。
    """
    for host in [None] + PAGES_HOSTS:
        cmd = ["curl", "-sS", "-L", "--connect-timeout", "8",
               "-m", str(timeout)]
        if host:
            cmd += ["--resolve", f"lvmyth.github.io:443:{host}"]
        cmd.append(PAGES_INDEX)
        try:
            r = subprocess.run(cmd, capture_output=True)
        except Exception:
            continue
        data = r.stdout
        if r.returncode == 0 and data and b"const TOOLS" in data and b"</html>" in data:
            return data
    return None


def tool_count_of(html):
    """从 index.html 文本中解析 TOOLS 条目数（严格匹配数组块）。"""
    m = re.search(r"const TOOLS\s*=\s*\[(.*?)\n\];", html, re.S)
    block = m.group(1) if m else ""
    return len(re.findall(r"\{\s*id:'([^']+)'", block))


def preflight(path="index.html"):
    """push 前纯文本自检，零第三方依赖，绝不成为新的失败源。

    返回 (ok, problems, warnings, local_count)：
      - problems 为硬性错误，任一存在即阻断推送（保护线上）
      - warnings 仅打印提示，不阻断（避免字符串内括号 / 远程重构误杀）
    """
    problems, warnings = [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        return False, [f"无法读取 {path}: {e}"], [], 0

    # 1) 结构性关键标记（缺失 = 文件被截断/改坏，必须阻断）
    for k in ["const TOOLS", "FALLBACK_DATA", "NAV_UPDATE_DATE",
              "footUpdateDate", "</html>"]:
        if k not in html:
            problems.append(f"关键标记缺失: {k}")

    # 2) 关键函数（仅告警，远程重构后不应误杀推送）
    for k in ["renderTools", "applyTableFilters", "toggleFav"]:
        if k not in html:
            warnings.append(f"未找到 {k}（若为重构可忽略）")

    # 3) JS 括号粗平衡（字符串内括号会干扰计数，仅告警）
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    for op, cl, name in [("{", "}", "花括号"), ("(", ")", "圆括号"),
                         ("[", "]", "方括号")]:
        d = js.count(op) - js.count(cl)
        if d != 0:
            warnings.append(f"{name}粗平衡差值 {d}（字符串内括号会干扰，仅供参考）")

    # 4) TOOLS 条目与 id 唯一性
    m = re.search(r"const TOOLS\s*=\s*\[(.*?)\n\];", html, re.S)
    block = m.group(1) if m else ""
    ids = re.findall(r"\{\s*id:'([^']+)'", block)
    local_count = len(ids)
    if not ids:
        problems.append("未能解析出任何 TOOLS 条目（数组格式可能被破坏）")
    dup = len(ids) - len(set(ids))
    if dup:
        problems.append(f"存在 {dup} 个重复 id")

    # 5) 术语结构化字段（domain / topics）完整性
    #    逐条检查而非只看总数，避免「新增术语漏填」被高覆盖率掩盖。
    #    单条格式差异仅告警；大面积缺失才硬阻断，防止误杀每日任务。
    gl_lines = [l for l in html.splitlines() if re.search(r"\{\s*id:'gl-", l)]
    if gl_lines:
        lack = [l for l in gl_lines if "topics:[" not in l or "domain:'" not in l]
        if lack:
            msg = f"{len(lack)}/{len(gl_lines)} 条术语缺少 domain/topics"
            if len(lack) > max(3, int(len(gl_lines) * 0.2)):
                problems.append(msg + "（超过 20%，疑似术语块被重写，禁止推送）")
            else:
                warnings.append(msg + "（少量缺失，建议补齐，不阻断）")
                for l in lack[:3]:
                    mid = re.search(r"id:'([^']+)'", l)
                    warnings.append("  缺字段示例: " + (mid.group(1) if mid else l[:40]))

    return (len(problems) == 0), problems, warnings, local_count


def remote_tool_count():
    """读取远程 index.html 的 TOOLS 条数用于防倒退；网络异常返回 None（不阻断）。

    优先 Pages 通道（快、稳）；Pages 可能比 main 略滞后，但防倒退只在
    「本地明显少于远程」时触发，滞后只会让检查更宽松，不会误杀推送。
    """
    try:
        data = fetch_pages_index(timeout=60) or remote_file("index.html", timeout=120)
        if not data:
            return None
        return tool_count_of(data.decode("utf-8", "ignore")) or None
    except Exception:
        return None


def cmd_pull():
    """把远程 main 最新版本同步到本地工作区。"""
    tree = get_tree()
    target_sha = tree.get("index.html")

    # 1) index.html：优先 Pages 通道，并用 git blob sha 校验与 main 完全一致。
    #    sha 不一致说明 Pages CDN 尚未构建到最新，此时绝不拿旧版覆盖本地。
    pages = fetch_pages_index()
    if pages is not None and target_sha and blob_sha(pages) == target_sha:
        with open("index.html", "wb") as fh:
            fh.write(pages)
        print("[pull] index.html ← GitHub Pages（blob sha 与 main 一致）✅")
    else:
        data = remote_file("index.html", timeout=150)
        if data is not None and (not target_sha or blob_sha(data) == target_sha):
            with open("index.html", "wb") as fh:
                fh.write(data)
            print("[pull] index.html ← Contents API ✅")
        else:
            print("[pull] ⚠️ 无法取得与 main 一致的 index.html，保留本地版本（不覆盖）")

    # 2) 其余脚本 / 状态文件（小文件，走 Contents API）
    for f in SYNC_FILES:
        if f == "index.html":
            continue
        if f in tree:
            data = remote_file(f)
            if data is not None:
                with open(f, "wb") as fh:
                    fh.write(data)

    # 3) 增量补全 logo png（本地缺失才下载，且带总时限，绝不拖死任务）
    deadline = time.time() + 150
    n_logo = n_skip = 0
    for p in tree:
        if p.startswith(LOGO_DIR + "/") and not os.path.exists(p):
            if time.time() > deadline:
                n_skip += 1
                continue
            data = remote_file(p)
            if data is not None:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as fh:
                    fh.write(data)
                n_logo += 1
            else:
                n_skip += 1
    print(f"[pull] 已用远程 main 最新版本同步本地 ✅ "
          f"(补全缺失 logo {n_logo} 个, 跳过 {n_skip} 个)")
    return 0


def put_file(local_path, message, remote_sha, retries=3):
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    data = {"message": message, "content": b64, "branch": BRANCH}
    if remote_sha:
        data["sha"] = remote_sha
    last = ""
    for i in range(retries):
        rc, out = api("PUT", f"/repos/{REPO}/contents/{local_path}", data,
                      timeout=90)
        if rc == 0 and '"sha"' in out:
            return True, out[:200]
        last = out[:200]
        if i < retries - 1:
            time.sleep(2)
    return False, last


def cmd_push():
    """对比本地与远程，只推送真正不同的文件。"""
    # 0) 推送前自检：本地文件损坏或条目倒退时坚决阻断，保护线上
    ok_pf, problems, warnings, local_count = preflight()
    for w in warnings:
        print(f"[preflight] ⚠️ {w}")
    if not ok_pf:
        print("[preflight] ❌ 自检未通过，已终止推送以保护线上：")
        for p in problems:
            print(f"   - {p}")
        return 1
    remote_count = remote_tool_count()
    if remote_count is not None and local_count < remote_count:
        print(f"[preflight] ❌ 拒绝推送：本地 {local_count} 条 < 远程 {remote_count} 条，"
              f"疑似旧镜像覆盖，请先执行 pull 对齐")
        return 1
    print(f"[preflight] ✅ 通过（本地 {local_count} 条 / 远程 "
          f"{remote_count if remote_count is not None else '未知'} 条）")

    tree = get_tree()
    # 收集本地候选文件
    candidates = []
    for f in SYNC_FILES:
        if os.path.exists(f):
            candidates.append(f)
    if os.path.isdir(LOGO_DIR):
        for fn in sorted(os.listdir(LOGO_DIR)):
            if fn.lower().endswith(".png"):
                candidates.append(os.path.join(LOGO_DIR, fn))
    # 计算差异（git blob sha 对比，不依赖 git 工作区状态）
    changed = []
    for f in candidates:
        data = open(f, "rb").read()
        if blob_sha(data) != tree.get(f):
            changed.append(f)
    if not changed:
        print("[push] 本地与远程一致，无改动，跳过 ✅")
        return 0
    # 排序: logo png -> index.html -> 脚本（保证引用先就位）
    def sortkey(f):
        if f.startswith(LOGO_DIR + "/"):
            return (0, f)
        if f == "index.html":
            return (1, f)
        return (2, f)
    changed.sort(key=sortkey)
    ok = fail = 0
    print(f"[push] 检测到 {len(changed)} 个文件与远程不同，开始推送：")
    for f in changed:
        s, o = put_file(f, f"每日更新：{f}", tree.get(f))
        if s:
            ok += 1
            print(f"  ✅ {f}")
        else:
            fail += 1
            print(f"  ❌ {f}: {o}")
    print(f"\n[push] 成功 {ok} / 失败 {fail} / 总计 {len(changed)}")
    return 0 if fail == 0 else 1


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "push"):
        print("用法: python3 ai_nav_deploy.py [pull|push]")
        return 1
    try:
        return cmd_pull() if sys.argv[1] == "pull" else cmd_push()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

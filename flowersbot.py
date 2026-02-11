import os
import logging
import asyncio
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from typing import Set, Optional, Dict, List, Tuple
from threading import Thread

# Web 框架
from flask import Flask, render_template_string, request, redirect, url_for

# Telegram 相關模組
from telegram import Update, MessageEntity, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError, BadRequest

# 第三方分析庫
import hanzidentifier
import tldextract

# --- 1. 系統日誌與時區設定 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TW_TZ = timezone(timedelta(hours=8))

def get_now_tw():
    """ 取得目前的台灣時間 """
    return datetime.now(timezone.utc).astimezone(TW_TZ)

# --- 2. 雲端資料庫管理 ---
class FirestoreManager:
    def __init__(self):
        try:
            raw_config = os.getenv("__firebase_config", "{}")
            self.config = json.loads(raw_config) if raw_config.strip() else {}
        except Exception as e:
            logger.error(f"Firebase 設定解析失敗: {e}")
            self.config = {}
            
        self.app_id = os.getenv("__app_id", "flowers-bot-default")
        self.project_id = self.config.get("projectId")
        self.api_key = self.config.get("apiKey")
        
        if self.project_id:
            self.base_url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/artifacts/{self.app_id}/public/data"
        else:
            self.base_url = None
        self.id_token = None

    def _authenticate(self):
        if not self.api_key or not self.project_id: return False
        try:
            url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={self.api_key}"
            resp = requests.post(url, json={"returnSecureToken": True}, timeout=10)
            data = resp.json()
            self.id_token = data.get("idToken")
            return True if self.id_token else False
        except Exception as e:
            logger.error(f"雲端驗證失敗: {e}")
            return False

    def save_data(self, collection: str, doc_id: str, data: dict):
        if not self.base_url or (not self.id_token and not self._authenticate()): return
        try:
            url = f"{self.base_url}/{collection}/{doc_id}"
            fields = {k: {"stringValue": str(v)} for k, v in data.items()}
            requests.patch(url, params={"updateMask.fieldPaths": list(data.keys())}, json={"fields": fields}, headers={"Authorization": f"Bearer {self.id_token}"}, timeout=10)
        except: pass

    def delete_data(self, collection: str, doc_id: str):
        if not self.base_url or (not self.id_token and not self._authenticate()): return
        try:
            url = f"{self.base_url}/{collection}/{doc_id}"
            requests.delete(url, headers={"Authorization": f"Bearer {self.id_token}"}, timeout=10)
        except: pass

    def load_all(self, collection: str) -> List[dict]:
        if not self.base_url or (not self.id_token and not self._authenticate()): return []
        try:
            url = f"{self.base_url}/{collection}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self.id_token}"}, timeout=10)
            if resp.status_code != 200: return []
            docs = resp.json().get("documents", [])
            result = []
            for d in docs:
                fields = d.get("fields", {})
                item = {k: v.get("stringValue") for k, v in fields.items()}
                if "uid" in item: item["uid"] = int(item["uid"])
                if "chat_id" in item: item["chat_id"] = int(item["chat_id"])
                result.append(item)
            return result
        except: return []

# --- 3. 全域配置與狀態儲存 ---
class BotConfig:
    def __init__(self):
        self.bot_token = os.getenv("TG_BOT_TOKEN")
        self.application = None 
        self.loop = None        
        self.db = FirestoreManager()
        
        self.warning_duration = 5
        self.max_violations = 3
        
        # 網域白名單
        self.allowed_domains = {
            "google.com", "wikipedia.org", "telegram.org", "t.me", 
            "facebook.com", "github.com", "blogspot.com", "line.me", 
            "portaly.cc", "ttt3388.com.tw", "webnode.tw", "ecup78.com", "jktank.net",
            "youtube.com", "youtu.be"
        }

        # Telegram ID 白名單
        self.telegram_link_whitelist = {
            "ecup78", "ttt3388", "setlanguage", "ecup788_lulu156", 
            "ecup788_hhaa555", "lulu156_ecup788", "flower_5555", 
            "ecup78_1", "ii17225278", "sexy_ttt3388", "line527817ii", 
            "tmdgan2_0", "ttt3388sex", "ii1722", "taiwan",
            "sanchong168", "xinzhuang168", "taishanwugu168", 
            "zhonghe168", "tucheng_168", "linkou168", "keelung168"
        }

        # 貼圖 ID 白名單
        self.sticker_whitelist = {"ecup78_bot", "ecup78"}

        # 電話前綴黑名單 (已整合您提供的清單)
        self.blocked_phone_prefixes = {
            "+91", "+86", "+95", "+852", "+60", "+84", "+63", "+1", "+62", "+41", "+44", "+855", "+66"
        }

        # 關鍵字黑名單 (已整合您提供的清單)
        self.blocked_keywords = {
            # 詐騙/博弈
            "假钞", "捡钱", "项目", "電報", "@xsm77788", "君临",
            "挣米", "日赚", "回款", "上压", "担保", "兼职", "手气",
            "风口", "一单", "博彩", "彩票", "赛车", "飞艇", "哈希",
            "百家乐", "投资", "USDT", "TRX", "包过", "洗米", "跑分",
            "现场", "连连", "满", 
            # 個資/黑產
            "查档", "身份证", "户籍", "开房", "定位", "手机号", "机主", 
            "轨迹", "车队", "入款", "出款",
            # 色情/引流 (針對截圖強化)
            "迷药", "春药", "裸聊", "极品", "强奸", "销魂", 
            "约炮", "同城", "资源", "人兽", "皮肤", "萌酱",
            "萝莉", "爆炒", "做坏事", "蜜桃臀", "路边", "坏事", 
            # 簡體高頻詞
            "置顶", "软件", "下载", "点击", "链接", "免费观看", "点击下方"
        }

        # 絕對簡體字表 (已整合您提供的清單)
        self.strict_simplified_chars = {
            "国", "会", "发", "现", "关", "质", "员", "机", "产", "气", 
            "实", "则", "两", "结", "营", "报", "种", "专", "务", "战",
            "风", "让", "钱", "变", "间", "给", "号", "图", "亲", "极",
            "点", "击", "库", "车", "东", "应", "库", "启", "书", "评",
            "无", "马", "过", "办", "证", "听", "说", "话", "频", "视",
            "户", "罗", "边", "观", "么", "开", "区", "帅", "费"
        }
        
        self.violation_tracker: Dict[Tuple[int, int], Dict] = {}
        self.blacklist_members: Dict[str, Dict] = {}
        self.total_deleted_count = 0
        self.logs: List[Dict] = []
        self.last_heartbeat: Optional[datetime] = None
        self.flagged_media_groups: Dict[str, datetime] = {}

    def sync_from_cloud(self):
        try:
            cloud_blacklist = self.db.load_all("blacklist")
            if not cloud_blacklist:
                self.add_log("INFO", "🦋 雲端黑名單目前為空")
                return
            for item in cloud_blacklist:
                uid, chat_id = item.get("uid"), item.get("chat_id")
                if uid and chat_id:
                    key = f"{chat_id}_{uid}"
                    time_val = item.get("time")
                    try:
                        dt = datetime.fromisoformat(time_val) if time_val else get_now_tw()
                    except:
                        dt = get_now_tw()
                    self.blacklist_members[key] = {
                        "uid": uid, "name": item.get("name", "未知用戶"), "chat_id": chat_id,
                        "chat_title": item.get("chat_title", "未知群組"), "time": dt
                    }
            self.add_log("INFO", f"🦋 同步完成，載入 {len(self.blacklist_members)} 筆黑名單")
        except Exception as e:
            self.add_log("ERROR", f"🦋 同步雲端資料失敗: {e}")

    def add_log(self, level: str, message: str):
        now = get_now_tw().strftime("%H:%M:%S")
        self.logs.insert(0, {"time": now, "level": level, "content": message})
        self.logs = self.logs[:30]
        logger.info(f"[{level}] {message}")

    def add_violation(self, chat_id: int, user_id: int) -> int:
        today = get_now_tw().date()
        key = (chat_id, user_id)
        if key not in self.violation_tracker or self.violation_tracker[key]["last_date"] != today:
            self.violation_tracker[key] = {"count": 1, "last_date": today}
        else:
            self.violation_tracker[key]["count"] += 1
        return self.violation_tracker[key]["count"]

    def record_blacklist(self, user_id: int, name: str, chat_id: int, chat_title: str):
        now = get_now_tw()
        key = f"{chat_id}_{user_id}"
        data = {"uid": user_id, "name": name, "chat_id": chat_id, "chat_title": chat_title, "time": now.isoformat()}
        self.blacklist_members[key] = {"uid": user_id, "name": name, "chat_id": chat_id, "chat_title": chat_title, "time": now}
        Thread(target=self.db.save_data, args=("blacklist", key, data), daemon=True).start()

    def reset_violation(self, chat_id: int, user_id: int):
        v_key, bl_key = (chat_id, user_id), f"{chat_id}_{user_id}"
        if v_key in self.violation_tracker: self.violation_tracker[v_key]["count"] = 0
        if bl_key in self.blacklist_members: 
            del self.blacklist_members[bl_key]
            Thread(target=self.db.delete_data, args=("blacklist", bl_key), daemon=True).start()

    def get_recent_blacklist(self, filter_chat_id: Optional[int] = None) -> List[Dict]:
        now = get_now_tw()
        recent = []
        for key, info in self.blacklist_members.items():
            try:
                if (now - info["time"]).total_seconds() < 86400: 
                    if filter_chat_id is None or info["chat_id"] == filter_chat_id:
                        recent.append(info)
            except: continue
        return sorted(recent, key=lambda x: x["time"], reverse=True)

    def get_blacklist_chats(self) -> Dict[int, str]:
        return {info["chat_id"]: info["chat_title"] for info in self.blacklist_members.values()}

config = BotConfig()

# --- 4. 偵測與處理邏輯 ---
def is_domain_allowed(url: str) -> bool:
    try:
        extracted = tldextract.extract(url.strip().lower())
        return extracted.registered_domain in config.allowed_domains
    except: return False

def contains_prohibited_content(text: str) -> Tuple[bool, Optional[str]]:
    if not text: return False, None
    for kw in config.blocked_keywords:
        if kw in text: return True, f"關鍵字: {kw}"
    try:
        if hanzidentifier.has_chinese(text):
            for char in text:
                if char in config.strict_simplified_chars: return True, f"禁語: {char}"
                if hanzidentifier.is_simplified(char) and not hanzidentifier.is_traditional(char):
                    return True, f"簡體: {char}"
    except: pass
    return False, None

async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, admin_sender = update.effective_chat, update.effective_user
    try:
        member = await chat.get_member(admin_sender.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: return
        
        user_id = None
        mention = "未知用戶"
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            user_id = target_user.id
            mention = target_user.mention_html()
        elif context.args:
            try: 
                user_id = int(context.args[0])
                mention = f'<a href="tg://user?id={user_id}">學員 {user_id}</a>'
            except: pass
            
        if user_id:
            p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
            await context.bot.restrict_chat_member(chat.id, user_id, p)
            config.reset_violation(chat.id, user_id)
            config.add_log("SUCCESS", f"🦋 管理員在 [{chat.title}] 指令解封 {user_id}")
            msg = await update.message.reply_text(
                text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>",
                parse_mode=ParseMode.HTML
            )
            # 指令解封保留訊息
    except Exception as e: await update.message.reply_text(f"❌ 錯誤: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config.last_heartbeat = get_now_tw()
    if not update.message: return
    msg = update.message
    user, chat = msg.from_user, msg.chat
    if not user or user.is_bot: return

    # 管理員豁免
    try:
        if chat.type != "private":
            cm = await chat.get_member(user.id)
            if cm.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: return 
    except: pass

    all_texts: List[str] = []
    violation_reason: Optional[str] = None
    if msg.text: all_texts.append(msg.text)
    if msg.caption: all_texts.append(msg.caption)
    
    # 轉傳來源深度檢查 (標題、人名)
    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat:
            src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user:
            src_name = msg.forward_origin.sender_user.full_name
        if src_name:
            all_texts.append(src_name) 
            is_bad_src, src_reason = contains_prohibited_content(src_name)
            if is_bad_src:
                violation_reason = f"轉傳來源違規 ({src_name})"

    # 按鈕與投票內容提取
    if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if hasattr(btn, 'text'): all_texts.append(btn.text)
    if msg.poll:
        all_texts.append(msg.poll.question)
        for opt in msg.poll.options: all_texts.append(opt.text)

    # 引用 (Reply/Quote) 內容提取
    quote = getattr(msg, 'quote', None)
    if quote and hasattr(quote, 'text') and quote.text:
        all_texts.append(quote.text)

    # 聯絡人/電話/姓名偵測 (強化版)
    if not violation_reason and msg.contact:
        phone = msg.contact.phone_number or ""
        clean_phone = re.sub(r'[+\-\s]', '', phone)
        blocked_clean = [re.sub(r'[+\-\s]', '', p) for p in config.blocked_phone_prefixes]
        
        if any(clean_phone.startswith(pre) for pre in blocked_clean if pre):
            violation_reason = f"來自受限國家門號 ({phone[:3]}...)"
        if msg.contact.first_name: all_texts.append(msg.contact.first_name)
        if msg.contact.last_name: all_texts.append(msg.contact.last_name)
    
    # 地點偵測 (地址/標題)
    if not violation_reason and msg.venue:
        if msg.venue.title: all_texts.append(msg.venue.title)
        if msg.venue.address: all_texts.append(msg.venue.address)

    # 貼圖偵測 (大小寫校正)
    if not violation_reason and msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            combined_lower = (s_set.title + msg.sticker.set_name).lower()
            if ("@" in combined_lower or "_by_" in combined_lower):
                if not any(wd in combined_lower for wd in config.sticker_whitelist):
                    safe_title = s_set.title.replace("@", "")
                    violation_reason = f"未授權 ID ({safe_title})"
            else: all_texts.append(s_set.title)
        except: pass

    # 綜合文本偵測 (關鍵字 + 簡體字) - 將所有抓到的字串合併掃描
    if not violation_reason:
        # 移除重複並合併，避免 Log 過長
        unique_texts = list(set(all_texts))
        for t in unique_texts:
            is_bad, r = contains_prohibited_content(t)
            if is_bad: violation_reason = r; break

    # 連結檢查
    if not violation_reason:
        ents = list(msg.entities or []) + list(msg.caption_entities or [])
        for ent in ents:
            if ent.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                u = ent.url if ent.type == MessageEntity.TEXT_LINK else (msg.text or msg.caption)[ent.offset : ent.offset+ent.length]
                u_clean = u.strip().lower()
                if not is_domain_allowed(u_clean):
                    violation_reason = "含有不明連結"; break
                if "t.me/" in u_clean:
                    path = u_clean.split('t.me/')[-1].split('/')[0].split('?')[0].replace("@", "")
                    if path and not any(wl in path for wl in config.telegram_link_whitelist):
                        violation_reason = f"未授權 TG 連結 ({path})"; break

    if violation_reason:
        if msg.media_group_id: config.flagged_media_groups[msg.media_group_id] = datetime.now()
        try:
            try: await msg.delete(); config.total_deleted_count += 1
            except: pass
            
            # 使用 user.id 作為違規主體
            v_count = config.add_violation(chat.id, user.id)
            
            if v_count >= config.max_violations:
                try: await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(can_send_messages=False))
                except: config.add_log("WARN", f"[{chat.title}] 技術禁言失敗")
                
                config.record_blacklist(user.id, user.full_name, chat.id, chat.title)
                config.add_log("ERROR", f"🦋 學員 {user.full_name} 在 [{chat.title}] 違規達上限，封鎖入阿茲卡班")
                await context.bot.send_message(
                    chat_id=chat.id, 
                    text=f"🦋 <b>霍格華茲禁言通知</b> 🦋\n\n🦉用戶學員：{user.mention_html()}\n🈲發言已多次違反校規。\n🈲已被咒語《阿哇呾喀呾啦》擊殺⚡️\n🪄<b>如被誤殺請待在阿茲卡班內稍等\n並請客服通知鄧不利多校長幫你解禁</b>", 
                    parse_mode=ParseMode.HTML
                )
            else:
                sent_warn = await context.bot.send_message(chat.id, f"🦋 <b>霍格華茲警告通知</b> 🦋\n\n🦉用戶學員：{user.mention_html()}\n⚠️違反校規：{violation_reason}\n⚠️違規計次：({v_count}/{config.max_violations})\n🪄<b>多次違規將被黑魔法教授擊殺</b>", parse_mode=ParseMode.HTML)
                await asyncio.sleep(config.warning_duration); await sent_warn.delete()
        except: pass
    elif msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete()
        except: pass
    elif not msg.sticker:
        # 更新日誌顯示：顯示所有抓取到的文字，方便 Debug
        full_content_log = " | ".join(all_texts)
        config.add_log("INFO", f"接收自[{chat.title}] - [{user.first_name}] 全文掃描: {full_content_log[:30]}...")

# --- 5. Flask 後台管理網頁 ---
app = Flask(__name__)

@app.route('/')
def index():
    is_active = config.application is not None
    filter_cid = request.args.get('filter_chat_id', type=int)
    members = config.get_recent_blacklist(filter_cid)
    filter_chats = config.get_blacklist_chats()
    return render_template_string(DASHBOARD_HTML, config=config, is_active=is_active, members=members, filter_chats=filter_chats, active_filter=filter_cid)

@app.route('/update', methods=['POST'])
def update():
    try:
        config.warning_duration = int(request.form.get('duration', 5))
        config.max_violations = int(request.form.get('max_v', 6))
        config.allowed_domains = {d.strip().lower() for d in request.form.get('domains', '').split(',') if d.strip()}
        config.telegram_link_whitelist = {t.strip().lower().replace("@", "") for t in request.form.get('tg_links', '').split(',') if t.strip()}
        config.blocked_phone_prefixes = {p.strip() for p in request.form.get('phone_pre', '').split(',') if p.strip()}
        config.blocked_keywords = {k.strip() for k in request.form.get('keywords', '').split(',') if k.strip()}
        config.sticker_whitelist = {s.strip().lower().replace("@", "") for s in request.form.get('sticker_ws', '').split(',') if s.strip()}
        config.add_log("SUCCESS", "🦋 所有校規與過濾設定已同步更新")
    except Exception as e: config.add_log("ERROR", f"🦋 更新失敗: {e}")
    return redirect(url_for('index'))

@app.route('/unban_member', methods=['POST'])
def unban_member():
    try:
        user_id, chat_id = int(request.form.get('user_id')), int(request.form.get('chat_id'))
        key = f"{chat_id}_{user_id}"
        member_data = config.blacklist_members.get(key, {})
        user_name = member_data.get("name", f"學員 {user_id}")
        mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
        async def do_unban():
            try:
                p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
                await config.application.bot.restrict_chat_member(chat.id, user_id, p); await config.application.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                config.reset_violation(chat_id, user_id)
                config.add_log("SUCCESS", f"🦋 網頁解封 {user_name}，地點 [{member_data.get('chat_title')}]")
                n_msg = await config.application.bot.send_message(
                    chat_id=chat_id, 
                    text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>", 
                    parse_mode=ParseMode.HTML
                )
                # 不刪除
            except Exception as e: config.add_log("ERROR", f"🦋 解封失敗: {e}")
        if config.loop: asyncio.run_coroutine_threadsafe(do_unban(), config.loop)
    except: pass
    return redirect(url_for('index'))

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
    <meta charset="UTF-8"><title>花家霍格華茲·石內卜教授🦋管理後台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>.terminal { background-color: #0f172a; height: 350px; overflow-y: auto; font-size: 11px; }</style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans p-6">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center border-b border-slate-700 pb-4 mb-6">
            <h1 class="text-3xl font-bold text-sky-400">花家霍格華茲·石內卜教授🦋管理後台</h1>
            <span class="px-3 py-1 rounded-full text-xs {{ 'bg-emerald-500/20 text-emerald-400' if is_active else 'bg-rose-500/20 text-rose-400' }}">
                {{ '● 機器人運行中' if is_active else '● 機器人未啟動' }}
            </span>
        </header>
        <div class="grid grid-cols-2 gap-4 mb-6">
            <div class="bg-slate-800 p-4 rounded-2xl border border-slate-700 shadow-lg text-center">
                <p class="text-slate-400 text-xs">今日攔截總數</p><h2 class="text-4xl font-black">{{ config.total_deleted_count }}</h2>
            </div>
            <div class="bg-slate-800 p-4 rounded-2xl border border-slate-700 shadow-lg text-center">
                <p class="text-slate-400 text-xs">雲端黑名單筆數</p><h2 class="text-4xl font-black text-rose-500">{{ members | length }}</h2>
            </div>
        </div>
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div class="lg:col-span-4 space-y-6">
                <div class="bg-slate-800 p-6 rounded-2xl border border-slate-700 shadow-xl">
                    <h3 class="text-lg font-bold mb-4 text-sky-300">🦉 霍格華茲校規</h3>
                    <form action="/update" method="POST" class="space-y-4">
                        <div class="grid grid-cols-2 gap-4">
                            <div><label class="block text-[10px] text-slate-400">警告停留(秒)</label><input type="number" name="duration" value="{{ config.warning_duration }}" class="w-full bg-slate-700 rounded p-1 text-sm text-white outline-none"></div>
                            <div><label class="block text-[10px] text-slate-400">違規上限(次)</label><input type="number" name="max_v" value="{{ config.max_violations }}" class="w-full bg-slate-700 rounded p-1 text-sm text-white outline-none"></div>
                        </div>
                        <div><label class="block text-[10px] text-slate-400 text-rose-400">黑名單關鍵字 (含簡體字)</label><textarea name="keywords" rows="2" class="w-full bg-slate-700 rounded p-1 text-[10px] text-white outline-none">{{ config.blocked_keywords | join(', ') }}</textarea></div>
                        <div><label class="block text-[10px] text-slate-400 text-rose-400">電話開頭黑名單 (+號開頭)</label><textarea name="phone_pre" rows="1" class="w-full bg-slate-700 rounded p-1 text-[10px] text-white outline-none">{{ config.blocked_phone_prefixes | join(', ') }}</textarea></div>
                        <div><label class="block text-[10px] text-slate-400">網域白名單</label><textarea name="domains" rows="1" class="w-full bg-slate-700 rounded p-1 text-[10px] text-white outline-none">{{ config.allowed_domains | join(', ') }}</textarea></div>
                        <div><label class="block text-[10px] text-slate-400">TG ID 白名單</label><textarea name="tg_links" rows="2" class="w-full bg-slate-700 rounded p-1 text-[10px] text-white outline-none">{{ config.telegram_link_whitelist | join(', ') }}</textarea></div>
                        <div><label class="block text-[10px] text-slate-400 font-bold text-sky-400">貼圖白名單</label><textarea name="sticker_ws" rows="1" class="w-full bg-slate-700 rounded p-1 text-[10px] text-white outline-none">{{ config.sticker_whitelist | join(', ') }}</textarea></div>
                        <button type="submit" class="w-full bg-sky-600 hover:bg-sky-500 py-2 rounded-xl font-bold text-sm text-white transition-all">更新校規</button>
                    </form>
                </div>
            </div>
            <div class="lg:col-span-8 space-y-6">
                <div class="bg-slate-800 p-6 rounded-2xl border border-slate-700 shadow-xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-lg font-bold text-rose-400">🚫 阿茲卡班監獄紀錄</h3>
                        <button onclick="location.reload()" class="text-[10px] text-sky-400 border border-sky-400 px-2 py-0.5 rounded hover:bg-sky-400 hover:text-white transition-all font-bold">刷新名單</button>
                    </div>
                    <div class="flex flex-wrap gap-2 mb-4">
                        <a href="/" class="px-2 py-1 text-[10px] rounded {{ 'bg-sky-600 text-white' if not active_filter else 'bg-slate-700 text-slate-400' }}">全部</a>
                        {% for cid, ctitle in filter_chats.items() %}<a href="/?filter_chat_id={{ cid }}" class="px-2 py-1 text-[10px] rounded {{ 'bg-sky-600 text-white' if active_filter == cid else 'bg-slate-700 text-slate-400' }} text-ellipsis overflow-hidden">{{ ctitle }}</a>{% endfor %}
                    </div>
                    <div class="overflow-x-auto terminal"><table class="w-full text-left text-[11px]"><tbody class="divide-y divide-slate-700">
                        {% for m in members %}<tr>
                            <td class="py-2"><b>{{ m.name }}</b><br><span class="text-slate-500">{{ m.uid }}</span></td>
                            <td class="py-2"><span class="bg-slate-700 px-2 rounded">{{ m.chat_title }}</span></td>
                            <td class="py-2 text-slate-400">{{ m.time.strftime('%H:%M') }}</td>
                            <td class="py-2 text-right"><form action="/unban_member" method="POST"><input type="hidden" name="user_id" value="{{ m.uid }}"><input type="hidden" name="chat_id" value="{{ m.chat_id }}"><button type="submit" class="bg-emerald-600/20 text-emerald-400 border border-emerald-600/30 px-2 py-1 rounded hover:bg-emerald-600 hover:text-white transition-all">解封</button></form></td>
                        </tr>{% endfor %}
                    </tbody></table></div>
                </div>
                <div class="bg-slate-800 p-6 rounded-2xl border border-slate-700 shadow-xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-lg font-bold text-sky-300">📝 違規 Log 紀錄</h3>
                        <button onclick="location.reload()" class="text-[10px] text-sky-400 border border-sky-400 px-2 py-0.5 rounded hover:bg-sky-400 hover:text-white transition-all font-bold">刷新日誌</button>
                    </div>
                    <div class="terminal rounded p-2 shadow-inner">{% for log in config.logs %}<div><span class="text-slate-500">[{{ log.time }}]</span> <span class="text-{{ 'rose-400' if log.level=='ERROR' else 'sky-400' }}">[{{ log.level }}]</span> {{ log.content }}</div>{% endfor %}</div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

# --- 6. 啟動區塊 ---
def run_telegram_bot():
    if not config.bot_token: return
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); config.loop = loop 
    config.sync_from_cloud()
    try:
        bot_app = ApplicationBuilder().token(config.bot_token).build(); config.application = bot_app 
        async def clear(): 
            try: await bot_app.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            config.add_log("INFO", "🦋 Telegram 通訊連線成功，資料已恢復。")
        loop.run_until_complete(clear())
        bot_app.add_handler(CommandHandler("unban", unban_handler))
        bot_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
        bot_app.run_polling(stop_signals=False, close_loop=False)
    except Exception as e: config.add_log("ERROR", f"🦋 核心崩潰: {e}")

if __name__ == '__main__':
    tg_thread = Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

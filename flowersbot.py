import os
import logging
import asyncio
import json
import re
import requests
import base64
import uuid
import random
from datetime import datetime, timedelta, timezone
from typing import Set, Optional, Dict, List, Tuple
from threading import Thread, Lock

from flask import Flask, render_template_string, request, redirect, url_for, jsonify, Response
from waitress import serve

from telegram import Update, MessageEntity, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError, BadRequest

import hanzidentifier
import tldextract

# ==========================================
# 1. 系統日誌與時區設定
# ==========================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
TW_TZ = timezone(timedelta(hours=8))

def get_now_tw():
    return datetime.now(timezone.utc).astimezone(TW_TZ)

# ==========================================
# 2. 雲端 / 本地 資料持久化管理 (Firebase + JSON)
# ==========================================
class PersistenceManager:
    def __init__(self, filename="flowersbot_data.json"):
        self.filename = filename
        self.firebase_url = os.getenv("FIREBASE_DB_URL")
        if self.firebase_url and self.firebase_url.endswith('/'):
            self.firebase_url = self.firebase_url[:-1]

    def save(self, data: dict):
        try:
            serializable_data = self._serialize(data)
            if self.firebase_url:
                try: requests.put(f"{self.firebase_url}/bot_data.json", json=serializable_data, timeout=5)
                except Exception as e: logger.error(f"雲端儲存失敗: {e}")
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(serializable_data, f, ensure_ascii=False, indent=2)
        except Exception as e: logger.error(f"資料儲存失敗: {e}")

    def load(self) -> dict:
        data = None
        try:
            if self.firebase_url:
                try:
                    resp = requests.get(f"{self.firebase_url}/bot_data.json", timeout=5)
                    if resp.status_code == 200 and resp.json(): data = resp.json()
                except Exception as e: logger.error(f"雲端讀取失敗: {e}")
            if not data and os.path.exists(self.filename):
                with open(self.filename, 'r', encoding='utf-8') as f: data = json.load(f)
        except Exception as e: logger.error(f"資料讀取失敗: {e}")
        return self._deserialize(data) if data else {}

    def _serialize(self, data):
        if isinstance(data, dict): return {k: self._serialize(v) for k, v in data.items()}
        elif isinstance(data, list): return [self._serialize(v) for v in data]
        elif isinstance(data, datetime): return data.isoformat()
        return data

    def _deserialize(self, data):
        if isinstance(data, dict):
            new_dict = {}
            for k, v in data.items():
                if isinstance(v, str):
                    try:
                        if "T" in v and v.count("-") == 2 and v.count(":") >= 2: new_val = datetime.fromisoformat(v)
                        else: new_val = v
                    except: new_val = v
                else: new_val = self._deserialize(v)
                new_dict[k] = new_val
            return new_dict
        elif isinstance(data, list): return [self._deserialize(v) for v in data]
        return data

# ==========================================
# 3. 全域配置與狀態儲存 (機器人的大腦)
# ==========================================
class BotConfig:
    def __init__(self):
        self.bot_token = os.getenv("TG_BOT_TOKEN")
        self.application = None 
        self.loop = None        
        self.pm = PersistenceManager()
        
        self.warning_duration = 5  
        self.max_violations = 3    
        
        # 網址白名單 (允許這些網域的連結)
        self.allowed_domains = {
            "google.com", "wikipedia.org", "telegram.org", "t.me", 
            "facebook.com", "github.com", "blogspot.com", "line.me", 
            "portaly.cc", "ttt3388.com.tw", "webnode.tw", "ecup78.com", "jktank.net",
            "youtube.com", "youtu.be"
        }

        # Telegram 內部連結白名單 (t.me/ 後面的 ID)
        self.telegram_link_whitelist = {
            "ecup78", "ttt3388", "setlanguage", "ecup788_lulu156", 
            "ecup788_hhaa555", "lulu156_ecup788", "flower_5555", 
            "ecup78_1", "ii17225278", "sexy_ttt3388", "line527817ii", 
            "tmdgan2_0", "ttt3388sex", "ii1722", "taiwan", "Hsinchu1688", 
            "sanchong168", "xinzhuang168", "taishanwugu168", "hsinchu1688", 
            "zhonghe168", "tucheng_168", "linkou168", "keelung168"
        }

        # 貼圖包白名單
        self.sticker_whitelist = {"ecup78_bot", "ecup78", "ttt3388"}

        # 電話前綴黑名單
        self.blocked_phone_prefixes = {
            "+91", "+86", "+95", "+852", "+60", "+84", "+63", "+1", "+62", "+41", "+44", "+855", "+87", "+66", "+48"
        }

        # 關鍵字黑名單
        self.blocked_keywords = {
            # 詐騙/博弈
            "假钞", "捡钱", "项目", "電報", "@xsm77788", "君临", "南宫", 
            "集团", "直招", "充值", "提款", "到账","挣米", "日赚", "回款", 
            "上压", "担保", "兼职", "手气", "撸金", "暴利", "押金", "包赚",
            "风口", "一单", "博彩", "彩票", "赛车", "飞艇", "哈希",
            "百家乐", "投资", "USDT", "TRX", "包过", "洗米", "跑分",
            "现场", "连连", "满", "澳门", "新澳", "风险", "搞d", "集团",
            "总代", "直招", "南宫", "充值", "撸金", "暴利", "押金", 
            "提款", "到账","没有风险", "带撸", "绿色项目", "纯绿色", "无风险", 
            "不用押金", "最稳", "提款到账", "叚", "普通人也能做", "@kdnfdf",
            "下面b", "@maopianqun", "@zhiqiang", "xi米", "@kdjfnmsz",
            "黑U", "包上岸", "一直都在呀", "箜", "柯月", "meng男",  
            # 個資/黑產
            "查档", "身份证", "户籍", "开房", "手机号", "机主", 
            "轨迹", "车队", "入款", "出款",
            # 色情/引流
            "迷药", "春药", "裸聊", "极品", "强奸", "销魂", 
            "约炮", "同城", "资源", "人兽", "皮肤", "萌酱",
            "萝莉", "爆炒", "做坏事", "蜜桃臀", "路边", "坏事", 
            "看B", "看b", "BB", "bb", "痒", "皮肤", "hhh",
            # 簡體高頻詞
            "置顶", "软件", "下载", "点击", "链接", "免费观看", "点击下方",
            # 新增拼音與短語規避詞
            "好 lu", "ju 金", "秒反", "秒返", "lu金", "带lu"
        }

        self.strict_simplified_chars = {
            "国", "会", "发", "现", "关", "质", "员", "机", "产", "气", 
            "实", "则", "两", "结", "营", "报", "种", "专", "务", "战",
            "风", "让", "钱", "变", "间", "给", "号", "图", "亲", "极",
            "点", "击", "库", "车", "东", "应", "启", "书", "评", "叚", 
            "无", "马", "过", "办", "证", "听", "说", "话", "频", "视",
            "户", "罗", "边", "观", "么", "开", "区", "帅", "费", "捞",
            "临", "宫", "际", "备", "绿", "团", "胜", "总", "没", "险", 
            "带", "撸", "优", "势", "纯", "赚", "稳", "账", "项", "叶",
            "万"
        }
        
        # 指定豁免檢查的 戰鬥群夥伴 VIP 用戶 ID (發言不受任何過濾規則限制)
        self.exempt_user_ids = {
            7363979036, 6168587103, 6660718633, 5152410443,
            1121824397, 739962535, 6176254570, 5074058687,
            7597693349, 835207824, 7716513113
        }
        
        self.violation_tracker: Dict[Tuple[int, int], Dict] = {}
        self.blacklist_members: Dict[str, Dict] = {}
        self.question_bank: Dict[str, Dict] = {}
        self.pending_verifications: Dict[str, Dict] = {}
        
        # [新增] 記錄驗證失敗/逾時的用戶
        self.failed_verifications: Dict[str, Dict] = {}
        
        self.deleted_timestamps: List[datetime] = []
        
        self.logs: List[Dict] = []
        self.log_lock = Lock()
        self.flagged_media_groups: Dict[str, datetime] = {}

    def load_state(self):
        data = self.pm.load()
        if data:
            self.blacklist_members = data.get("blacklist", {})
            self.question_bank = data.get("question_bank", {}) 
            
            # [新增] 讀取驗證失敗名單
            self.failed_verifications = data.get("failed_verifications", {})
            for k, v in self.failed_verifications.items():
                if isinstance(v.get("time"), str):
                     try: v["time"] = datetime.fromisoformat(v["time"])
                     except: v["time"] = get_now_tw()

            raw_tracker = data.get("tracker", {})
            for k, v in raw_tracker.items():
                try:
                    parts = k.split(',')
                    if len(parts) == 2: self.violation_tracker[(int(parts[0]), int(parts[1]))] = v
                except: pass
            
            for k, v in self.blacklist_members.items():
                if isinstance(v.get("time"), str):
                     try: v["time"] = datetime.fromisoformat(v["time"])
                     except: v["time"] = get_now_tw()
            
            raw_deleted = data.get("stats", {}).get("deleted_timestamps", [])
            self.deleted_timestamps = []
            for ts in raw_deleted:
                try:
                    if isinstance(ts, str): self.deleted_timestamps.append(datetime.fromisoformat(ts))
                    elif isinstance(ts, datetime): self.deleted_timestamps.append(ts)
                except: pass
        
        if not self.question_bank:
            default_qs = [
                {"text": "台語「好」的唸法為？", "options": ["熊🐻", "鶴🦩", "豬🐷", "狗🐶"], "correct_idx": 1},
                {"text": "請台灣髒話中「ㄍㄋㄋ」問候的是誰？", "options": ["奶奶", "妹妹", "媽媽", "表姊"], "correct_idx": 2},
                {"text": "哪一個是「他媽的」意思？", "options": ["ㄊㄇㄉ", "ㄒMㄅ", "ㄅㄆㄇ", "ㄔㄐㄅ"], "correct_idx": 0}
            ]
            for dq in default_qs:
                q_id = str(uuid.uuid4())[:8]
                self.question_bank[q_id] = {
                    "id": q_id,
                    "text": dq["text"],
                    "options": dq["options"],
                    "correct_idx": dq["correct_idx"],
                    "image_data": None
                }
            self.add_log("SYSTEM", "🦋 偵測到題庫為空，已自動寫入 3 題預設題庫並同步至資料庫。")
            self.save_state() 

        self.add_log("INFO", f"🦋 系統重啟，恢復 {len(self.blacklist_members)} 筆黑名單與 {len(self.question_bank)} 題庫")

    def save_state(self):
        tracker_serializable = {f"{k[0]},{k[1]}": v for k, v in self.violation_tracker.items()}
        data = {
            "blacklist": self.blacklist_members,
            "tracker": tracker_serializable,
            "question_bank": self.question_bank,
            "failed_verifications": self.failed_verifications, # [新增] 儲存驗證失敗名單
            "stats": {"deleted_timestamps": [ts.isoformat() for ts in self.deleted_timestamps]}
        }
        Thread(target=self.pm.save, args=(data,), daemon=True).start()

    def add_log(self, level: str, message: str):
        now = get_now_tw().strftime("%H:%M:%S")
        with self.log_lock:
            self.logs.insert(0, {"time": now, "level": level, "content": message})
            self.logs = self.logs[:50] 
        logger.info(f"[{level}] {message}")

    def add_violation(self, chat_id: int, user_id: int) -> int:
        today = get_now_tw().date()
        key = (chat_id, user_id)
        if key not in self.violation_tracker or self.violation_tracker[key]["last_date"].date() != today:
            self.violation_tracker[key] = {"count": 1, "last_date": get_now_tw()}
        else: self.violation_tracker[key]["count"] += 1
        self.save_state()
        return self.violation_tracker[key]["count"]

    def record_blacklist(self, user_id: int, name: str, chat_id: int, chat_title: str):
        now = get_now_tw()
        key = f"{chat_id}_{user_id}"
        self.blacklist_members[key] = {"uid": user_id, "name": name, "chat_id": chat_id, "chat_title": chat_title, "time": now}
        self.save_state()

    def reset_violation(self, chat_id: int, user_id: int):
        v_key = (chat_id, user_id)
        bl_key = f"{chat_id}_{user_id}"
        if v_key in self.violation_tracker: self.violation_tracker[v_key]["count"] = 0
        if bl_key in self.blacklist_members: del self.blacklist_members[bl_key]
        self.save_state()

    # [新增] 記錄驗證失敗的成員
    def record_failed_verification(self, user_id: int, name: str, chat_id: int, chat_title: str):
        now = get_now_tw()
        key = f"{chat_id}_{user_id}"
        self.failed_verifications[key] = {"uid": user_id, "name": name, "chat_id": chat_id, "chat_title": chat_title, "time": now}
        self.save_state()

    # [新增] 移除驗證失敗記錄
    def remove_failed_verification(self, chat_id: int, user_id: int):
        key = f"{chat_id}_{user_id}"
        if key in self.failed_verifications:
            del self.failed_verifications[key]
            self.save_state()

    # [修改] 取得「所有」的驗證失敗名單 (無24小時限制)
    def get_recent_failed(self, filter_chat_id: Optional[int] = None) -> List[Dict]:
        now = get_now_tw()
        all_failed = []
        for key, info in self.failed_verifications.items():
            try:
                t = info.get("time")
                if not isinstance(t, datetime): 
                    info["time"] = datetime.fromisoformat(t) if t else now
                if filter_chat_id is None or info["chat_id"] == filter_chat_id: 
                    all_failed.append(info)
            except: continue
        return sorted(all_failed, key=lambda x: x["time"], reverse=True)

    def _cleanup_old_timestamps(self):
        now = get_now_tw()
        self.deleted_timestamps = [ts for ts in self.deleted_timestamps if (now - ts).total_seconds() <= 86400]

    def record_deletion(self):
        self.deleted_timestamps.append(get_now_tw())
        self._cleanup_old_timestamps()
        self.save_state()

    def get_recent_deleted_count(self) -> int:
        self._cleanup_old_timestamps()
        return len(self.deleted_timestamps)

    def get_recent_blacklist(self, filter_chat_id: Optional[int] = None) -> List[Dict]:
        now = get_now_tw()
        recent = []
        for key, info in self.blacklist_members.items():
            try:
                t = info.get("time")
                if not isinstance(t, datetime): t = datetime.fromisoformat(t) if t else now
                if (now - t).total_seconds() < 86400: 
                    if filter_chat_id is None or info["chat_id"] == filter_chat_id: recent.append(info)
            except: continue
        return sorted(recent, key=lambda x: x["time"], reverse=True)

    def get_blacklist_chats(self) -> Dict[int, str]:
        chats = {info["chat_id"]: info["chat_title"] for info in self.blacklist_members.values()}
        chats.update({info["chat_id"]: info["chat_title"] for info in self.failed_verifications.values()})
        return chats

config = BotConfig()

# ==========================================
# 4. 偵測與處理邏輯 (核心過濾算法)
# ==========================================
def is_domain_allowed(url: str) -> bool:
    try: return tldextract.extract(url.strip().lower()).registered_domain in config.allowed_domains
    except: return False

def contains_prohibited_content(text: str) -> Tuple[bool, Optional[str]]:
    if not text: return False, None
    clean_text = re.sub(r'\s+|\u200b|\u200c|\u200d|\ufeff', '', text)
    for char in clean_text:
        if char.isalpha():
            cp = ord(char)
            is_allowed_lang = (
                (0x0041 <= cp <= 0x005A) or (0x0061 <= cp <= 0x007A) or (0x0E00 <= cp <= 0x0E7F) or
                (0x3040 <= cp <= 0x30FF) or (0xAC00 <= cp <= 0xD7A3) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F) or
                (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0x20000 <= cp <= 0x2EBEF) or
                (0x3100 <= cp <= 0x312F) or (0x31A0 <= cp <= 0x31BF) or (0x02B0 <= cp <= 0x02FF) or
                (0xFF21 <= cp <= 0xFF3A) or (0xFF41 <= cp <= 0xFF5A)
            )
            if not is_allowed_lang: return True, f"不允許的語言文字"
    for kw in config.blocked_keywords:
        if kw in text or kw in clean_text: return True, f"關鍵字: {kw}"
    if hanzidentifier.has_chinese(clean_text):
        for char in clean_text:
            if char in config.strict_simplified_chars: return True, f"禁語: {char}"
            try:
                if hanzidentifier.is_simplified(char) and not hanzidentifier.is_traditional(char): return True, f"簡體: {char}"
            except: continue
    return False, None

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 改為接收 chat_member 狀態更新
    result = update.chat_member
    if not result: return
    
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    
    # 判斷是否為「新進群」的行為 (原本不在群內 -> 變成成員)
    was_member = old_status in [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
    ]
    is_member = new_status in [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    ]
    
    # 如果原本就是成員，或者新狀態不是成員(例如退群)，則不處理
    if was_member or not is_member: return
    
    new_member = result.new_chat_member.user
    chat = result.chat
    
    if new_member.is_bot: return
    
    try:
        await context.bot.restrict_chat_member(
            chat.id, 
            new_member.id, 
            ChatPermissions(can_send_messages=False)
        )
        config.add_log("INFO", f"新成員加入: 準備對 {new_member.full_name} 進行分類帽測驗")
    except Exception as e:
        config.add_log("WARN", f"無法限制新成員 {new_member.full_name}: {e}")
        return

    available_qs = list(config.question_bank.values())
    if not available_qs:
        p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
        await context.bot.restrict_chat_member(chat.id, new_member.id, p)
        return
        
    sample_size = min(3, len(available_qs))
    selected_qs = random.sample(available_qs, sample_size)
    
    session_id = f"{chat.id}_{new_member.id}"
    config.pending_verifications[session_id] = {
        "user_id": new_member.id,
        "user_name": new_member.full_name,
        "chat_id": chat.id,
        "chat_title": chat.title if chat else "未知群組",
        "questions": selected_qs,
        "current_q": 0,
        "message_id": None,
        "expires_at": get_now_tw() + timedelta(minutes=5)
    }
    
    config.loop.create_task(verification_timeout(session_id, context))
    await send_verification_question(session_id, context)

async def send_verification_question(session_id, context):
    session = config.pending_verifications.get(session_id)
    if not session: return
    
    q_idx = session["current_q"]
    q_data = session["questions"][q_idx]
    
    keyboard = []
    for i, opt in enumerate(q_data["options"]):
        keyboard.append([InlineKeyboardButton(opt, callback_data=f"v_{session['user_id']}_{q_idx}_{i}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    now = get_now_tw()
    remaining = max(0, int((session["expires_at"] - now).total_seconds()))
    mins, secs = divmod(remaining, 60)
    time_str = f"{mins} 分 {secs} 秒"
    
    text = f"🦋 <b>霍格華茲入學測驗通知</b> 🦋\n\n<b>帽子分類帽測驗 ({q_idx+1}/{len(session['questions'])})</b>\n新入學員 <a href='tg://user?id={session['user_id']}'>{session.get('user_name', '學員')}</a> 請戴上分類帽作答\n⏱ <b>剩餘時間：{time_str}</b>\n(逾時將被施展「沉默咒」永久禁言)\n\n💡 <b>題目：{q_data['text']}</b>"
    
    chat_id = session["chat_id"]
    
    try:
        if session["message_id"]: await context.bot.delete_message(chat_id, session["message_id"])
    except: pass
    
    try:
        if q_data.get("image_data"):
            img_str = q_data["image_data"].split(',')[1]
            img_bytes = base64.b64decode(img_str)
            msg = await context.bot.send_photo(chat_id, photo=img_bytes, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            msg = await context.bot.send_message(chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        session["message_id"] = msg.message_id
    except Exception as e:
        config.add_log("ERROR", f"發送分類帽測驗失敗: {e}")

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if not data.startswith("v_"): return
    
    parts = data.split('_')
    target_user_id = int(parts[1])
    
    if query.from_user.id != target_user_id:
        await query.answer("❌ 警告：這不是你的分類帽，請勿干擾他人測驗！", show_alert=True)
        return
        
    session_id = f"{query.message.chat_id}_{target_user_id}"
    session = config.pending_verifications.get(session_id)
    
    if not session:
        await query.answer("❌ 你的分類帽測驗已過期，或者你被施展了遺忘咒！", show_alert=True)
        try: await query.message.delete()
        except: pass
        return
        
    ans_idx = int(parts[3])
    current_q = session["questions"][session["current_q"]]
    
    if ans_idx == current_q["correct_idx"]:
        session["current_q"] += 1
        if session["current_q"] >= len(session["questions"]):
            await query.answer("🦋霍格華茲入學測驗通知🦋\n\n✅ 測驗通過！\n🪄歡迎加入霍格華茲學院！", show_alert=True)
            p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
            await context.bot.restrict_chat_member(session["chat_id"], target_user_id, p)
            try: await query.message.delete()
            except: pass
            del config.pending_verifications[session_id]
            config.add_log("SUCCESS", f"新成員 {query.from_user.full_name} 通過分類帽測驗！")
        else:
            await query.answer("🦋霍格華茲入學測驗通知🦋\n\n✅ 答對了！\n🪄分類帽正在思考...請繼續作答下一題。", show_alert=False)
            await send_verification_question(session_id, context)
    else:
        await query.answer("🦋霍格華茲入學測驗通知🦋\n\n❌ 答錯了！\n🪄你已被施展沉默咒，將無法在學院內發言。", show_alert=True)
        try: await query.message.delete()
        except: pass
        
        # [記錄失敗]
        chat_title = query.message.chat.title if query.message.chat else "未知群組"
        config.record_failed_verification(target_user_id, query.from_user.full_name, session["chat_id"], chat_title)
        
        del config.pending_verifications[session_id]
        config.add_log("WARN", f"新成員 {query.from_user.full_name} 分類帽測驗失敗，已被施展沉默咒！")

async def verification_timeout(session_id, context):
    while True:
        await asyncio.sleep(15) 
        session = config.pending_verifications.get(session_id)
        if not session:
            break 
        
        now = get_now_tw()
        remaining = int((session["expires_at"] - now).total_seconds())
        
        if remaining <= 0:
            try:
                if session["message_id"]: await context.bot.delete_message(session["chat_id"], session["message_id"])
            except: pass
            
            # [記錄逾時失敗]
            chat_title = session.get("chat_title", "未知群組")
            user_name = session.get("user_name", "學員")
            config.record_failed_verification(session["user_id"], user_name, session["chat_id"], chat_title)
            
            if session_id in config.pending_verifications:
                del config.pending_verifications[session_id]
            config.add_log("WARN", f"某成員分類帽測驗逾時，已被沒收魔杖維持禁言狀態。")
            break
            
        q_idx = session["current_q"]
        q_data = session["questions"][q_idx]
        keyboard = []
        for i, opt in enumerate(q_data["options"]):
            keyboard.append([InlineKeyboardButton(opt, callback_data=f"v_{session['user_id']}_{q_idx}_{i}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        mins, secs = divmod(remaining, 60)
        time_str = f"{mins} 分 {secs} 秒"
        
        text = f"🦋 <b>霍格華茲入學測驗通知</b> 🦋\n\n<b>帽子分類帽測驗 ({q_idx+1}/{len(session['questions'])})</b>\n新入學員 <a href='tg://user?id={session['user_id']}'>{session.get('user_name', '學員')}</a> 請戴上分類帽作答\n⏱ <b>剩餘時間：{time_str}</b>\n(逾時將被施展「沉默咒」永久禁言)\n\n💡 <b>題目：{q_data['text']}</b>"
        
        try:
            if session["message_id"]:
                if q_data.get("image_data"):
                    await context.bot.edit_message_caption(chat_id=session["chat_id"], message_id=session["message_id"], caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                else:
                    await context.bot.edit_message_text(chat_id=session["chat_id"], message_id=session["message_id"], text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            pass 

async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, admin_sender = update.effective_chat, update.effective_user
    try:
        member = await chat.get_member(admin_sender.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: return
        user_id = None; mention = "未知用戶"
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            user_id = target_user.id; mention = target_user.mention_html()
        elif context.args:
            try: user_id = int(context.args[0]); mention = f'<a href="tg://user?id={user_id}">學員 {user_id}</a>'
            except: pass
        if user_id:
            p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
            await context.bot.restrict_chat_member(chat.id, user_id, p)
            config.reset_violation(chat.id, user_id)
            config.add_log("SUCCESS", f"🦋 管理員在 [{chat.title}] 指令解封 {user_id}")
            await update.message.reply_text(f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>", parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"❌ 錯誤: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg: return
    user = msg.from_user; sender_chat = msg.sender_chat
    offender_id = None; offender_name = "Unknown"; mention_html = ""; is_bot = False

    if user: offender_id = user.id; offender_name = user.full_name; is_bot = user.is_bot; mention_html = user.mention_html()
    elif sender_chat: offender_id = sender_chat.id; offender_name = sender_chat.title or "匿名頻道"; is_bot = False; mention_html = f"<b>{offender_name}</b>"
    else: return 

    if is_bot: return 

    all_texts: List[str] = []; urls_to_check: List[str] = [] 
    if msg.text: all_texts.append(msg.text)
    if msg.caption: all_texts.append(msg.caption)
    
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in ents:
        if ent.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
            u = ent.url if ent.type == MessageEntity.TEXT_LINK else (msg.text or msg.caption)[ent.offset : ent.offset+ent.length]
            if u: urls_to_check.append(u); all_texts.append(f"[實體連結]: {u}") 
    
    if msg.link_preview_options and msg.link_preview_options.url:
        hidden_url = msg.link_preview_options.url; urls_to_check.append(hidden_url); all_texts.append(f"[隱藏預覽]: {hidden_url}") 
    if msg.via_bot: all_texts.append(f"[呼叫機器人]: @{msg.via_bot.username}")
    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat: src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user: src_name = msg.forward_origin.sender_user.full_name
        if src_name: all_texts.append(src_name)
    if msg.contact:
        if msg.contact.first_name: all_texts.append(msg.contact.first_name)
        if msg.contact.last_name: all_texts.append(msg.contact.last_name)
    if msg.venue:
        if msg.venue.title: all_texts.append(msg.venue.title)
        if msg.venue.address: all_texts.append(msg.venue.address)
    if msg.sticker:
        try: s_set = await context.bot.get_sticker_set(msg.sticker.set_name); all_texts.append(s_set.title)
        except: pass
    if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if hasattr(btn, 'text'): all_texts.append(btn.text)
    if msg.poll:
        all_texts.append(msg.poll.question); 
        for opt in msg.poll.options: all_texts.append(opt.text)
    quote = getattr(msg, 'quote', None)
    if quote:
        if hasattr(quote, 'text') and quote.text: all_texts.append(quote.text)
        if hasattr(quote, 'caption') and quote.caption: all_texts.append(quote.caption)

    is_edit_tag = " (編輯訊息)" if update.edited_message else ""
    full_content_log = " | ".join(all_texts)
    
    if not full_content_log:
        if msg.sticker: full_content_log = f"<貼圖: {msg.sticker.set_name}>"
        elif msg.photo or msg.video or msg.animation or msg.document: full_content_log = "<媒體檔案>"
        else: full_content_log = "<無文字內容>"

    config.add_log("INFO", f"[{msg.chat.title}] [{offender_name}]{is_edit_tag} 偵測: {full_content_log[:150]}...")

    if user:
        if user.id in config.exempt_user_ids: return
        try:
            if msg.chat.type != "private":
                cm = await msg.chat.get_member(user.id)
                if cm.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: return 
        except: pass

    if msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete(); return
        except: pass

    violation_reason: Optional[str] = None

    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat: src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user: src_name = msg.forward_origin.sender_user.full_name
        if src_name:
             is_bad, r = contains_prohibited_content(src_name)
             if is_bad: violation_reason = f"轉傳來源違規 ({src_name})"

    if not violation_reason and msg.contact:
        phone = msg.contact.phone_number or ""
        clean_phone = re.sub(r'[+\-\s]', '', phone)
        blocked_clean = [re.sub(r'[+\-\s]', '', p) for p in config.blocked_phone_prefixes]
        if any(clean_phone.startswith(pre) for pre in blocked_clean if pre): violation_reason = f"來自受限國家門號 ({phone[:3]}...)"

    if not violation_reason and msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            combined_lower = (s_set.title + msg.sticker.set_name).lower()
            if ("@" in combined_lower or "_by_" in combined_lower):
                if not any(wd in combined_lower for wd in config.sticker_whitelist):
                    safe_title = s_set.title.replace("@", "")
                    violation_reason = f"未授權 ID ({safe_title})"
        except: pass

    if not violation_reason:
        unique_texts = list(set(all_texts))
        for t in unique_texts:
            is_bad, r = contains_prohibited_content(t)
            if is_bad: violation_reason = r; break

    if not violation_reason:
        for u in urls_to_check:
            u_clean = u.strip().lower()
            if not is_domain_allowed(u_clean):
                violation_reason = f"不明連結 ({u_clean[:30]}...)"
                break
            tg_domains = ["t.me/", "telegram.me/", "telegram.dog/"]
            for tg_domain in tg_domains:
                if tg_domain in u_clean:
                    path = u_clean.split(tg_domain)[-1].split('/')[0].split('?')[0].replace("@", "")
                    if path and path not in config.telegram_link_whitelist: violation_reason = f"未授權 TG 連結 ({path})"
                    break
            if violation_reason: break

    if violation_reason:
        if msg.media_group_id: config.flagged_media_groups[msg.media_group_id] = datetime.now()
        try:
            try: 
                await msg.delete()
                config.record_deletion() 
            except: pass
            
            v_count = config.add_violation(msg.chat.id, offender_id)
            if v_count >= config.max_violations:
                try: 
                    if user: await context.bot.restrict_chat_member(msg.chat.id, user.id, ChatPermissions(can_send_messages=False))
                    elif sender_chat: await context.bot.ban_chat_sender_chat(msg.chat.id, sender_chat.id)
                except: pass
                config.record_blacklist(offender_id, offender_name, msg.chat.id, msg.chat.title)
                config.add_log("ERROR", f"🦋 {offender_name} 在 [{msg.chat.title}] 違規達上限，封鎖入阿茲卡班")
                await context.bot.send_message(chat_id=msg.chat.id, text=f"🦋 <b>霍格華茲禁言通知</b> 🦋\n\n🦉用戶學員：{mention_html}\n🈲發言已多次違反校規。\n🈲已被咒語《阿哇呾喀呾啦》擊殺⚡️\n🪄<b>如被誤殺請待在阿茲卡班內稍等\n並請客服通知鄧不利多校長幫你解禁</b>", parse_mode=ParseMode.HTML)
            else:
                sent_warn = await context.bot.send_message(msg.chat.id, f"🦋 <b>霍格華茲警告通知</b> 🦋\n\n🦉用戶學員：{mention_html}\n⚠️違反校規：{violation_reason}\n⚠️違規計次：({v_count}/{config.max_violations})\n🪄<b>多次違規將被黑魔法教授擊殺</b>", parse_mode=ParseMode.HTML)
                await asyncio.sleep(config.warning_duration); await sent_warn.delete()
        except: pass
    elif msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete()
        except: pass

# ==========================================
# 5. Flask 後台管理網頁路由
# ==========================================
app = Flask(__name__)

@app.route('/')
def index():
    is_active = config.application is not None
    filter_cid = request.args.get('filter_chat_id', type=int)
    
    members = config.get_recent_blacklist(filter_cid)
    failed_members = config.get_recent_failed(filter_cid) # [傳送變數]
    filter_chats = config.get_blacklist_chats()
    
    recent_deleted_count = config.get_recent_deleted_count()
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ui_path = os.path.join(base_dir, 'dashboard_ui.html')
    try:
        with open(ui_path, 'r', encoding='utf-8') as f: html_template = f.read()
    except FileNotFoundError: html_template = f"<h1>找不到 {ui_path} 檔案，請確認檔案已上傳。</h1>"
        
    return render_template_string(
        html_template, 
        config=config, 
        is_active=is_active, 
        members=members, 
        failed_members=failed_members, 
        filter_chats=filter_chats, 
        active_filter=filter_cid,
        recent_deleted_count=recent_deleted_count
    )

@app.route('/api/logs')
def get_logs():
    try:
        with config.log_lock: logs_data = list(config.logs)
        return Response(json.dumps(logs_data, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        error_log = [{"time": "系統", "level": "ERROR", "content": f"內部錯誤: {str(e)}"}]
        return Response(json.dumps(error_log, ensure_ascii=False), status=200, mimetype='application/json')

@app.route('/update', methods=['POST'])
def update():
    try:
        raw_duration = request.form.get('duration', '5'); config.warning_duration = int(raw_duration) if raw_duration.isdigit() else 5
        raw_max_v = request.form.get('max_v', '3'); config.max_violations = int(raw_max_v) if raw_max_v.isdigit() else 3
        config.allowed_domains = {d.strip().lower() for d in request.form.get('domains', '').split(',') if d.strip()}
        config.telegram_link_whitelist = {t.strip().lower().replace("@", "") for t in request.form.get('tg_links', '').split(',') if t.strip()}
        config.blocked_phone_prefixes = {p.strip() for p in request.form.get('phone_pre', '').split(',') if p.strip()}
        config.blocked_keywords = {k.strip() for k in request.form.get('keywords', '').split(',') if k.strip()}
        config.sticker_whitelist = {s.strip().lower().replace("@", "") for s in request.form.get('sticker_ws', '').split(',') if s.strip()}
        config.save_state()
        config.add_log("SUCCESS", "🦋 校規與過濾設定已同步更新")
    except Exception as e: config.add_log("ERROR", f"🦋 更新失敗: {e}")
    return redirect(url_for('index'))

@app.route('/add_question', methods=['POST'])
def add_question():
    try:
        q_text = request.form.get('question_text')
        options = [request.form.get('opt0'), request.form.get('opt1'), request.form.get('opt2'), request.form.get('opt3')]
        correct_idx = int(request.form.get('correct_idx'))
        
        image_data = None
        file = request.files.get('image')
        if file and file.filename != '':
            mime_type = file.mimetype
            base64_img = base64.b64encode(file.read()).decode('utf-8')
            image_data = f"data:{mime_type};base64,{base64_img}"
            
        q_id = str(uuid.uuid4())[:8] 
        config.question_bank[q_id] = {
            "id": q_id,
            "text": q_text,
            "options": options,
            "correct_idx": correct_idx,
            "image_data": image_data
        }
        config.save_state()
        config.add_log("SUCCESS", f"🦋 成功新增一筆題庫: {q_text[:10]}...")
    except Exception as e:
        config.add_log("ERROR", f"🦋 新增題目失敗: {e}")
    return redirect(url_for('index'))

@app.route('/delete_question', methods=['POST'])
def delete_question():
    try:
        q_id = request.form.get('q_id')
        if q_id in config.question_bank:
            del config.question_bank[q_id]
            config.save_state()
            config.add_log("SUCCESS", "🦋 成功刪除一筆題庫")
    except Exception as e: pass
    return redirect(url_for('index'))

@app.route('/unban_member', methods=['POST'])
def unban_member():
    try:
        user_id, chat_id = int(request.form.get('user_id')), int(request.form.get('chat_id'))
        key = f"{chat_id}_{user_id}"; member_data = config.blacklist_members.get(key, {})
        user_name = member_data.get("name", f"學員 {user_id}")
        mention = f"<b>{user_name}</b>" if user_id < 0 else f'<a href="tg://user?id={user_id}">{user_name}</a>'
        
        async def do_unban():
            try:
                p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
                if user_id > 0:
                    await config.application.bot.restrict_chat_member(chat_id, user_id, p)
                    await config.application.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                else: await config.application.bot.unban_chat_sender_chat(chat_id, user_id)
                config.reset_violation(chat_id, user_id)
                config.add_log("SUCCESS", f"🦋 網頁解封 {user_name}，地點 [{member_data.get('chat_title')}]")
                await config.application.bot.send_message(chat_id=chat_id, text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>", parse_mode=ParseMode.HTML)
            except Exception as e: config.add_log("ERROR", f"🦋 解封失敗: {e}")
        if config.loop: asyncio.run_coroutine_threadsafe(do_unban(), config.loop)
    except: pass
    return redirect(url_for('index'))

@app.route('/unmute_member', methods=['POST'])
def unmute_member():
    try:
        user_id, chat_id = int(request.form.get('user_id')), int(request.form.get('chat_id'))
        key = f"{chat_id}_{user_id}"
        member_data = config.failed_verifications.get(key, {})
        user_name = member_data.get("name", f"學員 {user_id}")
        mention = f"<b>{user_name}</b>" if user_id < 0 else f'<a href="tg://user?id={user_id}">{user_name}</a>'
        
        async def do_unmute():
            try:
                p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
                await config.application.bot.restrict_chat_member(chat_id, user_id, p)
                config.remove_failed_verification(chat_id, user_id) 
                config.add_log("SUCCESS", f"🦋 網頁解禁 {user_name} 發言權，地點 [{member_data.get('chat_title')}]")
                await config.application.bot.send_message(chat_id=chat_id, text=f"🦋 <b>霍格華茲發言解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部重新審查\n✅已破除沉默咒恢復發言權\n🪄<b>歡迎學員加入花家霍格華茲</b>", parse_mode=ParseMode.HTML)
            except Exception as e: config.add_log("ERROR", f"🦋 解禁失敗: {e}")
        if config.loop: asyncio.run_coroutine_threadsafe(do_unmute(), config.loop)
    except: pass
    return redirect(url_for('index'))

def run_telegram_bot():
    if not config.bot_token: return
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); config.loop = loop 
    config.load_state()
    try:
        bot_app = ApplicationBuilder().token(config.bot_token).build(); config.application = bot_app 
        async def clear(): 
            try: await bot_app.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            config.add_log("INFO", "🦋 Telegram 通訊連線成功，系統已準備就緒。")
        loop.run_until_complete(clear())
        
        bot_app.add_handler(CommandHandler("unban", unban_handler))
        
        # 🟢 【修改此處】將原本的 MessageHandler 換成強大的 ChatMemberHandler
        bot_app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
        
        bot_app.add_handler(CallbackQueryHandler(verify_callback, pattern="^v_"))
        bot_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
        bot_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & (~filters.COMMAND), handle_message))
        
        bot_app.run_polling(stop_signals=False, close_loop=False)
    except Exception as e: config.add_log("ERROR", f"🦋 核心崩潰: {e}")

if __name__ == '__main__':
    tg_thread = Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()
    serve(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
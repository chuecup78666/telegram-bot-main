import os
import logging
import asyncio
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from typing import Set, Optional, Dict, List, Tuple
from threading import Thread

# --- Web 框架 ---
# 用於建立後台管理網頁，讓 Render 偵測到 Port 不會休眠
from flask import Flask, render_template_string, request, redirect, url_for
from waitress import serve

# --- Telegram 機器人核心模組 ---
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

# --- 第三方分析工具 ---
import hanzidentifier # 用於判斷簡體中文字
import tldextract     # 用於解析網址的網域 (Domain)

# ==========================================
# 1. 系統日誌與時區設定
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 設定台灣時區 (UTC+8)，確保後台時間顯示正確
TW_TZ = timezone(timedelta(hours=8))

def get_now_tw():
    """ 取得目前的台灣時間 """
    return datetime.now(timezone.utc).astimezone(TW_TZ)

# ==========================================
# 2. 雲端 / 本地 資料持久化管理 (Firebase + JSON)
# ==========================================
class PersistenceManager:
    """ 
    負責將機器人的設定與黑名單寫入儲存空間。
    [新增] Firebase 支援：因為 Render 每次重新部署都會清空本地檔案。
    只要在 Render 設定 FIREBASE_DB_URL，資料就會永久保存在雲端防重置。
    """
    def __init__(self, filename="flowersbot_data.json"):
        self.filename = filename
        # 從 Render 環境變數讀取 Firebase 資料庫網址
        self.firebase_url = os.getenv("FIREBASE_DB_URL")
        # 自動移除網址結尾的斜線以防請求錯誤
        if self.firebase_url and self.firebase_url.endswith('/'):
            self.firebase_url = self.firebase_url[:-1]

    def save(self, data: dict):
        """ 將記憶體中的資料寫入雲端或硬碟 """
        try:
            # 將 datetime 時間物件轉為字串，因為 JSON 不支援時間格式
            serializable_data = self._serialize(data)
            
            # 1. 優先儲存至 Firebase 雲端 (徹底解決 Render 刪除檔案問題)
            if self.firebase_url:
                try:
                    requests.put(f"{self.firebase_url}/bot_data.json", json=serializable_data, timeout=5)
                except Exception as e:
                    logger.error(f"雲端儲存失敗: {e}")

            # 2. 備份至本地檔案 (雙重保險)
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(serializable_data, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"資料儲存失敗: {e}")

    def load(self) -> dict:
        """ 從雲端或硬碟讀取資料回記憶體 """
        data = None
        try:
            # 1. 優先從 Firebase 雲端讀取
            if self.firebase_url:
                try:
                    resp = requests.get(f"{self.firebase_url}/bot_data.json", timeout=5)
                    if resp.status_code == 200 and resp.json():
                        data = resp.json()
                        logger.info("✅ 成功從 Firebase 雲端恢復資料！")
                except Exception as e:
                    logger.error(f"雲端讀取失敗: {e}")

            # 2. 若雲端無資料或未設定 URL，則讀取本地備份
            if not data and os.path.exists(self.filename):
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info("✅ 成功從本地檔案恢復資料！")
        except Exception as e:
            logger.error(f"資料讀取失敗: {e}")
            
        return self._deserialize(data) if data else {}

    def _serialize(self, data):
        """ 遞迴將資料中的 datetime 轉為 ISO 格式字串 """
        if isinstance(data, dict):
            return {k: self._serialize(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._serialize(v) for v in data]
        elif isinstance(data, datetime):
            return data.isoformat()
        return data

    def _deserialize(self, data):
        """ 遞迴將資料中的 ISO 格式字串轉回 datetime 物件 """
        if isinstance(data, dict):
            new_dict = {}
            for k, v in data.items():
                if isinstance(v, str):
                    try:
                        # 簡單判斷字串是否長得像時間格式 (由 T 分隔)
                        if "T" in v and v.count("-") == 2 and v.count(":") >= 2:
                             new_val = datetime.fromisoformat(v)
                        else:
                             new_val = v
                    except:
                        new_val = v
                else:
                    new_val = self._deserialize(v)
                new_dict[k] = new_val
            return new_dict
        elif isinstance(data, list):
            return [self._deserialize(v) for v in data]
        return data

# ==========================================
# 3. 全域配置與狀態儲存 (機器人的大腦)
# ==========================================
class BotConfig:
    def __init__(self):
        # 讀取 Telegram Token
        self.bot_token = os.getenv("TG_BOT_TOKEN")
        self.application = None 
        self.loop = None        
        
        # 初始化存檔管理員
        self.pm = PersistenceManager()
        
        # 預設規則 (可在後台修改)
        self.warning_duration = 5  # 警告訊息停留秒數
        self.max_violations = 3    # 違規幾次後封鎖
        
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
            "tmdgan2_0", "ttt3388sex", "ii1722", "taiwan",
            "sanchong168", "xinzhuang168", "taishanwugu168", 
            "zhonghe168", "tucheng_168", "linkou168", "keelung168"
        }

        # 貼圖包白名單
        self.sticker_whitelist = {"ecup78_bot", "ecup78", "ttt3388"}

        # 電話前綴黑名單
        self.blocked_phone_prefixes = {
            "+91", "+86", "+95", "+852", "+60", "+84", "+63", "+1", "+62", "+41", "+44", "+855", "+87"
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
            "不用押金", "最稳", "提款到账",
            # 個資/黑產
            "查档", "身份证", "户籍", "开房", "手机号", "机主", 
            "轨迹", "车队", "入款", "出款",
            # 色情/引流
            "迷药", "春药", "裸聊", "极品", "强奸", "销魂", 
            "约炮", "同城", "资源", "人兽", "皮肤", "萌酱",
            "萝莉", "爆炒", "做坏事", "蜜桃臀", "路边", "坏事", 
            "看B", "看b", "BB", "bb", "痒", "皮肤", 
            # 簡體高頻詞
            "置顶", "软件", "下载", "点击", "链接", "免费观看", "点击下方",
            # 新增拼音與短語規避詞
            "好 lu", "ju 金", "秒反", "秒返", "lu金", "带lu"
        }

        self.strict_simplified_chars = {
            "国", "会", "发", "现", "关", "质", "员", "机", "产", "气", 
            "实", "则", "两", "结", "营", "报", "种", "专", "务", "战",
            "风", "让", "钱", "变", "间", "给", "号", "图", "亲", "极",
            "点", "击", "库", "车", "东", "应", "启", "书", "评",
            "无", "马", "过", "办", "证", "听", "说", "话", "频", "视",
            "户", "罗", "边", "观", "么", "开", "区", "帅", "费", "捞",
            "临", "宫", "际", "备", "绿", "团", "胜", "总", "没", "险", 
            "带", "撸", "优", "势", "纯", "赚", "稳", "账", "项"
        }
        
        # 指定豁免檢查的 戰鬥群夥伴 VIP 用戶 ID (發言不受任何過濾規則限制)
        self.exempt_user_ids = {
            7363979036, 6168587103, 6660718633, 5152410443,
            1121824397, 739962535, 6176254570, 5074058687,
            7597693349, 835207824, 7716513113
        }
        
        self.violation_tracker: Dict[Tuple[int, int], Dict] = {}
        self.blacklist_members: Dict[str, Dict] = {}
        self.total_deleted_count = 0
        self.logs: List[Dict] = []
        self.last_heartbeat: Optional[datetime] = None
        self.flagged_media_groups: Dict[str, datetime] = {}

    def load_state(self):
        """ 啟動時呼叫：從檔案讀取上次的紀錄 """
        data = self.pm.load()
        if data:
            self.blacklist_members = data.get("blacklist", {})
            # 將 Tracker 的 Key 從字串轉回 Tuple (chat_id, user_id)
            raw_tracker = data.get("tracker", {})
            for k, v in raw_tracker.items():
                try:
                    parts = k.split(',')
                    if len(parts) == 2:
                        self.violation_tracker[(int(parts[0]), int(parts[1]))] = v
                except: pass
            
            # 修復時間格式
            for k, v in self.blacklist_members.items():
                if isinstance(v.get("time"), str):
                     try: v["time"] = datetime.fromisoformat(v["time"])
                     except: v["time"] = get_now_tw()
            
            self.total_deleted_count = data.get("stats", {}).get("deleted", 0)
            self.add_log("INFO", f"🦋 系統重啟，已恢復 {len(self.blacklist_members)} 筆黑名單資料")

    def save_state(self):
        """ 狀態變動時呼叫：將資料存入檔案 """
        tracker_serializable = {f"{k[0]},{k[1]}": v for k, v in self.violation_tracker.items()}
        data = {
            "blacklist": self.blacklist_members,
            "tracker": tracker_serializable,
            "stats": {"deleted": self.total_deleted_count}
        }
        # 使用執行緒背景存檔，不卡住機器人
        Thread(target=self.pm.save, args=(data,), daemon=True).start()

    def add_log(self, level: str, message: str):
        """ 新增後台 Log """
        now = get_now_tw().strftime("%H:%M:%S")
        self.logs.insert(0, {"time": now, "level": level, "content": message})
        self.logs = self.logs[:50] # 保留最近 50 筆紀錄
        logger.info(f"[{level}] {message}")

    def add_violation(self, chat_id: int, user_id: int) -> int:
        """ 增加違規次數 (每日重置) """
        today = get_now_tw().date()
        key = (chat_id, user_id)
        if key not in self.violation_tracker or self.violation_tracker[key]["last_date"].date() != today:
            self.violation_tracker[key] = {"count": 1, "last_date": get_now_tw()}
        else:
            self.violation_tracker[key]["count"] += 1
        
        self.save_state()
        return self.violation_tracker[key]["count"]

    def record_blacklist(self, user_id: int, name: str, chat_id: int, chat_title: str):
        """ 紀錄黑名單 """
        now = get_now_tw()
        key = f"{chat_id}_{user_id}"
        self.blacklist_members[key] = {
            "uid": user_id, "name": name, "chat_id": chat_id, 
            "chat_title": chat_title, "time": now
        }
        self.save_state()

    def reset_violation(self, chat_id: int, user_id: int):
        """ 清除違規與黑名單紀錄 (解封用) """
        v_key = (chat_id, user_id)
        bl_key = f"{chat_id}_{user_id}"
        if v_key in self.violation_tracker: self.violation_tracker[v_key]["count"] = 0
        if bl_key in self.blacklist_members: del self.blacklist_members[bl_key]
        self.save_state()

    def get_recent_blacklist(self, filter_chat_id: Optional[int] = None) -> List[Dict]:
        """ 獲取最近 24 小時內的黑名單 """
        now = get_now_tw()
        recent = []
        for key, info in self.blacklist_members.items():
            try:
                t = info.get("time")
                if not isinstance(t, datetime):
                     t = datetime.fromisoformat(t) if t else now
                if (now - t).total_seconds() < 86400: 
                    if filter_chat_id is None or info["chat_id"] == filter_chat_id:
                        recent.append(info)
            except: continue
        return sorted(recent, key=lambda x: x["time"], reverse=True)

    def get_blacklist_chats(self) -> Dict[int, str]:
        """ 取得有黑名單紀錄的群組清單 (供後台篩選用) """
        return {info["chat_id"]: info["chat_title"] for info in self.blacklist_members.values()}

config = BotConfig()

# ==========================================
# 4. 偵測與處理邏輯 (核心過濾算法)
# ==========================================

def is_domain_allowed(url: str) -> bool:
    """ 檢查連結是否在白名單 """
    try:
        extracted = tldextract.extract(url.strip().lower())
        return extracted.registered_domain in config.allowed_domains
    except: return False

def contains_prohibited_content(text: str) -> Tuple[bool, Optional[str]]:
    """ 檢查文字內容是否違規 (回傳: 是否違規, 原因) """
    if not text: return False, None
    
    # 步驟 1：文字淨化，移除所有空白、換行與隱形字元 (防駭客分割字串)
    clean_text = re.sub(r'\s+|\u200b|\u200c|\u200d|\ufeff', '', text)
    
    # 步驟 2：語言白名單限制 (嚴格檢查每一個字元)
    for char in clean_text:
        # 如果是字母/文字 (isalpha()=True 會排除標點、數字、Emoji)
        if char.isalpha():
            cp = ord(char)
            # 定義允許的 Unicode 範圍：英文, 泰文, 日文, 韓文, 漢字, 注音符號
            is_allowed_lang = (
                (0x0041 <= cp <= 0x005A) or (0x0061 <= cp <= 0x007A) or  # 英文 (A-Z, a-z)
                (0x0E00 <= cp <= 0x0E7F) or  # 泰文
                (0x3040 <= cp <= 0x30FF) or  # 日文 (平假名/片假名)
                (0xAC00 <= cp <= 0xD7A3) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F) or # 韓文
                (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0x20000 <= cp <= 0x2EBEF) or # 中文/漢字
                (0x3100 <= cp <= 0x312F) or  # 注音符號 (台灣常用)
                (0x31A0 <= cp <= 0x31BF) or  # 注音符號擴展
                (0x02B0 <= cp <= 0x02FF) or  # 聲調與間距修飾符號 (包含 ˉ, ˊ, ˇ, ˋ, ˙ 等注音聲調)
                (0xFF21 <= cp <= 0xFF3A) or (0xFF41 <= cp <= 0xFF5A)     # 全形英文字母
            )
            # 如果發現不屬於上述語系的字元 (如俄文、阿拉伯文、印尼文、越南文等)
            if not is_allowed_lang:
                return True, f"不允許的語言文字"

    # 步驟 3：關鍵字掃描
    for kw in config.blocked_keywords:
        if kw in text or kw in clean_text: 
            return True, f"關鍵字: {kw}"
            
    # 步驟 4：簡體字掃描
    if hanzidentifier.has_chinese(clean_text):
        for char in clean_text:
            # 絕對簡體字攔截
            if char in config.strict_simplified_chars:
                return True, f"禁語: {char}"
            # 傳統簡體字辨識 (單字過濾，防 Emoji 報錯)
            try:
                if hanzidentifier.is_simplified(char) and not hanzidentifier.is_traditional(char):
                    return True, f"簡體: {char}"
            except:
                continue
                
    return False, None

async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 處理 Telegram 群組內的 /unban 指令 """
    chat, admin_sender = update.effective_chat, update.effective_user
    try:
        # 檢查執行者是否為管理員
        member = await chat.get_member(admin_sender.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: return
        
        user_id = None
        mention = "未知用戶"
        
        # 判斷是指對回覆訊息解封，還是輸入 ID 解封
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
            # 給予全部權限
            p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
            await context.bot.restrict_chat_member(chat.id, user_id, p)
            config.reset_violation(chat.id, user_id)
            
            config.add_log("SUCCESS", f"🦋 管理員在 [{chat.title}] 指令解封 {user_id}")
            
            # 發送霍格華茲解禁通知
            msg = await update.message.reply_text(
                text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>",
                parse_mode=ParseMode.HTML
            )
            # 這裡不刪除訊息，保留紀錄
    except Exception as e: await update.message.reply_text(f"❌ 錯誤: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 處理所有進入群組的訊息 (核心過濾器) """
    config.last_heartbeat = get_now_tw()
    
    # [核心修復] 同時監聽「新訊息」與「被編輯過的訊息」
    msg = update.message or update.edited_message
    if not msg: return
    
    user = msg.from_user
    sender_chat = msg.sender_chat
    
    offender_id = None
    offender_name = "Unknown"
    mention_html = ""
    is_bot = False

    if user:
        offender_id = user.id
        offender_name = user.full_name
        is_bot = user.is_bot
        mention_html = user.mention_html()
    elif sender_chat:
        offender_id = sender_chat.id
        offender_name = sender_chat.title or "匿名頻道"
        is_bot = False
        mention_html = f"<b>{offender_name}</b>"
    else:
        return 

    if is_bot: return 

    # --- 1. 先提取所有文字與潛藏網址 ---
    all_texts: List[str] = []
    urls_to_check: List[str] = [] # 專門收集所有網址以供後續比對
    
    if msg.text: all_texts.append(msg.text)
    if msg.caption: all_texts.append(msg.caption)
    
    # 提取實體中的網址 (解決一般夾帶連結)
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in ents:
        if ent.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
            u = ent.url if ent.type == MessageEntity.TEXT_LINK else (msg.text or msg.caption)[ent.offset : ent.offset+ent.length]
            if u: 
                urls_to_check.append(u)
                all_texts.append(f"[實體連結]: {u}") # 強制讓連結在 Log 現形
    
    # [防禦核心] 提取透過 API 偷塞的「隱藏預覽網址」
    if msg.link_preview_options and msg.link_preview_options.url:
        hidden_url = msg.link_preview_options.url
        urls_to_check.append(hidden_url)
        all_texts.append(f"[隱藏預覽]: {hidden_url}") # 強制讓預覽來源現形
        
    if msg.via_bot:
        all_texts.append(f"[呼叫機器人]: @{msg.via_bot.username}")

    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat:
            src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user:
            src_name = msg.forward_origin.sender_user.full_name
        if src_name: all_texts.append(src_name)

    # 提取名片姓名
    if msg.contact:
        if msg.contact.first_name: all_texts.append(msg.contact.first_name)
        if msg.contact.last_name: all_texts.append(msg.contact.last_name)
    
    # 提取地點名稱與地址
    if msg.venue:
        if msg.venue.title: all_texts.append(msg.venue.title)
        if msg.venue.address: all_texts.append(msg.venue.address)

    # 提取貼圖標題
    if msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            all_texts.append(s_set.title)
        except: pass
    
    # 提取按鈕文字
    if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if hasattr(btn, 'text'): all_texts.append(btn.text)
    
    # 提取投票內容
    if msg.poll:
        all_texts.append(msg.poll.question)
        for opt in msg.poll.options: all_texts.append(opt.text)
        
    # 提取引用內容
    quote = getattr(msg, 'quote', None)
    if quote:
        if hasattr(quote, 'text') and quote.text: all_texts.append(quote.text)
        if hasattr(quote, 'caption') and quote.caption: all_texts.append(quote.caption)

    # 記錄 Log (加入編輯標示，方便查閱，並確保空訊息也有提示)
    is_edit_tag = " (編輯訊息)" if update.edited_message else ""
    full_content_log = " | ".join(all_texts)
    
    if not full_content_log:
        if msg.sticker: full_content_log = f"<貼圖: {msg.sticker.set_name}>"
        elif msg.photo or msg.video or msg.animation or msg.document: full_content_log = "<媒體檔案>"
        else: full_content_log = "<無文字內容>"

    config.add_log("INFO", f"[{msg.chat.title}] [{offender_name}]{is_edit_tag} 偵測: {full_content_log[:150]}...")

    # --- 3. 管理員與 VIP 豁免檢查 ---
    if user:
        # 檢查是否為 戰鬥群夥伴 VIP 白名單用戶
        if user.id in config.exempt_user_ids:
            config.add_log("SYSTEM", f"戰鬥群夥伴 {offender_name} 豁免，不執行攔截")
            return

        # 檢查是否為群組管理員
        try:
            if msg.chat.type != "private":
                cm = await msg.chat.get_member(user.id)
                # 判斷是否為管理員或群主
                if cm.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: 
                    config.add_log("SYSTEM", f"管理員 {offender_name} 豁免，不執行攔截")
                    return 
        except: pass

    if msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete(); return
        except: pass

    violation_reason: Optional[str] = None

    # --- 4. 開始執行各項檢查 ---
    
    # A. 轉傳來源檢查
    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat:
             src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user:
             src_name = msg.forward_origin.sender_user.full_name
        
        if src_name:
             is_bad, r = contains_prohibited_content(src_name)
             if is_bad: violation_reason = f"轉傳來源違規 ({src_name})"

    # B. 電話號碼檢查
    if not violation_reason and msg.contact:
        phone = msg.contact.phone_number or ""
        clean_phone = re.sub(r'[+\-\s]', '', phone)
        blocked_clean = [re.sub(r'[+\-\s]', '', p) for p in config.blocked_phone_prefixes]
        if any(clean_phone.startswith(pre) for pre in blocked_clean if pre):
            violation_reason = f"來自受限國家門號 ({phone[:3]}...)"

    # C. 貼圖檢查 (白名單 ID)
    if not violation_reason and msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            combined_lower = (s_set.title + msg.sticker.set_name).lower()
            if ("@" in combined_lower or "_by_" in combined_lower):
                if not any(wd in combined_lower for wd in config.sticker_whitelist):
                    safe_title = s_set.title.replace("@", "")
                    violation_reason = f"未授權 ID ({safe_title})"
        except: pass

    # D. 全文內容掃描 (核心)
    if not violation_reason:
        unique_texts = list(set(all_texts))
        for t in unique_texts:
            is_bad, r = contains_prohibited_content(t)
            if is_bad: violation_reason = r; break

    # [關鍵防禦] 嚴格驗證所有潛藏的 URL (包含實體連結與隱藏預覽連結)
    if not violation_reason:
        for u in urls_to_check:
            u_clean = u.strip().lower()
            
            # 一般網域檢查
            if not is_domain_allowed(u_clean):
                violation_reason = f"不明連結 ({u_clean[:30]}...)"
                break
            
            # Telegram 連結檢查 (涵蓋所有官方縮網址分身)
            tg_domains = ["t.me/", "telegram.me/", "telegram.dog/"]
            for tg_domain in tg_domains:
                if tg_domain in u_clean:
                    path = u_clean.split(tg_domain)[-1].split('/')[0].split('?')[0].replace("@", "")
                    # 如果路徑不在白名單「完全相等」的項目中，就攔截
                    if path and path not in config.telegram_link_whitelist:
                        violation_reason = f"未授權 TG 連結 ({path})"
                    break
                    
            if violation_reason:
                break

    # --- 5. 執行懲罰動作 ---
    if violation_reason:
        if msg.media_group_id: config.flagged_media_groups[msg.media_group_id] = datetime.now()
        try:
            # 刪除違規訊息
            try: await msg.delete(); config.total_deleted_count += 1
            except: pass
            
            # 增加違規計數
            v_count = config.add_violation(msg.chat.id, offender_id)
            
            # 達到上限 -> 封鎖 + 公告
            if v_count >= config.max_violations:
                try: 
                    if user:
                        await context.bot.restrict_chat_member(msg.chat.id, user.id, ChatPermissions(can_send_messages=False))
                    elif sender_chat:
                        await context.bot.ban_chat_sender_chat(msg.chat.id, sender_chat.id)
                except: config.add_log("WARN", f"[{msg.chat.title}] 技術禁言失敗")
                
                config.record_blacklist(offender_id, offender_name, msg.chat.id, msg.chat.title)
                config.add_log("ERROR", f"🦋 {offender_name} 在 [{msg.chat.title}] 違規達上限，封鎖入阿茲卡班")
                
                # 霍格華茲禁言通知
                await context.bot.send_message(
                    chat_id=msg.chat.id, 
                    text=f"🦋 <b>霍格華茲禁言通知</b> 🦋\n\n🦉用戶學員：{mention_html}\n🈲發言已多次違反校規。\n🈲已被咒語《阿哇呾喀呾啦》擊殺⚡️\n🪄<b>如被誤殺請待在阿茲卡班內稍等\n並請客服通知鄧不利多校長幫你解禁</b>", 
                    parse_mode=ParseMode.HTML
                )
            else:
                # 未達上限 -> 警告 (定時銷毀)
                sent_warn = await context.bot.send_message(msg.chat.id, f"🦋 <b>霍格華茲警告通知</b> 🦋\n\n🦉用戶學員：{mention_html}\n⚠️違反校規：{violation_reason}\n⚠️違規計次：({v_count}/{config.max_violations})\n🪄<b>多次違規將被黑魔法教授擊殺</b>", parse_mode=ParseMode.HTML)
                await asyncio.sleep(config.warning_duration); await sent_warn.delete()
        except: pass
    elif msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete()
        except: pass
    elif not msg.sticker:
        # 更新日誌顯示
        full_log_text = " | ".join(all_texts)
        config.add_log("INFO", f"[{msg.chat.title}] [{offender_name}] 全文掃描: {full_log_text[:50]}...")


# ==========================================
# 5. Flask 後台管理網頁路由
# ==========================================
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
        config.save_state()
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
        
        # 顯示處理：如果是頻道 (ID < 0)，顯示名稱，否則顯示連結
        mention = f"<b>{user_name}</b>" if user_id < 0 else f'<a href="tg://user?id={user_id}">{user_name}</a>'
        
        async def do_unban():
            try:
                # 執行解禁
                p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
                
                if user_id > 0:
                    await config.application.bot.restrict_chat_member(chat_id, user_id, p)
                    await config.application.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                else:
                    await config.application.bot.unban_chat_sender_chat(chat_id, user_id)
                
                config.reset_violation(chat_id, user_id)
                config.add_log("SUCCESS", f"🦋 網頁解封 {user_name}，地點 [{member_data.get('chat_title')}]")
                
                # 發送解禁通知
                await config.application.bot.send_message(
                    chat_id=chat_id, 
                    text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>", 
                    parse_mode=ParseMode.HTML
                )
            except Exception as e: config.add_log("ERROR", f"🦋 解封失敗: {e}")
        if config.loop: asyncio.run_coroutine_threadsafe(do_unban(), config.loop)
    except: pass
    return redirect(url_for('index'))

# --- 6. HTML 模板 (後台介面) ---
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
                        <h3 class="text-lg font-bold text-rose-400">🚫 阿茲卡班監獄</h3>
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
    # 啟動時讀取本地存檔或雲端資料
    config.load_state()
    try:
        bot_app = ApplicationBuilder().token(config.bot_token).build(); config.application = bot_app 
        async def clear(): 
            try: await bot_app.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            config.add_log("INFO", "🦋 Telegram 通訊連線成功，系統已準備就緒。")
        loop.run_until_complete(clear())
        bot_app.add_handler(CommandHandler("unban", unban_handler))
        
        # [關鍵修復] 同時攔截「一般訊息」與「編輯過的訊息」
        bot_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
        bot_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & (~filters.COMMAND), handle_message))
        
        bot_app.run_polling(stop_signals=False, close_loop=False)
    except Exception as e: config.add_log("ERROR", f"🦋 核心崩潰: {e}")

if __name__ == '__main__':
    tg_thread = Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()
    serve(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
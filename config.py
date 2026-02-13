import os
import logging
import json
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional
from threading import Thread

# --- 系統日誌與時區設定 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TW_TZ = timezone(timedelta(hours=8))

def get_now_tw():
    """ 取得目前的台灣時間 """
    return datetime.now(timezone.utc).astimezone(TW_TZ)

# --- 雲端資料庫管理 (Firestore REST) ---
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

# --- 全域配置與狀態儲存 ---
class BotConfig:
    def __init__(self):
        self.bot_token = os.getenv("TG_BOT_TOKEN")
        self.application = None 
        self.loop = None        
        self.db = FirestoreManager()
        
        # 預設規則
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
        
        # 戰鬥群夥伴 VIP 豁免名單 (這些 ID 不受檢查)
        self.exempt_user_ids = {
            7363979036, 6168587103, 6660718633, 5152410443,
            1121824397, 739962535, 6176254570, 5074058687,
            7597693349, 835207824, 7716513113
        }

        self.blocked_phone_prefixes = {
            "+91", "+95", "+60", "+62", "+855", "+84", "+44", "+86", "+41"
        }
        
        # 關鍵字黑名單
        self.blocked_keywords = {
            # 詐騙/博弈
            "假钞", "捡钱", "项目", "電報", "@xsm77788", "君临",
            "挣米", "日赚", "回款", "上压", "担保", "兼职", "手气",
            "风口", "一单", "博彩", "彩票", "赛车", "飞艇", "哈希",
            "百家乐", "投资", "USDT", "TRX", "包过", "洗米", "跑分",
            "现场", "连连", "满", "澳门", "新澳"
            # 個資/黑產
            "查档", "身份证", "户籍", "开房", "手机号", "机主", 
            "轨迹", "车队", "入款", "出款",
            # 色情/引流
            "迷药", "春药", "裸聊", "极品", "强奸", "销魂", 
            "约炮", "同城", "资源", "人兽", "皮肤", "萌酱",
            "萝莉", "爆炒", "做坏事", "蜜桃臀", "路边", "坏事", 
            # 簡體高頻詞
            "置顶", "软件", "下载", "点击", "链接", "免费观看", "点击下方",
            #用繁體躲殺
            "普通人也能做"
        }

        # 絕對簡體字表 (加入截圖中的 临, 宫, 际, 务, 员)
        self.strict_simplified_chars = {
            "国", "会", "发", "现", "关", "质", "员", "机", "产", "气", 
            "实", "则", "两", "结", "营", "报", "种", "专", "务", "战",
            "风", "让", "钱", "变", "间", "给", "号", "图", "亲", "极",
            "点", "击", "库", "车", "东", "应", "库", "启", "书", "评",
            "无", "马", "过", "办", "证", "听", "说", "话", "频", "视",
            "户", "罗", "边", "观", "么", "开", "区", "帅", "费",
            "临", "宫", "际", "备", "饭"
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
                if (now - t).total_seconds() < 86400: # 24小時
                    if filter_chat_id is None or info["chat_id"] == filter_chat_id:
                        recent.append(info)
            except: continue
        return sorted(recent, key=lambda x: x["time"], reverse=True)

    def get_blacklist_chats(self) -> Dict[int, str]:
        """ 取得有黑名單紀錄的群組清單 (供後台篩選用) """
        return {info["chat_id"]: info["chat_title"] for info in self.blacklist_members.values()}

# 初始化設定實例
config = BotConfig()
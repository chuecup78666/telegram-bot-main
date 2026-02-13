import os
import logging
import asyncio
import re
from threading import Thread
from typing import Tuple, Optional, List

# --- 引用模組 ---
# 從 config.py 引入設定、工具函式與 Logger
from config import config, get_now_tw, logger
# 從 dashboard.py 引入 Flask app
from dashboard import app  
# 引入生產環境伺服器
from waitress import serve

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

# ==========================================
# 4. 偵測與處理邏輯 (核心過濾算法)
# ==========================================

def is_domain_allowed(url: str) -> bool:
    try:
        extracted = tldextract.extract(url.strip().lower())
        return extracted.registered_domain in config.allowed_domains
    except: return False

def contains_prohibited_content(text: str) -> Tuple[bool, Optional[str]]:
    if not text: return False, None
    
    # 1. 關鍵字攔截 (優先級最高)
    for kw in config.blocked_keywords:
        if kw in text: return True, f"關鍵字: {kw}"

    # 2. 絕對簡體字表
    for char in text:
        if char in config.strict_simplified_chars:
            return True, f"禁語: {char}"

    # 3. 傳統簡體字庫偵測
    try:
        if hanzidentifier.has_chinese(text):
            for char in text:
                if hanzidentifier.is_simplified(char) and not hanzidentifier.is_traditional(char):
                    return True, f"簡體: {char}"
    except: pass
    return False, None

async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 處理 Telegram 群組內的 /unban 指令 """
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
            # 給予全部權限
            p = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True, can_change_info=True)
            await context.bot.restrict_chat_member(chat.id, user_id, p)
            config.reset_violation(chat.id, user_id)
            
            config.add_log("SUCCESS", f"🦋 管理員在 [{chat.title}] 指令解封 {user_id}")
            
            # 發送霍格華茲解禁通知 (不刪除)
            msg = await update.message.reply_text(
                text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>",
                parse_mode=ParseMode.HTML
            )
    except Exception as e: await update.message.reply_text(f"❌ 錯誤: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 處理所有進入群組的訊息 (核心過濾器) """
    config.last_heartbeat = get_now_tw()
    if not update.message: return
    msg = update.message
    
    # 獲取發送者資訊
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

    # --- 1. 提取所有文字內容 (合併掃描) ---
    all_texts: List[str] = []
    if msg.text: all_texts.append(msg.text)
    if msg.caption: all_texts.append(msg.caption)
    
    if msg.forward_origin:
        src_name = ""
        if hasattr(msg.forward_origin, 'chat') and msg.forward_origin.chat:
            src_name = msg.forward_origin.chat.title
        elif hasattr(msg.forward_origin, 'sender_user') and msg.forward_origin.sender_user:
            src_name = msg.forward_origin.sender_user.full_name
        if src_name: all_texts.append(src_name)

    if msg.contact:
        if msg.contact.first_name: all_texts.append(msg.contact.first_name)
        if msg.contact.last_name: all_texts.append(msg.contact.last_name)
    
    if msg.venue:
        if msg.venue.title: all_texts.append(msg.venue.title)
        if msg.venue.address: all_texts.append(msg.venue.address)

    if msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            all_texts.append(s_set.title)
        except: pass
    
    if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if hasattr(btn, 'text'): all_texts.append(btn.text)
    
    if msg.poll:
        all_texts.append(msg.poll.question)
        for opt in msg.poll.options: all_texts.append(opt.text)
        
    quote = getattr(msg, 'quote', None)
    if quote:
        if hasattr(quote, 'text') and quote.text: all_texts.append(quote.text)
        if hasattr(quote, 'caption') and quote.caption: all_texts.append(quote.caption)

    # --- 2. 記錄 Log ---
    full_content_log = " | ".join(all_texts)
    config.add_log("INFO", f"[{msg.chat.title}] [{offender_name}] 偵測: {full_content_log[:50]}...")

    # --- 3. 管理員與 VIP 豁免檢查 ---
    if user:
        # VIP 豁免
        if user.id in config.exempt_user_ids:
            config.add_log("SYSTEM", f"VIP 用戶 {offender_name} 豁免，不執行攔截")
            return

        # 管理員豁免
        try:
            if msg.chat.type != "private":
                cm = await msg.chat.get_member(user.id)
                if cm.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]: 
                    config.add_log("SYSTEM", f"管理員 {offender_name} 豁免，不執行攔截")
                    return 
        except: pass

    if msg.media_group_id and msg.media_group_id in config.flagged_media_groups:
        try: await msg.delete(); return
        except: pass

    violation_reason: Optional[str] = None

    # --- 4. 執行檢查 ---
    
    # 轉傳來源
    if msg.forward_origin:
        if src_name:
            is_bad_src, src_reason = contains_prohibited_content(src_name)
            if is_bad_src: violation_reason = f"轉傳來源違規 ({src_name})"

    # 電話號碼
    if not violation_reason and msg.contact:
        phone = msg.contact.phone_number or ""
        clean_phone = re.sub(r'[+\-\s]', '', phone)
        blocked_clean = [re.sub(r'[+\-\s]', '', p) for p in config.blocked_phone_prefixes]
        if any(clean_phone.startswith(pre) for pre in blocked_clean if pre):
            violation_reason = f"來自受限國家門號 ({phone[:3]}...)"

    # 貼圖白名單
    if not violation_reason and msg.sticker:
        try:
            s_set = await context.bot.get_sticker_set(msg.sticker.set_name)
            combined_lower = (s_set.title + msg.sticker.set_name).lower()
            if ("@" in combined_lower or "_by_" in combined_lower):
                if not any(wd in combined_lower for wd in config.sticker_whitelist):
                    safe_title = s_set.title.replace("@", "")
                    violation_reason = f"未授權 ID ({safe_title})"
        except: pass

    # 全文掃描
    if not violation_reason:
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
                    violation_reason = "不明連結"; break
                
                if "t.me/" in u_clean:
                    path = u_clean.split('t.me/')[-1].split('/')[0].split('?')[0].replace("@", "")
                    if path and not any(wl in path for wl in config.telegram_link_whitelist):
                        violation_reason = f"未授權 TG 連結 ({path})"; break

    # --- 5. 懲罰執行 (關鍵修正：確保警告發出) ---
    if violation_reason:
        if msg.media_group_id: config.flagged_media_groups[msg.media_group_id] = datetime.now()
        
        # 步驟 A: 嘗試刪除 (失敗不中斷)
        try: 
            await msg.delete()
            config.total_deleted_count += 1
        except: 
            # 可能是管理員測試或機器人權限不足
            config.add_log("WARN", f"無法刪除 [{offender_name}] 的違規訊息")

        # 步驟 B: 計算違規並處置
        v_count = config.add_violation(msg.chat.id, offender_id)
        
        # 情況 1: 達標封鎖
        if v_count >= config.max_violations:
            try: 
                if user:
                    await context.bot.restrict_chat_member(msg.chat.id, user.id, ChatPermissions(can_send_messages=False))
                elif sender_chat:
                    await context.bot.ban_chat_sender_chat(msg.chat.id, sender_chat.id)
            except Exception as e: 
                config.add_log("WARN", f"[{msg.chat.title}] 禁言指令執行失敗: {e}")
            
            # 紀錄黑名單
            config.record_blacklist(offender_id, offender_name, msg.chat.id, msg.chat.title)
            config.add_log("ERROR", f"🦋 {offender_name} 在 [{msg.chat.title}] 違規達上限，封鎖入阿茲卡班")
            
            # 發送禁言公告
            await context.bot.send_message(
                chat_id=msg.chat.id, 
                text=f"🚫 🦋<b>用戶禁言通知</b>🦋\n用戶：{mention_html}\n原因：多次違規。\n狀態：已被咒語《阿哇呾喀呾啦》擊殺，關入阿茲卡班。", 
                parse_mode=ParseMode.HTML
            )
        
        # 情況 2: 未達標警告
        else:
            # 發送警告通知
            sent_warn = await context.bot.send_message(
                chat_id=msg.chat.id, 
                text=f"⚠️ 🦋 <b>霍格華茲警告通知</b> 🦋\n\n🦉用戶學員：{mention_html}\n⚠️違反校規：{violation_reason}\n⚠️違規計次：({v_count}/{config.max_violations})\n🪄<b>多次違規將被黑魔法教授擊殺</b>", 
                parse_mode=ParseMode.HTML
            )
            # 延遲刪除警告
            await asyncio.sleep(config.warning_duration)
            try: await sent_warn.delete()
            except: pass

# --- 6. 啟動區塊 ---
def run_telegram_bot():
    if not config.bot_token: return
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); config.loop = loop 
    # 啟動時讀取存檔
    config.load_state()
    try:
        bot_app = ApplicationBuilder().token(config.bot_token).build(); config.application = bot_app 
        async def clear(): 
            try: await bot_app.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            config.add_log("INFO", "🦋 Telegram 通訊連線成功")
        loop.run_until_complete(clear())
        bot_app.add_handler(CommandHandler("unban", unban_handler))
        bot_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
        bot_app.run_polling(stop_signals=False, close_loop=False)
    except Exception as e: config.add_log("ERROR", f"🦋 核心崩潰: {e}")

if __name__ == '__main__':
    # 啟動機器人執行緒
    tg_thread = Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()
    
    # 啟動 Waitress 生產環境伺服器
    port = int(os.environ.get("PORT", 10000))
    serve(app, host='0.0.0.0', port=port)
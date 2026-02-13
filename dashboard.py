from flask import Flask, render_template_string, request, redirect, url_for
from telegram import ChatPermissions, ParseMode
import asyncio
from config import config, get_now_tw, logger

app = Flask(__name__)

# --- HTML 模板 ---
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
        config.save_state() # 立即存檔 (Render重啟會調用)
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
                await config.application.bot.restrict_chat_member(chat.id, user_id, p); await config.application.bot.unban_chat_member(chat.id, user_id, only_if_banned=True)
                config.reset_violation(chat.id, user_id)
                config.add_log("SUCCESS", f"🦋 網頁解封 {user_name}，地點 [{member_data.get('chat_title')}]")
                n_msg = await config.application.bot.send_message(
                    chat_id=chat_id, 
                    text=f"🦋 <b>霍格華茲解禁通知</b> 🦋\n🦉用戶學員：{mention}\n✅經由魔法部審判為無罪\n✅已被鳳凰的眼淚治癒返校\n🪄<b>請學員注意勿再違反校規</b>", 
                    parse_mode=ParseMode.HTML
                )
                # 不刪除解封訊息
            except Exception as e: config.add_log("ERROR", f"🦋 解封失敗: {e}")
        if config.loop: asyncio.run_coroutine_threadsafe(do_unban(), config.loop)
    except: pass
    return redirect(url_for('index'))
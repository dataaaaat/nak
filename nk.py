# --- REAL FIX FIRST ---
#   pip uninstall pyrogram -y
#   pip install kurigram tgcrypto -U
#   pip install pytgcalls -U

# --- SAFETY-NET PATCH ---
import pyrogram.errors

_legacy_error_names = [
    "GroupcallForbidden",
    "GroupcallInvalid",
    "GroupcallSsrcDuplicateMuted",
    "GroupCallInvalid",
    "GroupCallForbidden",
    "GroupCallJoinMissing",
    "GroupCallJoinMissingInvalid",
]
for _name in _legacy_error_names:
    if not hasattr(pyrogram.errors, _name):
        setattr(pyrogram.errors, _name, type(_name, (Exception,), {}))
# --------------------------------------

import asyncio
import json
import os
import subprocess
import uuid
import time
from pyrogram import Client, filters, idle
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid, UserAlreadyParticipant, FloodWait, InviteRequestSent
from pyrogram.raw.functions.phone import ToggleGroupCallRecord
from pyrogram.raw.functions.channels import GetAdminedPublicChannels
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw.functions.messages import GetFullChat
from pyrogram.raw.types import InputGroupCall
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web
from pytgcalls import PyTgCalls

# --- UNIVERSAL PYTGCALLS COMPATIBILITY (v2/v3 "MediaStream" vs legacy) ---
try:
    from pytgcalls.types import MediaStream
    IS_V3 = True
    # Quality enums moved around between pytgcalls releases, so import
    # defensively instead of assuming one fixed path.
    try:
        from pytgcalls.types import AudioQuality, VideoQuality
    except ImportError:
        try:
            from pytgcalls.types.stream import AudioQuality, VideoQuality
        except ImportError:
            AudioQuality = None
            VideoQuality = None
except ImportError:
    from pytgcalls.types.input_stream import AudioPiped, AudioVideoPiped
    try:
        from pytgcalls.types.input_stream.quality import HighQualityAudio, HighQualityVideo
    except ImportError:
        HighQualityAudio = None
        HighQualityVideo = None
    IS_V3 = False


def get_stream_object(file_path, stream_type):
    """
    Build the correct stream object for audio-only vs audio+video.

    Root cause of "video nahi chal raha" in the old code: the audio branch
    explicitly told pytgcalls to IGNORE the video track, which is correct,
    but the video branch never told it to explicitly REQUIRE/detect both
    tracks with real quality settings - on some pytgcalls builds that means
    the video m-line never gets negotiated and the stream silently falls
    back to audio-only. We now always pass explicit quality parameters and
    explicit flags for both tracks.
    """
    if IS_V3:
        if stream_type == "audio":
            kwargs = {"video_flags": MediaStream.Flags.IGNORE}
            if AudioQuality is not None:
                kwargs["audio_parameters"] = AudioQuality.STUDIO
            return MediaStream(file_path, **kwargs)
        else:
            kwargs = {
                "audio_flags": MediaStream.Flags.AUTO_DETECT,
                "video_flags": MediaStream.Flags.AUTO_DETECT,
            }
            if AudioQuality is not None:
                kwargs["audio_parameters"] = AudioQuality.STUDIO
            if VideoQuality is not None:
                kwargs["video_parameters"] = VideoQuality.SD_480p
            return MediaStream(file_path, **kwargs)
    else:
        if stream_type == "audio":
            if HighQualityAudio is not None:
                return AudioPiped(file_path, HighQualityAudio())
            return AudioPiped(file_path)
        else:
            if HighQualityAudio is not None and HighQualityVideo is not None:
                return AudioVideoPiped(file_path, HighQualityAudio(), HighQualityVideo())
            return AudioVideoPiped(file_path)


# --- CONFIGURATION ---
API_ID = "31879900"
API_HASH = "3238af8628b9134484a19cca430a962c"
BOT_TOKEN = "8990543629:AAHepSzM7UVucoAGG6HZLPOv8C0FNAzxO4U"
VPS_IP = "157.245.200.50"
PORT = 443
DB_FILE = "users_data.json"
ADMIN_ID = "7659712445"

app = Client("live_stream_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

login_tokens = {}
login_state = {}

# States & Active Data
user_steps = {}
active_clients = {}
record_tasks = {}

# --- DATABASE MANAGEMENT ---
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            if "banned_users" not in data:
                data["banned_users"] = []
            return data
    return {"banned_users": []}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def is_banned(user_id, db):
    return str(user_id) in db.get("banned_users", [])

def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB'}
    while size > power:
        size /= power
        n += 1
    return f"{round(size, 2)} {power_labels[n]}"

def format_time(seconds):
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{int(hours):02}:{int(mins):02}:{int(secs):02}"
    return f"{int(mins):02}:{int(secs):02}"

def get_media_duration(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
        )
        return float(result.stdout.strip())
    except Exception:
        return 0

# --- HELPER FUNCTIONS / IDLE PLACEHOLDER STREAMS ---
SILENCE_FILE = "silence.mp3"
SILENCE_VIDEO_FILE = "silence_video.mp4"

async def ensure_silence_file():
    """
    Pure-audio silence, used to keep an AUDIO session alive while idle.
    Runs ffmpeg in a background thread (asyncio.to_thread) so it can never
    block the bot's event loop - a blocking subprocess.run() call here was
    one of the causes of the bot appearing "stuck" and ignoring /start.
    """
    if not os.path.exists(SILENCE_FILE):
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", "3600", "-q:a", "9", "-acodec", "libmp3lame", SILENCE_FILE],
            True, subprocess.DEVNULL, subprocess.DEVNULL  # check, stdout, stderr
        )
    return SILENCE_FILE

async def ensure_silence_video_file():
    """
    Silent BLACK VIDEO, used to keep a VIDEO session alive while idle.

    This matters: if a video session joins/idles on an audio-only stream,
    the call connection is negotiated without a video track at all, so a
    real video played afterwards has nothing to attach to and never shows
    up on screen for other participants. Idling on a silent black video
    keeps the video track alive so real videos display correctly.

    Also runs via asyncio.to_thread - encoding an hour of video can take
    a while on a small VPS, and doing it synchronously would freeze the
    entire bot (including /start) until it finished.
    """
    if not os.path.exists(SILENCE_VIDEO_FILE):
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", "3600", "-c:v", "libx264", "-tune", "stillimage",
             "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest", SILENCE_VIDEO_FILE],
            True, subprocess.DEVNULL, subprocess.DEVNULL  # check, stdout, stderr
        )
    return SILENCE_VIDEO_FILE

async def get_idle_stream_source():
    # Always join/idle on a silent VIDEO placeholder. This keeps a video
    # track negotiated on the call at all times, so it doesn't matter
    # whether the session started as "Audio Live" or "Video Live" - a real
    # video sent later will always have a track to render on, and a real
    # audio file will simply play over the black idle screen.
    return await ensure_silence_video_file()

async def _mute(call_app, chat_id):
    for name in ("mute_stream", "mute"):
        method = getattr(call_app, name, None)
        if method: return await method(chat_id)

async def _unmute(call_app, chat_id):
    for name in ("unmute_stream", "unmute"):
        method = getattr(call_app, name, None)
        if method: return await method(chat_id)

async def _pause(call_app, chat_id):
    for name in ("pause_stream", "pause"):
        method = getattr(call_app, name, None)
        if method: return await method(chat_id)

async def _resume(call_app, chat_id):
    for name in ("resume_stream", "resume"):
        method = getattr(call_app, name, None)
        if method: return await method(chat_id)

async def sync_mic_state(data, chat_id, should_be_unmuted):
    """
    Single place that decides mic state, so 'muted while idle / unmuted
    while something is actually playing' is always consistent and never
    drifts between the various call sites.
    """
    call_app = data.get("call")
    if not call_app or not chat_id:
        return
    try:
        if should_be_unmuted:
            await _unmute(call_app, chat_id)
        else:
            await _mute(call_app, chat_id)
        data["is_muted"] = not should_be_unmuted
    except Exception:
        pass

def detect_media_kind(message, fallback="audio"):
    """
    Decide audio vs video from the ACTUAL file the user sent, instead of
    trusting whatever "Audio Live / Video Live" button they clicked at the
    start. This is what fixes "video bhejta hoon to audio ki tarah chalta
    hai" - previously every queued file inherited one fixed session-wide
    type, so a video sent during an "Audio Live" session was silently
    stripped down to audio-only.
    """
    if message.video:
        return "video"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return fallback
    if message.audio or message.voice:
        return "audio"
    return fallback

async def _force_teardown_session(phone):
    """
    Fully tear down a phone's session (leave call, stop PyTgCalls, stop the
    Pyrogram client) and remove it from active_clients so the NEXT join
    builds a completely fresh Client + PyTgCalls from scratch.

    Why this matters: reusing the same PyTgCalls instance after it leaves a
    call is a well-known way for its internal native state to get stuck,
    where the next play() call never resolves - and because that hang can
    block the event loop, even unrelated things like /start stop
    responding. Always rebuilding fresh after a stop/failure is the fix.
    """
    data = active_clients.pop(phone, None)
    if not data:
        return
    if data.get("timer_task"):
        try: data["timer_task"].cancel()
        except Exception: pass

    call_app = data.get("call")
    user_app = data.get("app")
    chat_id = data.get("chat_id")

    try:
        if call_app and chat_id:
            if hasattr(call_app, "leave_group_call"):
                await asyncio.wait_for(call_app.leave_group_call(chat_id), timeout=10)
            elif hasattr(call_app, "leave_call"):
                await asyncio.wait_for(call_app.leave_call(chat_id), timeout=10)
    except Exception:
        pass

    try:
        if call_app and hasattr(call_app, "stop"):
            await asyncio.wait_for(call_app.stop(), timeout=10)
    except Exception:
        pass

    try:
        if user_app:
            await asyncio.wait_for(user_app.stop(), timeout=10)
    except Exception:
        pass

async def get_input_group_call(user_client, chat_id):
    peer = await user_client.resolve_peer(chat_id)
    try:
        if hasattr(peer, "channel_id"):
            full = await user_client.invoke(GetFullChannel(channel=peer))
            call = full.full_chat.call
        else:
            full = await user_client.invoke(GetFullChat(chat_id=peer.chat_id))
            call = full.full_chat.call
        if call:
            return InputGroupCall(id=call.id, access_hash=call.access_hash)
    except Exception:
        pass
    return None

def generate_login_link(user_id):
    token = str(uuid.uuid4())
    login_tokens[token] = {"user_id": str(user_id), "expires": time.time() + 600}
    return f"http://{VPS_IP}:{PORT}/login?token={token}"

async def show_main_menu(message_or_query, user_id, db):
    user_data = db.get(str(user_id), {})
    active_phone = user_data.get("active_account")
    if not active_phone or active_phone not in user_data.get("accounts", {}):
        return False

    account_info = user_data["accounts"][active_phone]
    login_time = account_info.get("login_time")

    if login_time:
        time_str = time.strftime('%d-%b-%Y at %I:%M %p', time.localtime(login_time))
    else:
        time_str = "Time not recorded"

    keyboard_buttons = [
        [InlineKeyboardButton("➜ Login More", callback_data="login_more"),
         InlineKeyboardButton("➜ Switch Account", callback_data="switch_account")],
        [InlineKeyboardButton("➜ Audio Live", callback_data="audio_live"),
         InlineKeyboardButton("➜ Video Live", callback_data="video_live")]
    ]

    if str(user_id) == ADMIN_ID:
        keyboard_buttons.append([InlineKeyboardButton("➜ Admin Menu", callback_data="admin_menu")])

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    text = (
        "➜ **MAIN MENU** ➜\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"➜ Active Account: `{active_phone}`\n"
        "➜ Status: Successfully Linked ✅\n"
        f"➜ Account Added On: {time_str}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➜ Select an option below to start streaming."
    )

    if isinstance(message_or_query, CallbackQuery):
        await message_or_query.message.edit_text(text, reply_markup=keyboard)
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard)
    return True

# --- TRUE LAZY-LOAD QUEUE & PLAYER ENGINE ---
async def update_player_ui(phone):
    data = active_clients.get(phone)
    if not data or not data.get("ui_message"): return

    current = data.get("current")
    q_len = len(data.get("queue", []))
    is_repeat = data.get("repeat", False)
    chat_id = data.get("chat_id")
    mic_icon = "🔇 Muted" if data.get("is_muted", True) else "🔊 Live"

    if current:
        status = (
            "➜ **STREAM PANEL** ➜\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"➜ Now Playing: {current['name']}\n"
            f"➜ Type: {current['type'].upper()}\n"
            f"➜ Length: {format_time(current['duration'])}\n"
            f"➜ In Queue: {q_len} media left\n"
            f"➜ Mic: {mic_icon}\n"
            f"➜ Mode: {'Repeat ON' if is_repeat else 'Auto-Play Next'}"
        )
    else:
        status = (
            "➜ **STREAM PANEL** ➜\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"➜ Stream is Idle. Mic: {mic_icon}\n"
            f"➜ In Queue: {q_len} media left.\n"
            "➜ Send more audio/video to play."
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➜ Pause", callback_data=f"pause_{chat_id}"),
         InlineKeyboardButton("➜ Resume", callback_data=f"resume_{chat_id}")],
        [InlineKeyboardButton(f"➜ Repeat: {'[ON]' if is_repeat else '[OFF]'}", callback_data=f"repeat_{chat_id}"),
         InlineKeyboardButton("➜ Skip Next", callback_data=f"skip_{chat_id}")],
        [InlineKeyboardButton("➜ Record VC", callback_data=f"record_{chat_id}")],
        [InlineKeyboardButton("➜ Stop Stream", callback_data=f"stop_{chat_id}")]
    ])

    try:
        await data["ui_message"].edit_text(status, reply_markup=keyboard)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

async def media_timer_worker(phone, duration):
    """Timer that waits precisely for the media to finish, then triggers next queued item."""
    try:
        elapsed = 0
        total_wait = duration + 1.5
        while elapsed < total_wait:
            await asyncio.sleep(1)
            data = active_clients.get(phone)
            if not data: return
            if not data.get("is_paused", False):
                elapsed += 1

        # Stream has naturally finished, triggering next in queue automatically
        asyncio.create_task(play_next(phone))
    except asyncio.CancelledError:
        pass

async def play_next(phone):
    data = active_clients.get(phone)
    if not data: return
    if data.get("is_downloading"): return  # Agar pehle se download chal raha hai, to 2nd na ho

    call_app = data["call"]
    user_app = data["app"]
    chat_id = data.get("chat_id")
    join_as_peer = data.get("join_as_peer")
    if not chat_id: return

    # 1. Stop existing timer task
    if data.get("timer_task"):
        data["timer_task"].cancel()
        data["timer_task"] = None
    data["is_paused"] = False

    # 2. Storage Cleanup (Purana song delete kardo agar repeat OFF hai)
    old_current = data.get("current")
    if old_current and not data.get("repeat"):
        try:
            if os.path.exists(old_current["path"]) and old_current["path"] not in (SILENCE_FILE, SILENCE_VIDEO_FILE):
                os.remove(old_current["path"])
        except Exception:
            pass

    # 3. Queue Logic
    if data.get("repeat") and data.get("current"):
        pass  # Replay existing file path directly
    elif data.get("queue"):
        # YAHAN DOWNLOAD HOGA! (Jab line aayegi tabhi download hoga)
        data["is_downloading"] = True
        try:
            next_item = data["queue"].pop(0)
            msg_to_dl = next_item["message"]
            stream_type = next_item["type"]

            # Fetch telegram metadata duration first
            dur = getattr(msg_to_dl.audio, "duration", 0) or \
                  getattr(msg_to_dl.video, "duration", 0) or \
                  getattr(msg_to_dl.voice, "duration", 0) or 0

            dl_msg = await msg_to_dl.reply_text("➜ Fetching from Queue & Downloading...")
            start_time = time.time()
            last_edit_time = [time.time()]

            async def progress(current, total):
                now = time.time()
                if now - last_edit_time[0] >= 5.0 or current == total:
                    last_edit_time[0] = now
                    percentage = current * 100 / total if total else 0
                    speed = current / (now - start_time) if (now - start_time) > 0 else 0
                    eta = round((total - current) / speed) if speed > 0 else 0

                    bar_len = 20
                    filled = int((percentage / 100) * bar_len)
                    bar = '█' * filled + '▒' * (bar_len - filled)

                    text = (
                        f"➜ **Downloading queued file...**\n\n"
                        f"➜ [{bar}] {round(percentage, 2)}%\n"
                        f"➜ Size: {format_bytes(current)} / {format_bytes(total)}\n"
                        f"➜ Speed: {format_bytes(speed)}/s | ETA: {eta}s"
                    )
                    try:
                        await dl_msg.edit_text(text)
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass

            file_path = await msg_to_dl.download(progress=progress)
            await dl_msg.delete()

            # If duration wasn't in metadata, calculate from ffprobe
            if not dur:
                dur = get_media_duration(file_path) or 300  # Fallback 5 mins

            file_name = file_path.split('/')[-1]
            data["current"] = {
                "path": file_path,
                "type": stream_type,
                "name": file_name,
                "duration": dur
            }
        except Exception as e:
            print(f"Queue Download Error: {e}")
            data["is_downloading"] = False
            data["current"] = None
            asyncio.create_task(play_next(phone))  # Try next in queue if this fails
            return
        finally:
            data["is_downloading"] = False

    else:
        # Queue Empty -> Play idle placeholder (always video-capable) and MUTE Mic automatically
        data["current"] = None
        idle_source = await get_idle_stream_source()
        try:
            original_invoke = user_app.invoke
            async def patched_invoke(query, *args, **kwargs):
                if query.__class__.__name__ == "JoinGroupCall" and join_as_peer:
                    query.join_as = join_as_peer
                return await original_invoke(query, *args, **kwargs)
            user_app.invoke = patched_invoke

            # Timeout guard: if the native call hangs here, don't freeze the
            # whole bot forever - bail out after 25s so /start etc. still work.
            await asyncio.wait_for(
                call_app.play(chat_id, get_stream_object(idle_source, "video")),
                timeout=25
            )
            user_app.invoke = original_invoke

            await asyncio.sleep(1.5)
            # Not talking / nothing playing -> mic stays muted
            await sync_mic_state(data, chat_id, should_be_unmuted=False)
        except asyncio.TimeoutError:
            user_app.invoke = original_invoke
            print(f"[{phone}] Idle play() timed out - tearing down stuck session.")
            await _force_teardown_session(phone)
            return
        except Exception as e:
            user_app.invoke = original_invoke
            print(f"[{phone}] Idle play error: {e}")

        await update_player_ui(phone)
        return

    # 4. Play New Media
    file_path = data["current"]["path"]
    stream_type = data["current"]["type"]
    duration = data["current"]["duration"]

    # --- RAW PAYLOAD OVERRIDE (For Join As / Display As) ---
    original_invoke = user_app.invoke
    async def patched_invoke(query, *args, **kwargs):
        if query.__class__.__name__ == "JoinGroupCall" and join_as_peer:
            query.join_as = join_as_peer
        return await original_invoke(query, *args, **kwargs)
    user_app.invoke = patched_invoke

    try:
        # Trigger Play - timeout guard so a stuck native call can't freeze the bot
        await asyncio.wait_for(
            call_app.play(chat_id, get_stream_object(file_path, stream_type)),
            timeout=25
        )

        # Smart Unmute: Wait for WebRTC to connect properly, then OPEN Mic
        await asyncio.sleep(1.5)
        # Actively playing/"talking" -> mic opens automatically
        await sync_mic_state(data, chat_id, should_be_unmuted=True)

        # Start Tracker Timer for Exact Auto-Next
        data["timer_task"] = asyncio.create_task(media_timer_worker(phone, duration))

    except asyncio.TimeoutError:
        print(f"[{phone}] play() timed out - the call session looks stuck.")
        data["current"] = None
        user_app.invoke = original_invoke
        return
    except Exception as e:
        print(f"Play Error: {e}")
        data["current"] = None
        user_app.invoke = original_invoke
        asyncio.create_task(play_next(phone))
        return
    finally:
        user_app.invoke = original_invoke

    await update_player_ui(phone)


# --- TELEGRAM BOT HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = str(message.from_user.id)
    db = load_db()

    if is_banned(user_id, db):
        return await message.reply_text("➜ You are banned from using this bot.")

    if await show_main_menu(message, user_id, db):
        return

    login_url = generate_login_link(user_id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➜ Login Account", url=login_url)],
        [InlineKeyboardButton("➜ Continue", callback_data="check_login")]
    ])
    await message.reply_text("➜ **Telegram Live Stream Bot** ➜\n\n➜ No account connected. Click below to login:", reply_markup=keyboard)

@app.on_callback_query(filters.regex("check_login"))
async def check_login_status(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    db = load_db()
    if is_banned(user_id, db):
        return await callback_query.answer("➜ You are banned.", show_alert=True)

    if not await show_main_menu(callback_query, user_id, db):
        await callback_query.answer("➜ No account active! Please login first.", show_alert=True)

@app.on_callback_query(filters.regex("login_more"))
async def login_more_callback(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    login_url = generate_login_link(user_id)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("➜ Add New Account", url=login_url)],
                                     [InlineKeyboardButton("➜ Back to Menu", callback_data="check_login")]])
    await callback_query.message.edit_text("➜ Use this secure link to login a new account. Expires in 10 mins.", reply_markup=keyboard)

@app.on_callback_query(filters.regex("switch_account"))
async def switch_account_callback(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    db = load_db()
    accounts = db.get(user_id, {}).get("accounts", {})
    if not accounts: return await callback_query.answer("➜ No account found!", show_alert=True)
    buttons = []
    for phone in accounts.keys():
        text = f"➜ [ACTIVE] {phone}" if phone == db[user_id].get("active_account") else f"➜ {phone}"
        buttons.append([InlineKeyboardButton(text, callback_data=f"set_acc_{phone}")])
    buttons.append([InlineKeyboardButton("➜ Back to Menu", callback_data="check_login")])
    await callback_query.message.edit_text("➜ **SWITCH ACCOUNT** ➜\n\n➜ Select an account:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^set_acc_"))
async def set_active_account(client, callback_query: CallbackQuery):
    phone = callback_query.data.split("set_acc_")[1]
    user_id = str(callback_query.from_user.id)
    db = load_db()
    if phone in db.get(user_id, {}).get("accounts", {}):
        db[user_id]["active_account"] = phone
        save_db(db)
        await callback_query.answer("➜ Active account updated.", show_alert=True)
        await show_main_menu(callback_query, user_id, db)

# --- ADMIN SYSTEM ---
@app.on_callback_query(filters.regex("admin_menu"))
async def admin_menu_click(client, callback_query: CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        return await callback_query.answer("➜ Not Authorized", show_alert=True)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➜ Ban User", callback_data="adm_ban"),
         InlineKeyboardButton("➜ Unban User", callback_data="adm_unban")],
        [InlineKeyboardButton("➜ Login Sessions", callback_data="adm_sessions")],
        [InlineKeyboardButton("➜ Back to Menu", callback_data="check_login")]
    ])
    await callback_query.message.edit_text("➜ **ADMIN PANEL** ➜\n\n➜ Select an option:", reply_markup=keyboard)

@app.on_callback_query(filters.regex(r"^adm_(ban|unban)$"))
async def admin_ban_unban_click(client, callback_query: CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID: return
    action = callback_query.data.split("_")[1]
    user_steps[str(callback_query.from_user.id)] = {"step": f"wait_{action}_id"}
    await callback_query.message.edit_text(f"➜ Send the User ID or Username to {action}:")

@app.on_callback_query(filters.regex("adm_sessions"))
async def admin_sessions_click(client, callback_query: CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID: return
    db = load_db()
    buttons = []

    for uid, data in db.items():
        if uid != "banned_users" and isinstance(data, dict):
            for phone in data.get("accounts", {}).keys():
                buttons.append([InlineKeyboardButton(f"➜ Session: {phone}", callback_data=f"test_session_{phone}")])

    buttons.append([InlineKeyboardButton("➜ Back to Admin Menu", callback_data="admin_menu")])
    await callback_query.message.edit_text("➜ **LOGIN SESSIONS** ➜\n\n➜ Click any to test:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^test_session_"))
async def admin_test_session(client, callback_query: CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID: return
    phone = callback_query.data.split("test_session_")[1]

    db = load_db()
    session_string = None
    for uid, data in db.items():
        if uid != "banned_users" and isinstance(data, dict):
            if phone in data.get("accounts", {}):
                session_string = data["accounts"][phone]["session_string"]
                break

    if not session_string:
        return await callback_query.answer("➜ Session not found!", show_alert=True)

    await callback_query.message.edit_text("➜ Testing session... Please wait.")

    try:
        test_app = Client(f"test_{phone}", session_string=session_string, api_id=API_ID, api_hash=API_HASH)
        await test_app.connect()
        await test_app.send_message(int(ADMIN_ID), f"➜ Bot Test Message: Session for {phone} is active and working!")
        await test_app.disconnect()
        await callback_query.message.edit_text("➜ Test Successful! Message sent to your chat.\n\n➜ Click /start to return.")
    except Exception as e:
        await callback_query.message.edit_text(f"➜ Test Failed. Session might be dead.\n➜ Error: {e}\n\n➜ Click /start to return.")

# --- STREAMING SETUP LOGIC ---
@app.on_callback_query(filters.regex(r"^(audio_live|video_live)$"))
async def media_live_click(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    db = load_db()
    if is_banned(user_id, db): return await callback_query.answer("➜ You are banned.", show_alert=True)

    stream_type = "audio" if callback_query.data == "audio_live" else "video"
    active_phone = db.get(user_id, {}).get("active_account")

    if not active_phone:
        return await callback_query.answer("➜ Please login first!", show_alert=True)

    user_steps[user_id] = {"step": "wait_vc_link", "phone": active_phone, "type": stream_type}
    icon = "🎧" if stream_type == "audio" else "🎬"
    await callback_query.message.edit_text(f"➜ {icon} Send me the VC Link or ID for {stream_type} stream (e.g. -100123456789 or https://t.me/...):")

@app.on_message(filters.private & ~filters.command("start"))
async def handle_user_steps(client, message):
    user_id = str(message.from_user.id)
    db = load_db()
    if is_banned(user_id, db): return

    if user_id not in user_steps:
        return

    state_info = user_steps[user_id]
    step = state_info.get("step")

    if step in ["wait_ban_id", "wait_unban_id"] and user_id == ADMIN_ID:
        target = message.text.strip()
        try:
            target_user = await app.get_users(target)
            target_id = str(target_user.id)
        except Exception:
            target_id = target

        if step == "wait_ban_id":
            if target_id not in db["banned_users"]:
                db["banned_users"].append(target_id)
                save_db(db)
            await message.reply_text(f"➜ User {target_id} has been banned.")
        else:
            if target_id in db["banned_users"]:
                db["banned_users"].remove(target_id)
                save_db(db)
            await message.reply_text(f"➜ User {target_id} has been unbanned.")

        del user_steps[user_id]
        return

    phone = state_info.get("phone")
    if not phone: return
    stream_type = state_info.get("type", "audio")
    session_string = db.get(user_id, {}).get("accounts", {}).get(phone, {}).get("session_string")

    if step == "wait_vc_link":
        raw_input = message.text.strip()
        msg = await message.reply_text("➜ Verifying VC link/ID and fetching 'Display As' Profiles...")
        target_input = raw_input.split("?")[0] if "?" in raw_input else raw_input

        if phone not in active_clients:
            user_app = Client(f"session_{phone}", session_string=session_string, api_id=API_ID, api_hash=API_HASH)
            await user_app.start()

            call_app = PyTgCalls(user_app)
            await call_app.start()

            active_clients[phone] = {
                "app": user_app,
                "call": call_app,
                "queue": [],
                "current": None,
                "repeat": False,
                "ui_message": None,
                "chat_id": None,
                "join_as_peer": None,
                "timer_task": None,
                "is_paused": False,
                "is_downloading": False,
                "is_muted": True,
                "session_type": stream_type,  # remembers audio_live vs video_live for this session
            }
        else:
            # Reusing an existing client for a fresh VC - keep session type in sync
            active_clients[phone]["session_type"] = stream_type

        user_app = active_clients[phone]["app"]

        try:
            chat_id = None
            title = "Unknown"

            if target_input.lstrip("-").isdigit():
                chat_id = int(target_input)
                chat = await user_app.get_chat(chat_id)
                title = chat.title
            elif "t.me/+" in target_input or "joinchat" in target_input:
                try:
                    chat = await user_app.join_chat(target_input)
                    chat_id = chat.id
                    title = chat.title
                except UserAlreadyParticipant:
                    await msg.edit_text("➜ Account is already in this private chat.\n➜ Please provide the direct ID (e.g. -100xxxx).")
                    return
                except InviteRequestSent:
                    await msg.edit_text("➜ Private Link me Join Request (Admin Approval) ON hai.\n➜ Bot ne request nahi bheji. Aap khud join karke aaiye aur dobara try karein.")
                    return
            else:
                username = target_input.split("/")[-1].replace("@", "") if "/" in target_input else target_input.replace("@", "")
                try:
                    chat = await user_app.get_chat(username)
                except Exception:
                    try:
                        await user_app.join_chat(username)
                        chat = await user_app.get_chat(username)
                    except UserAlreadyParticipant:
                        chat = await user_app.get_chat(username)
                    except InviteRequestSent:
                        return await msg.edit_text("➜ Is Public Group/Channel me Join Request ON hai.\n➜ Main request nahi bhej raha. Aap khud join karke dobara link bhejein.")
                    except Exception as e:
                        return await msg.edit_text(f"➜ Chat access nahi ho pa rahi. Pehle khud join karein, fir try karein.\n➜ Error: {e}")

                chat_id = chat.id
                title = chat.title

            channels_buttons = [[InlineKeyboardButton("➜ Personal Account", callback_data=f"joinas_personal_{chat_id}")]]
            try:
                result = await user_app.invoke(GetAdminedPublicChannels(by_location=False, check_limit=False))
                for c in result.chats:
                    real_id = int(f"-100{c.id}")
                    channels_buttons.append([InlineKeyboardButton(f"➜ {c.title}", callback_data=f"joinas_{real_id}_{chat_id}")])
            except Exception:
                pass

            await msg.edit_text(f"➜ Found VC: {title}\n\n➜ **DISPLAY AS** (Select Identity):", reply_markup=InlineKeyboardMarkup(channels_buttons))

        except Exception as e:
            await msg.edit_text(f"➜ Failed to connect to VC. Error: {e}")

    # LAZY LOAD QUEUE LOGIC
    elif step == "wait_media":
        if (message.audio or message.voice or message.document or message.video):

            # Detect the REAL type of this specific file - a video always
            # gets queued as "video" and an audio file always as "audio",
            # regardless of whether "Audio Live" or "Video Live" was
            # clicked at the start. This is what stops videos being
            # streamed as audio-only.
            item_type = detect_media_kind(message, fallback=stream_type)

            active_clients[phone]["queue"].append({
                "message": message,
                "type": item_type
            })

            q_len = len(active_clients[phone]["queue"])
            icon = "🎬" if item_type == "video" else "🎧"
            await message.reply_text(f"➜ {icon} Added to Queue (Position: {q_len})\n➜ It will download and play when its turn comes.")

            if not active_clients[phone].get("ui_message"):
                ui_msg = await message.reply_text("➜ Setting up Player...")
                active_clients[phone]["ui_message"] = ui_msg

            # If nothing is playing AND not currently downloading, trigger next
            if not active_clients[phone].get("current") and not active_clients[phone].get("is_downloading"):
                asyncio.create_task(play_next(phone))
            else:
                await update_player_ui(phone)

        else:
            await message.reply_text("➜ Please send a valid Audio or Video file!")

# --- DISPLAY AS / JOIN CALLBACK ---
@app.on_callback_query(filters.regex(r"^joinas_"))
async def joinas_callback(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    if user_id not in user_steps:
        return await callback_query.answer("➜ Request expired. Start again.", show_alert=True)

    state_info = user_steps[user_id]
    phone = state_info["phone"]
    session_type = state_info.get("type", "audio")

    parts = callback_query.data.split("_")
    join_as_id = parts[1]
    chat_id = int(parts[2])

    msg = callback_query.message
    await msg.edit_text("➜ Connecting to Voice Chat... Please wait.")

    user_app = active_clients[phone]["app"]
    call_app = active_clients[phone]["call"]
    active_clients[phone]["chat_id"] = chat_id
    active_clients[phone]["session_type"] = session_type

    join_as_peer = None
    if join_as_id != "personal":
        try:
            join_as_peer = await user_app.resolve_peer(int(join_as_id))
        except Exception:
            pass

    active_clients[phone]["join_as_peer"] = join_as_peer

    # --- RAW PAYLOAD OVERRIDE (For Join As) ---
    original_invoke = user_app.invoke
    async def patched_invoke(query, *args, **kwargs):
        if query.__class__.__name__ == "JoinGroupCall" and join_as_peer:
            query.join_as = join_as_peer
        return await original_invoke(query, *args, **kwargs)
    user_app.invoke = patched_invoke

    try:
        # Join using an always-video-capable idle placeholder (silent black
        # video). This guarantees a video track exists on the call from the
        # very start, so it doesn't matter whether "Audio Live" or "Video
        # Live" was chosen - real video files sent later will always render
        # correctly for other participants, and real audio files will just
        # play over the black idle screen.
        idle_source = await get_idle_stream_source()

        # Timeout guard: reusing a PyTgCalls instance after a previous
        # leave_call is a known source of hangs where play() never resolves.
        # Bail out after 25s instead of freezing the whole bot (including
        # /start) forever.
        await asyncio.wait_for(
            call_app.play(chat_id, get_stream_object(idle_source, "video")),
            timeout=25
        )

        # MUTE FIX: Wait so WebRTC connects fully, then force Mute (idle = muted).
        await asyncio.sleep(2)
        await sync_mic_state(active_clients[phone], chat_id, should_be_unmuted=False)

        user_steps[user_id]["step"] = "wait_media"
        user_steps[user_id]["chat_id"] = chat_id

        icon = "🎧" if session_type == "audio" else "🎬"
        await msg.edit_text(
            f"➜ {icon} Joined VC Successfully as {'Channel' if join_as_peer else 'Personal Account'}! (Mic is Muted 🔇)\n\n"
            f"➜ Send media files now.\n"
            f"➜ Media will safely stay in queue and download ONLY when their turn comes."
        )

    except asyncio.TimeoutError:
        # The call session is stuck (very common if this PyTgCalls instance
        # was reused after a previous stop). Tear it down completely so the
        # NEXT attempt builds a fresh Client + PyTgCalls from scratch.
        await _force_teardown_session(phone)
        await msg.edit_text(
            "➜ VC join timed out and the session looked stuck, so it has been fully reset. ⚠️\n\n"
            "➜ Please send /start and try joining again - this usually works on a clean retry."
        )
    except Exception as join_err:
        await msg.edit_text(f"➜ Connected to chat but failed to join VC: {join_err}")
    finally:
        user_app.invoke = original_invoke

# --- IN-STREAM CONTROLS ---
@app.on_callback_query(filters.regex(r"^(pause_|resume_|stop_|record_|stoprec_|repeat_|skip_)"))
async def stream_controls(client, callback_query: CallbackQuery):
    action, chat_id_str = callback_query.data.split("_")
    chat_id = int(chat_id_str)
    user_id = str(callback_query.from_user.id)

    db = load_db()
    active_phone = db.get(user_id, {}).get("active_account")
    if not active_phone or active_phone not in active_clients:
        return await callback_query.answer("➜ Stream session not found.", show_alert=True)

    call_app = active_clients[active_phone]["call"]
    user_app = active_clients[active_phone]["app"]

    try:
        if action == "pause":
            active_clients[active_phone]["is_paused"] = True
            await _pause(call_app, chat_id)
            await callback_query.answer("➜ Stream Paused! ⏸")

        elif action == "resume":
            active_clients[active_phone]["is_paused"] = False
            await _resume(call_app, chat_id)
            await callback_query.answer("➜ Stream Resumed! ▶")

        elif action == "repeat":
            active_clients[active_phone]["repeat"] = not active_clients[active_phone].get("repeat", False)
            await update_player_ui(active_phone)
            await callback_query.answer("➜ Repeat Mode Toggled!")

        elif action == "skip":
            active_clients[active_phone]["repeat"] = False
            await callback_query.answer("➜ Skipping to next media...")
            asyncio.create_task(play_next(active_phone))

        elif action == "stop":
            data = active_clients.get(active_phone, {})

            if data.get("current"):
                try: os.remove(data["current"]["path"])
                except: pass

            # Full teardown (leave call, stop PyTgCalls, stop client) and
            # drop the session entirely, so the NEXT "Audio/Video Live"
            # click builds a completely fresh Client + PyTgCalls instead of
            # reusing one that might be in a stuck internal state - this is
            # what was causing the bot to hang/freeze on a second join.
            await _force_teardown_session(active_phone)

            if user_id in user_steps: del user_steps[user_id]

            await callback_query.message.edit_text("➜ Stream Stopped, Session Fully Reset! 🛑\n\n➜ Bot has left the VC. Send /start to connect again.")

        elif action == "record":
            input_call = await get_input_group_call(user_app, chat_id)
            if not input_call:
                return await callback_query.answer("➜ VC Error: Unable to fetch Call Data.", show_alert=True)

            try:
                await user_app.invoke(ToggleGroupCallRecord(call=input_call, start=True, video=False, title="Bot Record"))
            except Exception as e:
                return await callback_query.answer(f"➜ Failed! You are not Admin in VC or Error: {e}", show_alert=True)

            msg = await callback_query.message.reply_text("➜ Recording Started... 🔴\n➜ Duration: 00:00\n\n➜ (Saved to Account's Saved Messages)")

            async def timer_task():
                secs = 0
                while True:
                    await asyncio.sleep(5)
                    secs += 5
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("➜ Stop Recording", callback_data=f"stoprec_{chat_id}")]])
                    try:
                        await msg.edit_text(f"➜ Recording VC Live... 🔴\n➜ Duration: {format_time(secs)}", reply_markup=keyboard)
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass

            task = asyncio.create_task(timer_task())
            record_tasks[f"{user_id}_{chat_id}"] = {"task": task, "msg": msg}
            await callback_query.answer("➜ Started Recording!", show_alert=True)

        elif action == "stoprec":
            task_info = record_tasks.pop(f"{user_id}_{chat_id}", None)
            if task_info:
                task_info["task"].cancel()

            input_call = await get_input_group_call(user_app, chat_id)
            if input_call:
                await user_app.invoke(ToggleGroupCallRecord(call=input_call, start=False, video=False))

            await callback_query.message.edit_text("➜ Recording Stopped! ✅\n\n➜ Check Saved Messages of the connected account.")

    except Exception as e:
        await callback_query.answer(f"➜ Error: {e}", show_alert=True)

# --- WEB SERVER (SECURE UI) ---
def base_html(title, content):
    return f"""
    <html>
        <head>
            <title>{title}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #e6ebee; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
                .container {{ background: white; padding: 40px 30px; border-radius: 12px; box-shadow: 0px 8px 20px rgba(0,0,0,0.1); width: 100%; max-width: 350px; }}
                h2 {{ color: #2AABEE; margin-bottom: 10px; }}
                p {{ color: #555; font-size: 14px; margin-bottom: 20px; }}
                input {{ padding: 14px; margin: 10px 0; width: 100%; box-sizing: border-box; border-radius: 8px; border: 1px solid #ccd1d9; outline: none; font-size: 15px; }}
                button {{ background-color: #2AABEE; color: white; padding: 14px; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; width: 100%; font-size: 16px; margin-top: 10px; }}
            </style>
        </head>
        <body>
            <div class="container">{content}</div>
        </body>
    </html>
    """

def get_valid_user(token):
    if not token or token not in login_tokens: return None
    if time.time() > login_tokens[token]["expires"]:
        del login_tokens[token]
        return None
    return login_tokens[token]["user_id"]

async def handle_login_page(request):
    token = request.query.get("token")
    user_id = get_valid_user(token)
    if not user_id: return web.Response(text="<h1>404 Not Found</h1><p>Link expired.</p>", status=404, content_type='text/html')

    content = f"""<h2>Secure Login</h2><p>Live stream bot me account add karein.</p>
        <form action="/send_code" method="post"><input type="hidden" name="token" value="{token}">
        <input type="text" name="phone" placeholder="Phone Number" required><button type="submit">Send OTP</button></form>"""
    return web.Response(text=base_html("Login", content), content_type='text/html')

async def handle_send_code(request):
    data = await request.post()
    token, phone = data.get("token"), data.get("phone")
    user_id = get_valid_user(token)
    if not user_id: return web.Response(text="<h1>404 Expired</h1>", status=404, content_type='text/html')

    user_client = Client(f":memory:", api_id=API_ID, api_hash=API_HASH)
    await user_client.connect()
    try:
        sent_code = await user_client.send_code(phone)
        login_state[token] = {"client": user_client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        content = f"""<h2>Enter OTP</h2><form action="/verify_code" method="post">
        <input type="hidden" name="token" value="{token}"><input type="text" name="otp" required autocomplete="off">
        <button type="submit">Verify OTP</button></form>"""
        return web.Response(text=base_html("Verify OTP", content), content_type='text/html')
    except Exception as e:
        await user_client.disconnect()
        return web.Response(text=base_html("Error", f"<h2 style='color:red;'>Error</h2><p>{str(e)}</p>"), content_type='text/html')

async def handle_verify_code(request):
    data = await request.post()
    token, otp = data.get("token"), data.get("otp")
    user_id = get_valid_user(token)
    state = login_state.get(token)
    if not user_id or not state: return web.Response(text="<h1>404 Expired</h1>", status=404, content_type='text/html')

    user_client, phone, phone_code_hash = state["client"], state["phone"], state["phone_code_hash"]
    try:
        await user_client.sign_in(phone, phone_code_hash, otp)
        return await finalize_login(token, user_id, user_client, phone)
    except SessionPasswordNeeded:
        content = f"""<h2>2FA Password</h2><form action="/verify_2fa" method="post">
        <input type="hidden" name="token" value="{token}"><input type="password" name="password" required>
        <button type="submit">Submit Password</button></form>"""
        return web.Response(text=base_html("2FA Required", content), content_type='text/html')
    except PhoneCodeInvalid:
        return web.Response(text=base_html("Error", "<h2 style='color:red;'>Invalid OTP!</h2>"), content_type='text/html')

async def handle_verify_2fa(request):
    data = await request.post()
    token, password = data.get("token"), data.get("password")
    user_id, state = get_valid_user(token), login_state.get(token)
    if not user_id or not state: return web.Response(text="<h1>404 Expired</h1>", status=404, content_type='text/html')
    user_client, phone = state["client"], state["phone"]
    try:
        await user_client.check_password(password)
        return await finalize_login(token, user_id, user_client, phone)
    except PasswordHashInvalid:
        return web.Response(text=base_html("Error", "<h2 style='color:red;'>Wrong Password!</h2>"), content_type='text/html')

async def finalize_login(token, user_id, user_client, phone):
    session_string = await user_client.export_session_string()
    await user_client.disconnect()

    db = load_db()
    if user_id not in db: db[user_id] = {"accounts": {}, "active_account": None}

    db[user_id]["accounts"][phone] = {
        "session_string": session_string,
        "logged_in": True,
        "login_time": time.time()
    }
    db[user_id]["active_account"] = phone
    save_db(db)

    if token in login_tokens: del login_tokens[token]
    if token in login_state: del login_state[token]

    content = """<h2 style="color:green;">Success!</h2>
        <p>Account linked. Telegram me wapas jayein aur <b>Continue</b> dabayein.</p>"""
    return web.Response(text=base_html("Success", content), content_type='text/html')

async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get('/login', handle_login_page)
    web_app.router.add_post('/send_code', handle_send_code)
    web_app.router.add_post('/verify_code', handle_verify_code)
    web_app.router.add_post('/verify_2fa', handle_verify_2fa)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def main():
    await start_web_server()
    await app.start()
    print("➜ Bot is UP and Ready with Perfect Lazy-Load Queue, Video Fix & Mute Logic!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

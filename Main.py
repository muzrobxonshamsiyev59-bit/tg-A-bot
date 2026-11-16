import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from shazamio import Shazam
from yt_dlp import YoutubeDL
from yt_dlp.utils import match_filter_func
from ytmusicapi import YTMusic

# =====================================================================
#  SOZLAMALAR
# =====================================================================
BOT_TOKEN = "9272089130:AAErU2viRRCaxCnDrs95rrzWeNawXeFLHa4"
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN bo'sh. Kod ichiga tokenni yozing.")

# YouTube bloklasa, quyidagilardan BITTASINI yoqing:
COOKIES_FILE = None        # masalan: "cookies.txt"
COOKIES_BROWSER = None     # masalan: "firefox" (yoki "chrome", "edge")

MAX_DURATION = 15 * 60                 # 15 daqiqadan uzun treklar chiqarilmaydi
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # Telegramdan yuklab olish limiti (20 MB)
TG_UPLOAD_LIMIT = 50 * 1024 * 1024     # Telegramga yuborish limiti (50 MB)
SEARCH_LIMIT = 15                      # nechta natija qidiriladi
PAGE_SIZE = 5                          # bir sahifada nechta tugma
MAX_PARALLEL_DOWNLOADS = 3             # bir vaqtda nechta yuklash

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ytm = YTMusic()  # YouTube Music qidiruvi (login shart emas)

DOWNLOAD_SEM = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
ACTIVE_USERS: set[int] = set()

# Xotiradagi kichik keshlar (eskilari avtomatik o'chadi)
TRACKS: "OrderedDict[str, dict]" = OrderedDict()     # video_id -> trek ma'lumoti
SEARCHES: "OrderedDict[str, list]" = OrderedDict()   # qidiruv id -> natijalar


def cache_put(cache: OrderedDict, key: str, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


# =====================================================================
#  QIDIRUV (YouTube Music) va YUKLASH (yt-dlp)
# =====================================================================
def base_ydl_opts() -> dict:
    opts = {"quiet": True, "noplaylist": True}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    elif COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    return opts


def _parse_results(raw: list) -> list[dict]:
    out = []
    for r in raw:
        vid = r.get("videoId")
        if not vid:
            continue
        dur = r.get("duration_seconds") or 0
        if dur and dur > MAX_DURATION:
            continue
        artists = ", ".join(a["name"] for a in (r.get("artists") or []) if a.get("name"))
        out.append(
            {
                "id": vid,
                "title": r.get("title") or "?",
                "artist": artists,
                "duration": dur,
            }
        )
    return out


def ytdlp_search_sync(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """Zaxira qidiruv: oddiy YouTube qidiruvi (yt-dlp orqali)."""
    opts = {**base_ydl_opts(), "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        dur = int(e.get("duration") or 0)
        if dur > MAX_DURATION:
            continue
        out.append(
            {
                "id": e["id"],
                "title": e.get("title") or "?",
                "artist": e.get("uploader") or e.get("channel") or "",
                "duration": dur,
            }
        )
    return out


def search_music_sync(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """1) YouTube Music 'songs', 2) 'videos', 3) zaxira: yt-dlp qidiruvi."""
    results = []
    try:
        results = _parse_results(ytm.search(query, filter="songs", limit=limit))
        if not results:
            results = _parse_results(ytm.search(query, filter="videos", limit=limit))
    except Exception:
        logging.warning("ytmusicapi qidiruvi xato berdi", exc_info=True)

    if not results:
        logging.info("ytmusicapi natija bermadi, yt-dlp qidiruviga o'tilmoqda: %s", query)
        results = ytdlp_search_sync(query, limit)

    logging.info("Qidiruv '%s': %d ta natija", query, len(results))
    return results


def ydl_download(url: str, out_dir: str):
    opts = {
        **base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "match_filter": match_filter_func(f"duration < {MAX_DURATION}"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        return None
    files = list(Path(out_dir).glob("*.mp3"))
    return files[0] if files else None


def soundcloud_find_sync(artist: str, title: str, duration: int):
    """Zaxira manba: SoundCloud'dan mos trekni topadi."""
    opts = {**base_ydl_opts(), "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch5:{artist} {title}".strip(), download=False)
    for e in (info or {}).get("entries") or []:
        url = e.get("url") or e.get("webpage_url")
        d = e.get("duration") or 0
        if not url:
            continue
        if d and d > MAX_DURATION:
            continue
        if duration and d and abs(d - duration) > max(20, duration * 0.2):
            continue  # davomiyligi mos kelmasa, boshqa qo'shiq bo'lishi mumkin
        return url
    return None


def download_track_sync(meta: dict, out_dir: str):
    """1) YouTube, bo'lmasa 2) SoundCloud. (fayl, manba) qaytaradi."""
    last_err = None
    try:
        f = ydl_download(f"https://www.youtube.com/watch?v={meta['id']}", out_dir)
        if f:
            return f, "YouTube"
    except Exception as e:
        logging.warning("YouTube yuklash o'xshamadi: %s", e)
        last_err = e

    if meta.get("title") and meta["title"] != "?":
        try:
            url = soundcloud_find_sync(meta.get("artist", ""), meta["title"], meta.get("duration", 0))
            if url:
                f = ydl_download(url, out_dir)
                if f:
                    return f, "SoundCloud"
        except Exception as e:
            logging.warning("SoundCloud yuklash o'xshamadi: %s", e)
            last_err = last_err or e

    if last_err:
        raise last_err
    return None


# =====================================================================
#  YORDAMCHI FUNKSIYALAR
# =====================================================================
def fmt_duration(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        logging.warning("edit_text failed", exc_info=True)


async def safe_delete(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


def explain_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "sign in to confirm" in text or "not a bot" in text or "403" in text:
        return "YouTube so'rovni blokladi. Keyinroq urinib ko'ring."
    if "private" in text or "unavailable" in text or "not available" in text:
        return "Bu video mavjud emas yoki yopiq."
    if "ffmpeg" in text or "ffprobe" in text:
        return "Serverda ffmpeg bilan muammo bor."
    if "age" in text and "restrict" in text:
        return "Bu video yosh cheklovi tufayli yuklanmaydi."
    return "Xatolik yuz berdi. Keyinroq urinib ko'ring."


def build_keyboard(sid: str, page: int) -> InlineKeyboardMarkup:
    items = SEARCHES[sid]
    start = page * PAGE_SIZE
    rows = []
    for n, r in enumerate(items[start : start + PAGE_SIZE], start=start + 1):
        label = f"{n}. {r['artist']} — {r['title']}" if r["artist"] else f"{n}. {r['title']}"
        if r["duration"]:
            label += f" ({fmt_duration(r['duration'])})"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"dl:{r['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"pg:{sid}:{page - 1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"pg:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def to_wav(src: str, dst: str) -> bool:
    """Shazam uchun ishonchli format: 44.1 kHz mono WAV, birinchi 20 soniya."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src, "-t", "20", "-ar", "44100", "-ac", "1", dst,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0 and os.path.exists(dst)


# =====================================================================
#  YUBORISH VA QIDIRISH
# =====================================================================
async def send_track(message: Message, video_id: str) -> bool:
    """Trekni MP3 qilib chatga yuboradi."""
    meta = TRACKS.get(video_id) or {"id": video_id, "title": "?", "artist": "", "duration": 0}
    status = await message.answer(f"⏳ Yuklab olinmoqda: {meta['title']}"[:100])
    tmp = tempfile.mkdtemp()
    try:
        async with DOWNLOAD_SEM:
            res = await asyncio.to_thread(download_track_sync, meta, tmp)
        if not res:
            await safe_edit(status, "Yuklab bo'lmadi (juda uzun yoki mavjud emas).")
            return False
        file, source = res
        if file.stat().st_size > TG_UPLOAD_LIMIT:
            await safe_edit(status, "Fayl Telegram limitidan (50 MB) katta, yuborib bo'lmaydi.")
            return False
        await message.answer_audio(
            FSInputFile(file),
            title=meta["title"] if meta["title"] != "?" else None,
            performer=meta["artist"] or None,
            duration=meta["duration"] or None,
        )
        await safe_delete(status)
        return True
    except Exception as e:
        logging.exception("download error")
        await safe_edit(status, explain_error(e))
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def send_results(message: Message, query: str, auto: bool = False):
    """Natijalarni tugmalar bilan ko'rsatadi. auto=True bo'lsa, 1-trekni darhol yuboradi."""
    status = await message.answer("🔎 Qidirilmoqda...")
    try:
        results = await asyncio.to_thread(search_music_sync, query)
    except Exception:
        logging.exception("search error")
        await safe_edit(status, "Qidiruvda xatolik yuz berdi. Keyinroq urinib ko'ring.")
        return
    await safe_delete(status)

    if not results:
        await message.answer("Hech narsa topilmadi 😕")
        return

    for r in results:
        cache_put(TRACKS, r["id"], r, 2000)

    if auto:
        await send_track(message, results[0]["id"])

    sid = uuid.uuid4().hex[:8]
    cache_put(SEARCHES, sid, results, 200)
    title = "Boshqa variant kerak bo'lsa, tanlang 👇" if auto else f"🎵 Natijalar: {query[:40]}"
    await message.answer(title, reply_markup=build_keyboard(sid, 0))


# =====================================================================
#  HANDLERLAR
# =====================================================================
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Salom! 🎵\n\n"
        "• Qo'shiq yoki ijrochi nomini yozing, natijalardan tanlang\n"
        "• Yoki ovozli xabar / audio / video yuboring, qo'shiqni aniqlayman"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Qo'shiq nomini yozing, masalan: Xamdam Sobirov Peshta\n"
        "Ro'yxatdan kerakli trekni bosing, MP3 yuboriladi.\n"
        "Qo'shiqni topa olmasangiz, 10-15 soniyalik ovoz yozib yuboring."
    )


@dp.message(F.voice | F.audio | F.video | F.video_note)
async def recognize(message: Message):
    user_id = message.from_user.id
    if user_id in ACTIVE_USERS:
        await message.answer("Avvalgi so'rovingiz hali bajarilmoqda, biroz kuting ⏳")
        return

    media = message.voice or message.audio or message.video or message.video_note
    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer("Fayl juda katta (20 MB dan oshmasin). Qisqaroq parcha yuboring.")
        return

    ACTIVE_USERS.add(user_id)
    status = await message.answer("🎧 Tinglayapman...")
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "input")
        wav = os.path.join(tmp, "input.wav")
        await bot.download(media.file_id, destination=src)

        if not await to_wav(src, wav):
            await safe_edit(status, "Faylni o'qib bo'lmadi. Boshqa formatda yuborib ko'ring.")
            return

        out = await Shazam().recognize(wav)
        track = out.get("track")
        if not track:
            await safe_edit(status, "Qo'shiq aniqlanmadi 😕 Uzunroq (10-15 soniya) parcha yuboring.")
            return
        query = f"{track['subtitle']} - {track['title']}"
        await safe_edit(status, f"✅ Topildi: {query}")
        await send_results(message, query, auto=True)
    except Exception:
        logging.exception("recognize error")
        await safe_edit(status, "Qo'shiqni aniqlashda xatolik yuz berdi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        ACTIVE_USERS.discard(user_id)


@dp.message(F.text & ~F.text.startswith("/"))
async def search(message: Message):
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Qo'shiq yoki ijrochi nomini to'liqroq yozing ✍️")
        return

    user_id = message.from_user.id
    if user_id in ACTIVE_USERS:
        await message.answer("Avvalgi so'rovingiz hali bajarilmoqda, biroz kuting ⏳")
        return

    ACTIVE_USERS.add(user_id)
    try:
        await send_results(message, query)
    finally:
        ACTIVE_USERS.discard(user_id)


@dp.callback_query(F.data.startswith("pg:"))
async def change_page(call: CallbackQuery):
    _, sid, page = call.data.split(":")
    if sid not in SEARCHES:
        await call.answer("Qidiruv eskirgan, qaytadan yozing.", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=build_keyboard(sid, int(page)))
    except Exception:
        logging.warning("page edit failed", exc_info=True)
    await call.answer()


@dp.callback_query(F.data.startswith("dl:"))
async def download(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id in ACTIVE_USERS:
        await call.answer("Avvalgi so'rov tugashini kuting ⏳", show_alert=True)
        return

    video_id = call.data.split(":", 1)[1]
    await call.answer("Yuklanmoqda...")
    ACTIVE_USERS.add(user_id)
    try:
        await send_track(call.message, video_id)
    finally:
        ACTIVE_USERS.discard(user_id)


async def main():
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg topilmadi! MP3 yaratish va ovoz aniqlash ishlamaydi.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
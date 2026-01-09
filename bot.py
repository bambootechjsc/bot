import os
import json
import re
import threading
from datetime import datetime
import pytz
from dotenv import load_dotenv
import gspread
import google.generativeai as genai
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# --- 1. CẤU HÌNH HỆ THỐNG ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Danh sách kho hiển thị trên nút bấm
DANH_SACH_KHO = [["KHO_TONG", "KHO_LE", "KHO_DONG_LANH"]]

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None

app_web = Flask(__name__)
@app_web.route('/')
def home(): return "System Online!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. HÀM HỖ TRỢ ---
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def get_now_vntime():
    return datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).strftime("%d/%m/%Y %H:%M:%S")

def find_product_by_name(search_term, dm_data):
    search_term = search_term.strip().lower()
    for row in dm_data:
        if len(row) < 3: continue
        if search_term == row[1].lower() or search_term == row[0].lower():
            return {"ma": row[0], "ten": row[1], "rate": int(row[2])}
    return None

# --- 3. LỆNH NHẬP/XUẤT THỦ CÔNG (TEXT) ---
async def process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if update.message.from_user.id not in ADMIN_IDS: return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(f"⚠️ HD: /{mode.lower()} [kho] [tên sp] [sl]\nVí dụ: /{mode.lower()} KHO_TONG coca 10c")
        return
    try:
        kho, sl_raw, search_term = args[0].upper(), args[-1].lower(), " ".join(args[1:-1])
        ws_data, ws_dm = get_sheets()
        p = find_product_by_name(search_term, ws_dm.get_all_values()[1:])
        if not p:
            await update.message.reply_text(f"❌ Không thấy SP: {search_term}")
            return
        num = int(re.findall(r'\d+', sl_raw)[0])
        qty = (num * p['rate'] if 't' in sl_raw else num) * (1 if mode == "NHAP" else -1)
        ws_data.append_row([get_now_vntime(), kho, p['ma'], p['ten'], qty, mode, update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ Đã ghi {mode}: {p['ten']} {sl_raw}")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

# --- 4. LỆNH TỒN KHO ---
async def ton_kho_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("📊 Đang tính tồn kho...")
    try:
        ws_data, ws_dm = get_sheets()
        data_rows = ws_data.get_all_values()[1:]
        dm_rows = ws_dm.get_all_values()[1:]
        names = {r[0]: r[1] for r in dm_rows}
        res = {}
        for r in data_rows:
            k, m, q = r[1], r[2], int(r[4])
            if k not in res: res[k] = {}
            res[k][m] = res[k].get(m, 0) + q
        report = "📦 **TỒN KHO THỰC TẾ**\n"
        for k, sps in res.items():
            report += f"\n🏠 **{k}**\n" + "\n".join([f"• {names.get(m, m)}: `{s}`" for m, s in sps.items() if s != 0])
        await msg.edit_text(report, parse_mode="Markdown")
    except Exception as e: await msg.edit_text(f"❌ Lỗi: {e}")

# --- 5. XỬ LÝ AI VÀ TƯƠNG TÁC ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['temp_photo_bytes'] = await photo_file.download_as_bytearray()
    keyboard = [["NHAP", "XUAT"]]
    await update.message.reply_text("📥 Chọn loại giao dịch:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    context.user_data['step'] = 'CHOOSING_TYPE'

async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    text, step = update.message.text, context.user_data.get('step')

    if step == 'CHOOSING_TYPE' and text in ["NHAP", "XUAT"]:
        context.user_data['temp_type'] = text
        await update.message.reply_text(f"📍 Chọn kho {text}:", reply_markup=ReplyKeyboardMarkup(DANH_SACH_KHO, one_time_keyboard=True, resize_keyboard=True))
        context.user_data['step'] = 'CHOOSING_KHO'
    
    elif step == 'CHOOSING_KHO':
        kho, loai = text, context.user_data.get('temp_type')
        status = await update.message.reply_text(f"🤖 AI đang đọc phiếu {loai} tại {kho}...", reply_markup=ReplyKeyboardRemove())
        try:
            ws_data, ws_dm = get_sheets()
            dm_all = ws_dm.get_all_values()
            dm_txt = "\n".join([f"- {r[1]} ({r[0]})" for r in dm_all[1:]])
            img = context.user_data.get('temp_photo_bytes')

            if STORAGE_CHANNEL_ID:
                try: await context.bot.send_photo(STORAGE_CHANNEL_ID, bytes(img), caption=f"{loai} | {kho} | {get_now_vntime()}")
                except: pass

            prompt = f"Bạn là kế toán kho. Đọc ảnh phiếu {loai} vào kho {kho}. Danh mục SP:\n{dm_txt}\nNếu SP không có trong danh mục, đặt ma_sp là 'NEW'. Trả về JSON duy nhất: {{\"type\": \"{loai}\", \"transactions\": [{{\"kho\": \"{kho}\", \"ma_sp\": \"Mã\", \"ten_sp\": \"Tên\", \"so_luong\": \"10c\"}}]}}"
            
            response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img)}])
            # Fix lỗi đứng máy: Làm sạch JSON
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if not match: raise Exception("AI không trả về JSON hợp lệ.")
            data = json.loads(match.group())
            
            context.user_data['pending_ai'] = data
            summary = f"🎯 **AI ĐỀ XUẤT ({loai} - {kho}):**\n" + "\n".join([f"• {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']])
            await status.edit_text(summary + "\n\nBấm /ok để ghi sổ hoặc /huy để bỏ qua.")
            context.user_data['step'] = None
        except Exception as e:
            await status.edit_text(f"❌ Lỗi AI: {str(e)}")
            context.user_data.clear()

# --- 6. XÁC NHẬN GHI SHEET ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    try:
        ws_data, ws_dm = get_sheets()
        dm_all = ws_dm.get_all_values()
        vntime, news = get_now_vntime(), []
        for tx in data.get('transactions', []):
            ma, ten, kho, sl_raw = tx.get('ma_sp'), tx.get('ten_sp'), tx.get('kho'), str(tx.get('so_luong', '0')).lower()
            if ma == "NEW":
                ma = f"SP{len(dm_all) + len(news)}"
                ws_dm.append_row([ma, ten, "1"])
                rate, news = 1, news + [ten]
            else:
                row = next((r for r in dm_all if r[0] == ma), None)
                rate = int(row[2]) if row else 1
            num_match = re.findall(r'\d+', sl_raw)
            num = int(num_match[0]) if num_match else 0
            qty = (num * rate if 't' in sl_raw else num) * (1 if data['type'] == "NHAP" else -1)
            ws_data.append_row([vntime, kho, ma, ten, qty, data['type'], update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ Đã ghi thành công! SP mới: {', '.join(news) if news else 'Không'}")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi ghi Sheet: {e}")
    context.user_data.clear()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("tonkho", ton_kho_cmd))
    app.add_handler(CommandHandler("nhap", lambda u,c: process_manual(u,c,"NHAP")))
    app.add_handler(CommandHandler("xuat", lambda u,c: process_manual(u,c,"XUAT")))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", lambda u,c: u.message.reply_text("Đã hủy.", reply_markup=ReplyKeyboardRemove())))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_interaction))
    app.run_polling()

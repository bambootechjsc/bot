import os
import json
import re
import threading
from datetime import datetime
import pytz
from flask import Flask
from dotenv import load_dotenv
import gspread
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ChatAction

# --- 1. WEB SERVER (NUÔI RENDER) ---
app_web = Flask(__name__)
@app_web.route('/')
def home(): return "Bot is Live!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. CẤU HÌNH ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DANH_SACH_KHO = [["KHO_TONG", "KHO_LE", "KHO_DONG_LANH"]]

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name='models/gemini-2.5-flash',
        generation_config={"temperature": 0.1, "response_mime_type": "application/json"}
    )
else:
    model = None

# --- 3. HELPER FUNCTIONS ---
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def get_now_vntime():
    return datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).strftime("%d/%m/%Y %H:%M:%S")

# --- 4. LỆNH THỦ CÔNG & TỒN KHO ---
async def ton_kho_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    query_date = context.args[0] if context.args else None
    msg = await update.message.reply_text("📊 Đang tính...")
    try:
        ws_data, ws_dm = get_sheets()
        data = ws_data.get_all_values()[1:]
        dm = ws_dm.get_all_values()[1:]
        names = {r[0]: r[1] for r in dm}
        res = {}
        for r in data:
            if len(r) < 5: continue
            if query_date:
                try:
                    if datetime.strptime(r[0].split(' ')[0], "%d/%m/%Y") > datetime.strptime(query_date, "%d/%m/%Y"): continue
                except: pass
            k, m, q = r[1], r[2], int(r[4])
            if k not in res: res[k] = {}
            res[k][m] = res[k].get(m, 0) + q
        
        rep = f"📦 TỒN KHO {query_date if query_date else 'HIỆN TẠI'}\n"
        for k, sps in res.items():
            rep += f"\n🏠 **{k}**\n"
            rep += "\n".join([f"• {names.get(m, m)}: `{s}`" for m, s in sps.items() if s != 0]) or "• (Trống)"
        await msg.edit_text(rep, parse_mode="Markdown")
    except Exception as e: await msg.edit_text(f"❌ Lỗi: {e}")

async def process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if update.message.from_user.id not in ADMIN_IDS: return
    args = context.args
    if len(args) < 3: return await update.message.reply_text(f"⚠️ /{mode.lower()} [kho] [tên] [sl]")
    try:
        kho, sl_raw, search = args[0].upper(), args[-1].lower(), " ".join(args[1:-1])
        ws_data, ws_dm = get_sheets()
        dm = ws_dm.get_all_values()[1:]
        p = next(( {"ma": r[0], "ten": r[1], "rate": int(r[2])} for r in dm if search.lower() in r[1].lower() or search.lower() == r[0].lower() ), None)
        if not p: return await update.message.reply_text(f"❌ Không tìm thấy SP.")
        num = int(re.findall(r'\d+', sl_raw)[0])
        qty = (num * p['rate'] if 't' in sl_raw else num) * (1 if mode == "NHAP" else -1)
        ws_data.append_row([get_now_vntime(), kho, p['ma'], p['ten'], qty, mode, update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ Đã ghi {mode}: {p['ten']} {sl_raw}")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

# --- 5. AI XỬ LÝ ẢNH CHỤP/VIẾT TAY ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    await update.message.reply_chat_action(ChatAction.TYPING)
    photo_file = await update.message.photo[-1].get_file()
    img_data = await photo_file.download_as_bytearray()
    context.user_data['temp_photo_bytes'] = list(img_data)
    await update.message.reply_text("📥 Chọn NHẬP hay XUẤT?", reply_markup=ReplyKeyboardMarkup([["NHAP", "XUAT"]], one_time_keyboard=True, resize_keyboard=True))
    context.user_data['step'] = 'CHOOSING_TYPE'

async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    text, step = update.message.text, context.user_data.get('step')
    
    if step == 'CHOOSING_TYPE' and text in ["NHAP", "XUAT"]:
        context.user_data['temp_type'] = text
        await update.message.reply_text(f"📍 Chọn kho cho phiếu {text}:", reply_markup=ReplyKeyboardMarkup(DANH_SACH_KHO, one_time_keyboard=True, resize_keyboard=True))
        context.user_data['step'] = 'CHOOSING_KHO'
    
    elif step == 'CHOOSING_KHO':
        kho, loai = text, context.user_data.get('temp_type')
        status = await update.message.reply_text("🚀 AI 2.5 đang đọc ảnh...", reply_markup=ReplyKeyboardRemove())
        try:
            img_bytes = bytes(context.user_data.get('temp_photo_bytes'))
            if STORAGE_CHANNEL_ID:
                try: await context.bot.send_photo(STORAGE_CHANNEL_ID, img_bytes, caption=f"📸 {loai} | {kho}")
                except: pass
            
            ws_data, ws_dm = get_sheets()
            dm_txt = "\n".join([f"{r[0]}:{r[1]}" for r in ws_dm.get_all_values()[1:]])
            prompt = f"Đọc ảnh (viết tay/hàng hóa) {loai} vào {kho}. Gộp hương vị. DM: {dm_txt}\nJSON: {{\"type\": \"{loai}\", \"transactions\": [{{\"kho\": \"{kho}\", \"ma_sp\": \"Mã\", \"ten_sp\": \"Tên\", \"so_luong\": \"số+đv\"}}]}}"
            
            response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": img_bytes}])
            data = json.loads(response.text)
            context.user_data['pending_ai'] = data
            
            summary = f"📋 ĐỀ XUẤT (AI 2.5):\n" + "\n".join([f"• {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']])
            
            # FIX LỖI MESSAGE CAN'T BE EDITED Ở ĐÂY
            try:
                await status.edit_text(summary + "\n\n/ok để ghi hoặc /huy.")
            except:
                await update.message.reply_text(summary + "\n\n/ok để ghi hoặc /huy.")
                
        except Exception as e: await update.message.reply_text(f"❌ Lỗi AI: {e}")
        finally: context.user_data['step'] = None

async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    try:
        ws_data, ws_dm = get_sheets()
        dm_all = ws_dm.get_all_values()
        vntime = get_now_vntime()
        for tx in data.get('transactions', []):
            ma, ten, kho, sl_raw = tx.get('ma_sp'), tx.get('ten_sp'), tx.get('kho'), str(tx.get('so_luong', '0')).lower()
            if ma == "NEW":
                ma = f"SP{len(dm_all)}"
                ws_dm.append_row([ma, ten, "1"])
                rate = 1
                dm_all.append([ma, ten, "1"])
            else:
                row = next((r for r in dm_all if r[0] == ma), None)
                rate = int(row[2]) if row else 1
            num = int(re.findall(r'\d+', sl_raw)[0]) if re.findall(r'\d+', sl_raw) else 0
            qty = (num * rate if 't' in sl_raw else num) * (1 if data['type'] == "NHAP" else -1)
            ws_data.append_row([vntime, kho, ma, ten, qty, data['type'], update.message.from_user.full_name, sl_raw])
        await update.message.reply_text("✅ Ghi thành công!")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi ghi: {e}")
    context.user_data.clear()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).connect_timeout(60).read_timeout(60).build()
    
    app.add_handler(CommandHandler("tonkho", ton_kho_cmd))
    app.add_handler(CommandHandler("nhap", lambda u,c: process_manual(u,c,"NHAP")))
    app.add_handler(CommandHandler("xuat", lambda u,c: process_manual(u,c,"XUAT")))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", lambda u,c: u.message.reply_text("Hủy.", reply_markup=ReplyKeyboardRemove())))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_interaction))
    
    app.run_polling()

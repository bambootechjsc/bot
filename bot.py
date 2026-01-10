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
from telegram.constants import ChatAction

# --- 1. CẤU HÌNH ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DANH_SACH_KHO = [["KHO_TONG", "KHO_LE", "KHO_DONG_LANH"]]

# Cấu hình Model Gemini 2.5 Flash
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # Cập nhật model lên bản 2.5-flash
    model = genai.GenerativeModel(
        model_name='models/gemini-2.5-flash',
        generation_config={
            "temperature": 0.1,
            "response_mime_type": "application/json",
        }
    )
else:
    model = None

app_web = Flask(__name__)
@app_web.route('/')
def home(): return "Warehouse Bot Gemini 2.5 Flash is Running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. HÀM TRỢ GIÚP ---
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def get_now_vntime():
    return datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).strftime("%d/%m/%Y %H:%M:%S")

# --- 3. QUY TRÌNH XỬ LÝ ẢNH ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    photo_file = await update.message.photo[-1].get_file()
    img_data = await photo_file.download_as_bytearray()
    context.user_data['temp_photo_bytes'] = list(img_data)
    
    keyboard = [["NHAP", "XUAT"]]
    await update.message.reply_text("📥 Chọn loại giao dịch (Gemini 2.5):", 
                                   reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    context.user_data['step'] = 'CHOOSING_TYPE'

async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    text, step = update.message.text, context.user_data.get('step')

    if step == 'CHOOSING_TYPE' and text in ["NHAP", "XUAT"]:
        context.user_data['temp_type'] = text
        await update.message.reply_text(f"📍 Chọn kho:", 
                                       reply_markup=ReplyKeyboardMarkup(DANH_SACH_KHO, one_time_keyboard=True, resize_keyboard=True))
        context.user_data['step'] = 'CHOOSING_KHO'
    
    elif step == 'CHOOSING_KHO':
        kho, loai = text, context.user_data.get('temp_type')
        status = await update.message.reply_text(f"🚀 Gemini 2.5 Flash đang đọc phiếu...")
        
        try:
            img_bytes = bytes(context.user_data.get('temp_photo_bytes'))

            # Lưu ảnh đối soát sang Telegram Group
            if STORAGE_CHANNEL_ID:
                try: await context.bot.send_photo(chat_id=STORAGE_CHANNEL_ID, photo=img_bytes, caption=f"📸 {loai} | {kho}")
                except: pass

            ws_data, ws_dm = get_sheets()
            dm_txt = "\n".join([f"{r[0]}:{r[1]}" for r in ws_dm.get_all_values()[1:]])
            
            prompt = f"""Phân tích ảnh phiếu {loai} vào kho {kho}.
            Danh mục (mã:tên):
            {dm_txt}
            
            Yêu cầu: 
            - Trích xuất SP và số lượng. 
            - Nếu SP chưa có trong danh mục, mã là 'NEW'.
            - Luôn trả về JSON theo mẫu:
            {{"type": "{loai}", "transactions": [{{"kho": "{kho}", "ma_sp": "Mã", "ten_sp": "Tên", "so_luong": "10c"}}]}}"""
            
            response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": img_bytes}])
            data = json.loads(response.text)
            context.user_data['pending_ai'] = data

            summary = f"📋 **KẾT QUẢ TỪ GEMINI 2.5:**\n" + "\n".join([f"• {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']])
            await status.edit_text(summary + "\n\n/ok để xác nhận hoặc /huy.")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {str(e)}")
        finally:
            context.user_data['step'] = None

# --- 4. XÁC NHẬN GHI SHEET ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    try:
        ws_data, ws_dm = get_sheets()
        dm_all = ws_dm.get_all_values()
        vntime = get_now_vntime()
        for tx in data.get('transactions', []):
            ma, ten, kho, sl_raw = tx.get('ma_sp'), tx.get('ten_sp'), tx.get('kho'), str(tx.get('so_luong', '0')).lower()
            
            # Tính toán số lượng thực tế
            nums = re.findall(r'\d+', sl_raw)
            num = int(nums[0]) if nums else 0
            row = next((r for r in dm_all if r[0] == ma), None)
            rate = int(row[2]) if row else 1
            
            qty = (num * rate if 't' in sl_raw else num) * (1 if data['type'] == "NHAP" else -1)
            ws_data.append_row([vntime, kho, ma, ten, qty, data['type'], update.message.from_user.full_name, sl_raw])
        
        await update.message.reply_text(f"✅ Đã ghi vào Google Sheet thành công!")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi ghi dữ liệu: {e}")
    context.user_data.clear()

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).connect_timeout(60).read_timeout(60).build()
    
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", lambda u,c: u.message.reply_text("Đã hủy.", reply_markup=ReplyKeyboardRemove())))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_interaction))
    
    app.run_polling()

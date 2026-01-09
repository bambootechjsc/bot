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
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# --- 1. CẤU HÌNH ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    model = None

app_web = Flask(__name__)
@app_web.route('/')
def home(): return "Bot GMT+7 is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. HÀM HỖ TRỢ ---
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def get_now_vntime():
    vntime = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    return vntime.strftime("%d/%m/%Y %H:%M:%S")

def find_product_by_name(search_term, dm_data):
    search_term = search_term.strip().lower()
    for row in dm_data:
        if len(row) < 3: continue
        if search_term == row[1].lower() or search_term == row[0].lower():
            return {"ma": row[0], "ten": row[1], "rate": int(row[2])}
    return None

# --- 3. XỬ LÝ ẢNH AI ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🤖 Gemini 2.5 đang đọc phiếu & kiểm tra danh mục...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_byte = await photo_file.download_as_bytearray()
        
        prompt = "Đọc ảnh phiếu kho. Trả về JSON: {\"type\": \"XUAT\", \"transactions\": [{\"kho\": \"KHO1\", \"ten_sp\": \"Coca\", \"so_luong\": \"10c\"}]}"
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img_byte)}])
        data = json.loads(re.sub(r'```json|```', '', response.text).strip())
        
        context.user_data['pending_ai'] = data
        confirm = f"✅ **KẾT QUẢ ĐỌC PHIẾU ({data['type']}):**\n"
        for t in data['transactions']:
            confirm += f"• {t['ten_sp']}: {t['so_luong']}\n"
        await msg.edit_text(confirm + "\nBấm /ok để xác nhận ghi kho (Bot sẽ tự thêm SP mới nếu chưa có).")
    except Exception as e: await msg.edit_text(f"❌ Lỗi: {e}")

# --- 4. XÁC NHẬN & TỰ ĐỘNG CẬP NHẬT SP MỚI ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    
    ws_data, ws_dm = get_sheets()
    dm_all = ws_dm.get_all_values()
    dm_rows = dm_all[1:]
    
    success_logs = []
    new_prods = []
    vntime = get_now_vntime()

    for tx in data['transactions']:
        ten_ai = tx['ten_sp']
        p = find_product_by_name(ten_ai, dm_rows)
        
        # Nếu chưa có SP, tự động thêm vào DANH_MUC
        if not p:
            new_ma = f"SP{len(dm_all) + len(new_prods)}"
            ws_dm.append_row([new_ma, ten_ai, "1"]) # Mặc định 1t = 1c
            p = {"ma": new_ma, "ten": ten_ai, "rate": 1}
            new_prods.append(f"{ten_ai} ({new_ma})")

        sl_raw = tx['so_luong'].lower()
        num = int(re.findall(r'\d+', sl_raw)[0])
        qty = num * (p['rate'] if 't' in sl_raw else 1)
        final_qty = qty if data['type'] == "NHAP" else -abs(qty)
        
        ws_data.append_row([vntime, tx['kho'], p['ma'], p['ten'], final_qty, data['type'], update.message.from_user.full_name, sl_raw])
        success_logs.append(f"{p['ten']} ({sl_raw})")
    
    report = "✅ **HOÀN TẤT GHI KHO:**\n" + "\n".join([f"➕ {item}" for item in success_logs])
    if new_prods:
        report += "\n\n✨ **SP MỚI ĐÃ THÊM:**\n" + "\n".join([f"🆕 {np}" for np in new_prods])
    
    context.user_data.pop('pending_ai')
    await update.message.reply_text(report, parse_mode="Markdown")

# --- 5. LỆNH NHẬP XUẤT THỦ CÔNG ---
async def process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    args = context.args
    if len(args) < 3: return await update.message.reply_text(f"⚠️ /{mode.lower()} [kho] [tên sp] [sl]")
    try:
        kho, sl_raw, search_term = args[0].upper(), args[-1].lower(), " ".join(args[1:-1])
        ws_data, ws_dm = get_sheets()
        p = find_product_by_name(search_term, ws_dm.get_all_values()[1:])
        if not p: return await update.message.reply_text(f"❌ '{search_term}' chưa có trong danh mục.")
        
        qty = int(re.findall(r'\d+', sl_raw)[0]) * (p['rate'] if 't' in sl_raw else 1)
        ws_data.append_row([get_now_vntime(), kho, p['ma'], p['ten'], (qty if mode=="NHAP" else -qty), mode, update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ Đã ghi: {p['ten']} {sl_raw}")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

# --- KHỞI CHẠY ---
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("nhap", lambda u,c: process_manual(u,c,"NHAP")))
    app.add_handler(CommandHandler("xuat", lambda u,c: process_manual(u,c,"XUAT")))
    app.run_polling()

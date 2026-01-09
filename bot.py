import os
import json
import re
import io
import threading
from datetime import datetime
from dotenv import load_dotenv
import gspread
import google.generativeai as genai
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# 1. LOAD CẤU HÌNH
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. KHỞI TẠO GEMINI AI (Đặt ở đây để tránh lỗi 'not defined')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None
    print("⚠️ Thiếu GEMINI_API_KEY")

# 3. WEB SERVER ĐỂ TRÁNH LỖI PORT TRÊN RENDER
app_web = Flask(__name__)

@app_web.route('/')
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# 4. CÁC HÀM HỖ TRỢ GOOGLE SHEETS
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def find_product_by_name(search_term, dm_data):
    search_term = search_term.strip().lower()
    matches = []
    for row in dm_data:
        if len(row) < 3: continue
        if search_term in row[1].lower() or search_term == row[0].lower():
            matches.append({"ma": row[0], "ten": row[1], "rate": int(row[2])})
    return matches

# 5. XỬ LÝ NHẬP/XUẤT THỦ CÔNG
async def process_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(f"⚠️ /{mode.lower()} [kho] [tên sp] [sl+t/c]")
        return
    try:
        kho, sl_raw, search_term = args[0].upper(), args[-1].lower(), " ".join(args[1:-1])
        ws_data, ws_dm = get_sheets()
        products = find_product_by_name(search_term, ws_dm.get_all_values()[1:])
        if not products: return await update.message.reply_text(f"❌ Không thấy SP '{search_term}'")
        
        p = products[0]
        qty = int(sl_raw[:-1]) * (p['rate'] if sl_raw.endswith('t') else 1)
        final_qty = qty if mode == "NHAP" else -abs(qty)
        
        ws_data.append_row([datetime.now().strftime("%d/%m/%Y %H:%M:%S"), kho, p['ma'], p['ten'], final_qty, mode, update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ {mode}: {p['ten']} ({sl_raw})")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

async def nhap(u, c): await process_transaction(u, c, "NHAP")
async def xuat(u, c): await process_transaction(u, c, "XUAT")

# 6. XỬ LÝ ẢNH AI (GEMINI)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    if not model: return await update.message.reply_text("❌ Chưa cấu hình Gemini API Key.")

    msg = await update.message.reply_text("🤖 AI đang đọc phiếu...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_byte = await photo_file.download_as_bytearray()
        img_part = {"mime_type": "image/jpeg", "data": bytes(img_byte)}
        
        prompt = """
        Đọc ảnh phiếu kho: 1. Loại: XUAT (trừ khi có chữ NHAP). 
        2. SP: Tên bên trái, SL là kết quả cuối phép tính. 
        3. Đơn vị: mặc định 'c'. 
        Trả về JSON: {"type": "XUAT", "transactions": [{"kho": "KHO_TONG", "ten_sp": "Coca", "so_luong": "65c"}]}
        """
        
        response = model.generate_content([prompt, img_part])
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        data = json.loads(clean_json)
        
        context.user_data['pending_ai'] = data
        confirm = f"✅ AI đọc được {data['type']}:\n" + "\n".join([f"- {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']]) + "\n\nBấm /ok để ghi hoặc /huy để bỏ."
        await msg.edit_text(confirm)
    except Exception as e: await msg.edit_text(f"❌ Lỗi AI: {e}")

async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    ws_data, ws_dm = get_sheets()
    dm_data = ws_dm.get_all_values()[1:]
    for tx in data['transactions']:
        p_list = find_product_by_name(tx['ten_sp'], dm_data)
        if p_list:
            p = p_list[0]
            qty = int(tx['so_luong'][:-1]) * (p['rate'] if tx['so_luong'].endswith('t') else 1)
            ws_data.append_row([datetime.now().strftime("%d/%m/%Y %H:%M:%S"), tx['kho'], p['ma'], p['ten'], (qty if data['type'] == "NHAP" else -qty), data['type'], update.message.from_user.full_name, tx['so_luong']])
    context.user_data.pop('pending_ai')
    await update.message.reply_text("🎉 Đã ghi kho thành công!")

# 7. THỐNG KÊ BIẾN ĐỘNG
async def thongketheogio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Cú pháp: /thongketheogio 01/01/2026 10/01/2026")
    try:
        start, end = datetime.strptime(args[0], "%d/%m/%Y"), datetime.strptime(args[1], "%d/%m/%Y")
        ws_data, ws_dm = get_sheets()
        logs, dm = ws_data.get_all_values()[1:], ws_dm.get_all_values()[1:]
        names = {r[0]: r[1] for r in dm}
        res = {}
        for r in logs:
            dt = datetime.strptime(r[0].split()[0], "%d/%m/%Y")
            if start <= dt <= end:
                k, m, q, tp = r[1], r[2], int(r[4]), r[5]
                if k not in res: res[k] = {}
                if m not in res[k]: res[k][m] = {"nhap": 0, "xuat": 0}
                if tp == "NHAP": res[k][m]["nhap"] += abs(q)
                else: res[k][m]["xuat"] += abs(q)
        msg = f"📅 Thống kê {args[0]}-{args[1]}:\n"
        for k, items in res.items():
            msg += f"\n🏠 Kho: {k}\n" + "\n".join([f"• {names.get(m, m)}: +{t['nhap']}c, -{t['xuat']}c" for m, t in items.items()])
        await update.message.reply_text(msg)
    except Exception as e: await update.message.reply_text(f"Lỗi: {e}")

# 8. KHỞI CHẠY
if __name__ == "__main__":
    # Chạy Web Server ở luồng riêng để tránh lỗi Port trên Render
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("nhap", nhap))
    app.add_handler(CommandHandler("xuat", xuat))
    app.add_handler(CommandHandler("thongketheogio", thongketheogio))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    print("Bot is running...")
    app.run_polling()

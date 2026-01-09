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

# --- 1. CẤU HÌNH HỆ THỐNG ---
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Khởi tạo Gemini 2.5 Flash
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    model = None

# --- 2. WEB SERVER GIẢ (FIX LỖI PORT RENDER) ---
app_web = Flask(__name__)
@app_web.route('/')
def home(): return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 3. HÀM HỖ TRỢ GOOGLE SHEETS ---
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

# --- 4. XỬ LÝ NHẬP/XUẤT THỦ CÔNG ---
async def process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(f"⚠️ Cú pháp: /{mode.lower()} [kho] [tên sp] [sl+t/c]")
        return
    try:
        kho, sl_raw, search_term = args[0].upper(), args[-1].lower(), " ".join(args[1:-1])
        ws_data, ws_dm = get_sheets()
        prods = find_product_by_name(search_term, ws_dm.get_all_values()[1:])
        
        if not prods: return await update.message.reply_text(f"❌ Không tìm thấy SP '{search_term}'")
        p = prods[0]
        qty = int(re.findall(r'\d+', sl_raw)[0]) * (p['rate'] if 't' in sl_raw else 1)
        final_qty = qty if mode == "NHAP" else -abs(qty)
        
        ws_data.append_row([datetime.now().strftime("%d/%m/%Y %H:%M:%S"), kho, p['ma'], p['ten'], final_qty, mode, update.message.from_user.full_name, sl_raw])
        await update.message.reply_text(f"✅ {mode} thành công: {p['ten']} ({sl_raw})")
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

async def nhap_cmd(u, c): await process_manual(u, c, "NHAP")
async def xuat_cmd(u, c): await process_manual(u, c, "XUAT")

# --- 5. XỬ LÝ ẢNH AI (GEMINI 2.5 FLASH) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🤖 Gemini 2.5 Flash đang đọc phiếu...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_byte = await photo_file.download_as_bytearray()
        
        prompt = (
            "Bạn là kế toán kho. Đọc ảnh này: 1. Loại: XUAT (trừ khi có chữ NHAP). "
            "2. SP: Tên bên trái, SL là kết quả cuối phép tính. 3. Đơn vị: mặc định 'c'. "
            "Trả về DUY NHẤT JSON: {\"type\": \"XUAT\", \"transactions\": [{\"kho\": \"KHO_TONG\", \"ten_sp\": \"Coca\", \"so_luong\": \"65c\"}]}"
        )
        
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img_byte)}])
        data = json.loads(re.sub(r'```json|```', '', response.text).strip())
        
        context.user_data['pending_ai'] = data
        confirm = f"✅ **AI ĐỌC ĐƯỢC {data['type']}:**\n" + "\n".join([f"• {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']])
        await msg.edit_text(confirm + "\n\nBấm /ok để xác nhận hoặc /huy để bỏ.")
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
            sl_raw = tx['so_luong'].lower()
            qty = int(re.findall(r'\d+', sl_raw)[0]) * (p['rate'] if 't' in sl_raw else 1)
            ws_data.append_row([datetime.now().strftime("%d/%m/%Y %H:%M:%S"), tx['kho'], p['ma'], p['ten'], (qty if data['type'] == "NHAP" else -qty), data['type'], update.message.from_user.full_name, tx['so_luong']])
    
    context.user_data.pop('pending_ai')
    await update.message.reply_text("🎉 Đã ghi kho thành công!")

# --- 6. LỆNH THỐNG KÊ ---
async def thongketheogio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Cú pháp: /thongke 01/01/2026 10/01/2026")
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
        
        msg = f"📅 Thống kê {args[0]} - {args[1]}:\n"
        for k, items in res.items():
            msg += f"\n🏠 Kho: {k}\n" + "\n".join([f"• {names.get(m, m)}: +{t['nhap']}c, -{t['xuat']}c" for m, t in items.items()])
        await update.message.reply_text(msg)
    except Exception as e: await update.message.reply_text(f"Lỗi: {e}")

async def cancel(u, c):
    c.user_data.pop('pending_ai', None)
    await u.message.reply_text("❌ Đã hủy.")

# --- 7. CHẠY BOT ---
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("nhap", nhap_cmd))
    app.add_handler(CommandHandler("xuat", xuat_cmd))
    app.add_handler(CommandHandler("thongke", thongketheogio))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", cancel))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    print("Bot is live with Gemini 2.5 Flash!")
    app.run_polling()

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
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID") # ID Group lưu trữ ảnh
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
def home(): return "Bot Warehouse Live!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. HÀM TRỢ GIÚP ---
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

# --- 3. XỬ LÝ ẢNH & LƯU TRỮ ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    
    # Thông báo bắt đầu
    msg = await update.message.reply_text("📸 Đang lưu trữ ảnh & đọc dữ liệu...")
    
    try:
        # Tải ảnh từ Telegram
        photo_file = await update.message.photo[-1].get_file()
        img_byte = await photo_file.download_as_bytearray()
        vntime = get_now_vntime()
        user_name = update.message.from_user.full_name

        # PHƯƠNG ÁN 2: Gửi ảnh sang Group lưu trữ
        if STORAGE_CHANNEL_ID:
            caption = f"📄 PHIẾU KHO MỚI\n⏰ Thời gian: {vntime}\n👤 Người gửi: {user_name}"
            await context.bot.send_photo(chat_id=STORAGE_CHANNEL_ID, photo=bytes(img_byte), caption=caption)
        
        # Gửi sang AI xử lý
        prompt = (
            "Bạn là kế toán kho. Đọc ảnh phiếu kho này.\n"
            "Trả về JSON: {\"type\": \"XUAT\", \"transactions\": [{\"kho\": \"KHO1\", \"ten_sp\": \"Coca\", \"so_luong\": \"10c\"}]}"
        )
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img_byte)}])
        data = json.loads(re.sub(r'```json|```', '', response.text).strip())
        
        context.user_data['pending_ai'] = data
        confirm = f"✅ **AI ĐỌC THÀNH CÔNG ({data['type']}):**\n"
        for t in data['transactions']:
            confirm += f"• {t.get('ten_sp', 'N/A')}: {t.get('so_luong', 'N/A')} ({t.get('kho', 'N/A')})\n"
        await msg.edit_text(confirm + "\nBấm /ok để ghi kho hoặc /huy để bỏ qua.")

    except Exception as e:
        await msg.edit_text(f"❌ Lỗi: {str(e)}")

# --- 4. XÁC NHẬN GHI SHEET ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    
    ws_data, ws_dm = get_sheets()
    dm_all = ws_dm.get_all_values()
    dm_rows = dm_all[1:]
    
    success_logs, error_logs, new_prods = [], [], []
    vntime = get_now_vntime()

    for tx in data.get('transactions', []):
        ten_ai = str(tx.get('ten_sp', '')).strip()
        kho_ai = str(tx.get('kho', '')).strip().upper()
        sl_raw = str(tx.get('so_luong', '')).strip().lower()

        if not ten_ai or ten_ai == "N/A":
            error_logs.append("Dòng lỗi: Thiếu tên SP")
            continue

        num_match = re.findall(r'\d+', sl_raw)
        if not num_match:
            error_logs.append(f"{ten_ai}: SL '{sl_raw}' không hợp lệ")
            continue

        try:
            p = find_product_by_name(ten_ai, dm_rows)
            if not p:
                new_ma = f"SP{len(dm_all) + len(new_prods)}"
                ws_dm.append_row([new_ma, ten_ai, "1"])
                p = {"ma": new_ma, "ten": ten_ai, "rate": 1}
                new_prods.append(f"{ten_ai} ({new_ma})")

            num = int(num_match[0])
            qty = num * (p['rate'] if 't' in sl_raw else 1)
            final_qty = qty if data['type'] == "NHAP" else -abs(qty)
            
            ws_data.append_row([vntime, kho_ai, p['ma'], p['ten'], final_qty, data['type'], update.message.from_user.full_name, sl_raw])
            success_logs.append(f"{p['ten']} ({sl_raw})")
        except Exception as e:
            error_logs.append(f"{ten_ai}: {str(e)}")
    
    report = f"📊 **KẾT QUẢ GHI KHO ({data['type']})**\n"
    if success_logs: report += "✅ **XONG:**\n" + "\n".join([f"• {i}" for i in success_logs]) + "\n\n"
    if new_prods: report += "🆕 **SP MỚI:**\n" + "\n".join([f"• {n}" for n in new_prods]) + "\n\n"
    if error_logs: report += "❌ **LỖI:**\n" + "\n".join([f"• {e}" for e in error_logs])
    
    context.user_data.pop('pending_ai', None)
    await update.message.reply_text(report, parse_mode="Markdown")

# --- 5. LỆNH KHÁC ---
async def cancel(u, c):
    c.user_data.pop('pending_ai', None)
    await u.message.reply_text("❌ Đã hủy.")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", cancel))
    app.run_polling()

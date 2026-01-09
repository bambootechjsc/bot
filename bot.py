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
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
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
def home(): return "Bot Warehouse Precision is Running!"

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

# --- 3. XỬ LÝ ẢNH VỚI DANH MỤC CÓ SẴN ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    
    status_msg = await update.message.reply_text("🔍 Đang tải danh mục & phân tích ảnh...")
    
    try:
        # Lấy danh mục sản phẩm từ Sheet để gửi cho AI
        ws_data, ws_dm = get_sheets()
        dm_rows = ws_dm.get_all_values()[1:] # Bỏ header
        danh_sach_sp_text = "\n".join([f"- {r[1]} (Mã: {r[0]})" for r in dm_rows if len(r) > 1])

        # Tải ảnh
        photo_file = await update.message.photo[-1].get_file()
        img_byte = await photo_file.download_as_bytearray()
        
        # Lưu trữ ảnh vào Group trước
        if STORAGE_CHANNEL_ID:
            try:
                vntime = get_now_vntime()
                cap = f"📸 Phiếu đối soát\n⏰ {vntime}\n👤 {update.message.from_user.full_name}"
                await context.bot.send_photo(chat_id=STORAGE_CHANNEL_ID, photo=bytes(img_byte), caption=cap)
            except Exception as e: print(f"Lỗi gửi ảnh lưu trữ: {e}")

        # PROMPT NÂNG CAO: Gửi kèm danh sách sản phẩm thực tế
        prompt = f"""
Bạn là chuyên gia kiểm kho. Hãy đọc ảnh phiếu kho được gửi kèm.
DANH SÁCH SẢN PHẨM TRONG KHO:
{danh_sach_sp_text}

NHIỆM VỤ:
1. Xác định loại phiếu: NHAP hoặc XUAT.
2. Với mỗi dòng trong ảnh, hãy tìm sản phẩm khớp nhất trong "DANH SÁCH SẢN PHẨM TRONG KHO" ở trên.
3. Nếu sản phẩm trong ảnh không có trong danh sách, hãy ghi đúng tên sản phẩm đó và đánh dấu mã là "NEW".
4. Số lượng: Lấy con số cuối cùng (ví dụ: 5+2=7 thì lấy 7). Đơn vị mặc định là 'c'.

TRẢ VỀ DUY NHẤT JSON:
{{
  "type": "XUAT",
  "transactions": [
    {{"kho": "KHO1", "ma_sp": "Mã tìm được hoặc NEW", "ten_sp": "Tên sản phẩm khớp nhất", "so_luong": "10c"}}
  ]
}}
"""
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img_byte)}])
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        data = json.loads(clean_json)
        
        context.user_data['pending_ai'] = data
        
        res_text = f"🎯 **KẾT QUẢ ĐỐI CHIẾU DANH MỤC ({data['type']}):**\n"
        for t in data['transactions']:
            status = "✨ Mới" if t['ma_sp'] == "NEW" else f"🆔 {t['ma_sp']}"
            res_text += f"• {t['ten_sp']} [{status}]: {t['so_luong']}\n"
        
        await status_msg.edit_text(res_text + "\nBấm /ok để xác nhận ghi Sheet.")

    except Exception as e:
        await status_msg.edit_text(f"❌ Lỗi xử lý: {str(e)}")

# --- 4. XÁC NHẬN GHI (Sử dụng Mã SP AI đã tìm được) ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    
    ws_data, ws_dm = get_sheets()
    dm_all = ws_dm.get_all_values()
    vntime = get_now_vntime()
    success_logs, new_prods, error_logs = [], [], []

    for tx in data['transactions']:
        try:
            ma_sp = tx.get('ma_sp')
            ten_sp = tx.get('ten_sp')
            kho = tx.get('kho', 'KHO1').upper()
            sl_raw = tx.get('so_luong', '0c').lower()
            
            # Nếu là SP mới
            if ma_sp == "NEW":
                ma_sp = f"SP{len(dm_all) + len(new_prods)}"
                ws_dm.append_row([ma_sp, ten_sp, "1"])
                rate = 1
                new_prods.append(f"{ten_sp} ({ma_sp})")
            else:
                # Lấy tỷ lệ quy đổi từ danh mục cũ
                row = next((r for r in dm_all if r[0] == ma_sp), None)
                rate = int(row[2]) if row else 1

            num = int(re.findall(r'\d+', sl_raw)[0])
            qty = num * rate if 't' in sl_raw else num
            final_qty = qty if data['type'] == "NHAP" else -abs(qty)
            
            ws_data.append_row([vntime, kho, ma_sp, ten_sp, final_qty, data['type'], update.message.from_user.full_name, sl_raw])
            success_logs.append(f"{ten_sp} ({sl_raw})")
        except Exception as e:
            error_logs.append(f"{tx.get('ten_sp')}: {e}")

    report = f"📊 **HOÀN TẤT GHI KHO**\n"
    if success_logs: report += "✅ Thành công:\n" + "\n".join(success_logs)
    if new_prods: report += "\n\n🆕 SP mới đã thêm:\n" + "\n".join(new_prods)
    if error_logs: report += "\n\n❌ Lỗi:\n" + "\n".join(error_logs)
    
    context.user_data.pop('pending_ai', None)
    await update.message.reply_text(report)

# --- KHỞI CHẠY ---
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.run_polling()

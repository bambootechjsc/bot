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

# Tên các kho thực tế của bạn
DANH_SACH_KHO = [["KHO_TONG", "KHO_LE", "KHO_DONG_LANH"]]

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash') # Hoặc gemini-1.5-flash
else:
    model = None

# Flask để giữ Bot sống trên Hosting
app_web = Flask(__name__)
@app_web.route('/')
def home(): return "Warehouse Management System is Running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

# --- 2. HÀM TƯƠNG TÁC GOOGLE SHEETS ---
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

def get_now_vntime():
    return datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).strftime("%d/%m/%Y %H:%M:%S")

# --- 3. TÍNH NĂNG TỒN KHO ---
async def ton_kho_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("📊 Đang truy xuất dữ liệu tồn kho...")
    try:
        ws_data, ws_dm = get_sheets()
        data_rows = ws_data.get_all_values()[1:]
        dm_rows = ws_dm.get_all_values()[1:]
        
        names = {r[0]: r[1] for r in dm_rows}
        ton_kho_dict = {} # {Kho: {MaSP: SoLuong}}

        for r in data_rows:
            if len(r) < 5: continue
            kho, ma, sl = r[1], r[2], int(r[4])
            if kho not in ton_kho_dict: ton_kho_dict[kho] = {}
            ton_kho_dict[kho][ma] = ton_kho_dict[kho].get(ma, 0) + sl

        report = "📦 **BÁO CÁO TỒN KHO CHI TIẾT**\n"
        for kho, sps in ton_kho_dict.items():
            report += f"\n🏠 **{kho}**\n"
            items_list = [f"• {names.get(m, m)}: `{s}` c" for m, s in sps.items() if s != 0]
            report += "\n".join(items_list) if items_list else "• (Hết hàng)"
            report += "\n"
        
        await msg.edit_text(report, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi tồn kho: {e}")

# --- 4. XỬ LÝ GỬI ẢNH (NHẬP/XUẤT/KHO) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    
    # Tải ảnh tạm thời
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['temp_photo_bytes'] = await photo_file.download_as_bytearray()
    
    # Bước 1: Hỏi Nhập hay Xuất
    keyboard = [["NHAP", "XUAT"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("📥 Bạn muốn thực hiện giao dịch gì?", reply_markup=reply_markup)
    context.user_data['step'] = 'CHOOSING_TYPE'

async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS: return
    text = update.message.text
    step = context.user_data.get('step')

    # Xử lý chọn loại NHAP/XUAT
    if step == 'CHOOSING_TYPE':
        if text in ["NHAP", "XUAT"]:
            context.user_data['temp_type'] = text
            reply_markup = ReplyKeyboardMarkup(DANH_SACH_KHO, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(f"📍 Chọn kho cho phiếu {text}:", reply_markup=reply_markup)
            context.user_data['step'] = 'CHOOSING_KHO'
        return

    # Xử lý chọn KHO và gọi AI
    if step == 'CHOOSING_KHO':
        kho_selected = text
        loai = context.user_data.get('temp_type')
        status_msg = await update.message.reply_text(f"🤖 AI đang đọc phiếu {loai} tại {kho_selected}...", reply_markup=ReplyKeyboardRemove())
        
        try:
            ws_data, ws_dm = get_sheets()
            dm_rows = ws_dm.get_all_values()[1:]
            danh_sach_sp_text = "\n".join([f"- {r[1]} (Mã: {r[0]})" for r in dm_rows])
            
            img_bytes = context.user_data.get('temp_photo_bytes')

            # Lưu ảnh vào kênh lưu trữ
            if STORAGE_CHANNEL_ID:
                await context.bot.send_photo(chat_id=STORAGE_CHANNEL_ID, photo=bytes(img_bytes), 
                                             caption=f"📝 {loai} | {kho_selected} | {get_now_vntime()}")

            prompt = f"""
            Bạn là một trợ lý kế toán kho chuyên nghiệp. Hãy đọc ảnh đính kèm và trích xuất dữ liệu.
            Hành động: {loai} hàng vào Kho: {kho_selected}.
            Danh sách sản phẩm hiện có (Mã - Tên):
            {danh_sach_sp_text}

            Yêu cầu:
            - So khớp tên SP trong ảnh với danh sách trên.
            - Nếu sản phẩm mới hoàn toàn, hãy để mã_sp là "NEW".
            - Trả về JSON theo cấu trúc: 
            {{"type": "{loai}", "transactions": [{{"kho": "{kho_selected}", "ma_sp": "Mã", "ten_sp": "Tên", "so_luong": "10c hoặc 1t"}}]}}
            """
            
            response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(img_bytes)}])
            cleaned_json = re.sub(r'```json|```', '', response.text).strip()
            data = json.loads(cleaned_json)
            
            context.user_data['pending_ai'] = data
            summary = f"🎯 **ĐỀ XUẤT TỪ AI ({loai}):**\n🏠 Kho: {kho_selected}\n"
            summary += "\n".join([f"• {t['ten_sp']}: {t['so_luong']}" for t in data['transactions']])
            await status_msg.edit_text(summary + "\n\nBấm /ok để xác nhận ghi sổ hoặc /huy.")
            context.user_data['step'] = None
        except Exception as e:
            await status_msg.edit_text(f"❌ Lỗi phân tích: {e}")

# --- 5. LỆNH XÁC NHẬN GHI DỮ LIỆU ---
async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get('pending_ai')
    if not data: return
    
    ws_data, ws_dm = get_sheets()
    dm_all = ws_dm.get_all_values()
    vntime, user_name = get_now_vntime(), update.message.from_user.full_name
    success_count = 0

    for tx in data.get('transactions', []):
        try:
            ma, ten, kho, sl_raw = tx.get('ma_sp'), tx.get('ten_sp'), tx.get('kho'), tx.get('so_luong', '0').lower()
            
            # Xử lý sản phẩm mới
            if ma == "NEW":
                ma = f"SP{len(dm_all) + 1}"
                ws_dm.append_row([ma, ten, "1"])
                rate = 1
                dm_all.append([ma, ten, "1"]) # Cập nhật danh sách tạm
            else:
                row = next((r for r in dm_all if r[0] == ma), None)
                rate = int(row[2]) if row else 1

            # Tính toán số lượng (thùng -> cái)
            num_match = re.findall(r'\d+', sl_raw)
            num = int(num_match[0]) if num_match else 0
            qty_calc = (num * rate if 't' in sl_raw else num)
            final_qty = qty_calc if data['type'] == "NHAP" else -qty_calc
            
            # Ghi vào Sheet DATA
            ws_data.append_row([vntime, kho, ma, ten, final_qty, data['type'], user_name, sl_raw])
            success_count += 1
        except: continue

    await update.message.reply_text(f"✅ Đã ghi thành công {success_count} sản phẩm vào sổ kho.")
    context.user_data.clear()

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Đã hủy bỏ thao tác.", reply_markup=ReplyKeyboardRemove())

# --- KHỞI CHẠY BOT ---
if __name__ == "__main__":
    # Chạy Web Server trong một luồng riêng
    threading.Thread(target=run_web, daemon=True).start()
    
    # Chạy Telegram Bot
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("tonkho", ton_kho_cmd))
    app.add_handler(CommandHandler("ok", confirm_ok))
    app.add_handler(CommandHandler("huy", cancel_action))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_interaction))
    
    print("Bot is starting...")
    app.run_polling()

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import gspread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load biến môi trường
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
# Render sẽ đọc chuỗi JSON này từ Environment Variables
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))

# Khởi tạo Google Sheets
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

# --- HÀM TRỢ GIÚP ---
def get_conversion_rate(ma_sp, dm_data):
    """dm_data là danh sách từ worksheet DANH_MUC"""
    for row in dm_data:
        if row[0].upper() == ma_sp.upper():
            return int(row[2])
    return 1 # Mặc định là 1 nếu không tìm thấy

# --- LỆNH NHẬP / XUẤT ---
async def process_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    args = context.args
    user = update.message.from_user
    full_name = user.full_name if user.full_name else f"@{user.username}"
    
    if len(args) < 4:
        await update.message.reply_text(f"⚠️ Cú pháp: /{mode.lower()} [kho] [mã] [tên] [sl+t/c]\nVí dụ: /nhap KHO1 BIA Bia 10t")
        return

    try:
        kho, ma = args[0].upper(), args[1].upper()
        sl_raw = args[-1].lower()
        ten = " ".join(args[2:-1])
        
        ws_data, ws_dm = get_sheets()
        dm_data = ws_dm.get_all_values()[1:]
        
        # Xử lý đơn vị
        rate = get_conversion_rate(ma, dm_data)
        if sl_raw.endswith('t'):
            don_vi_goc = f"{sl_raw[:-1]} Thùng"
            qty = int(sl_raw[:-1]) * rate
        elif sl_raw.endswith('c'):
            don_vi_goc = f"{sl_raw[:-1]} Chai"
            qty = int(sl_raw[:-1])
        else:
            await update.message.reply_text("❌ Thiếu đơn vị! Thêm 't' (thùng) hoặc 'c' (chai).")
            return

        final_qty = qty if mode == "NHAP" else -abs(qty)

        # Ghi vào Sheet DATA
        row = [
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            kho, ma, ten, final_qty, mode, full_name, don_vi_goc, str(user.id)
        ]
        ws_data.append_row(row)
        
        await update.message.reply_text(
            f"✅ {mode} thành công!\n📦 SP: {ten}\n🔢 Tổng quy đổi: {abs(final_qty)} chai\n👤 Người: {full_name}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

async def nhap(u, c): await process_transaction(u, c, "NHAP")
async def xuat(u, c): await process_transaction(u, c, "XUAT")

# --- LỆNH TỒN KHO ---
async def tonkho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ws_data, ws_dm = get_sheets()
        data = ws_data.get_all_values()[1:]
        dm = ws_dm.get_all_values()[1:]
        
        conv_map = {r[0].upper(): int(r[2]) for r in dm}
        name_map = {r[0].upper(): r[1] for r in dm}
        
        inventory = {}
        for r in data:
            k, m, q = r[1], r[2], int(r[4])
            if k not in inventory: inventory[k] = {}
            inventory[k][m] = inventory[k].get(m, 0) + q

        search_kho = context.args[0].upper() if context.args else None
        msg = "📊 **TỒN KHO CHI TIẾT**\n"
        
        for kho, items in inventory.items():
            if search_kho and kho != search_kho: continue
            msg += f"\n🏢 **KHO: {kho}**\n"
            for ma, total in items.items():
                if total == 0: continue
                rate = conv_map.get(ma, 1)
                t, c = total // rate, total % rate
                res = f"{t} thùng " if t > 0 else ""
                res += f"{c} chai" if c > 0 else ""
                msg += f"• `{ma}`: {res} ({total} chai)\n"
        
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("nhap", nhap))
    app.add_handler(CommandHandler("xuat", xuat))
    app.add_handler(CommandHandler("tonkho", tonkho))
    print("Bot is running...")
    app.run_polling()

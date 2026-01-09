import os
import json
import gspread
from datetime import datetime
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# Load biến môi trường
load_dotenv()

# Cấu hình Google Sheets
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Xử lý lấy Credentials từ file hoặc biến môi trường (để deploy server)
creds_json = os.getenv("GOOGLE_SHEETS_CREDS_JSON")
if creds_json:
    creds_dict = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)

client = gspread.authorize(creds)
spreadsheet = client.open_by_key(os.getenv("SHEET_ID"))
inventory_sheet = spreadsheet.get_worksheet(0)  # Tab đầu tiên: Tồn kho
history_sheet = spreadsheet.get_worksheet(1)    # Tab thứ hai: Lịch sử

# Danh sách Admin (ID Telegram)
ADMIN_LIST = [int(id.strip()) for id in os.getenv("ADMIN_IDS").split(",")]

def is_admin(user_id):
    return user_id in ADMIN_LIST

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Kiểm tra tồn kho", callback_data='check_inv')],
        [InlineKeyboardButton("📜 Xem lịch sử", callback_data='view_history')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📦 *Hệ thống Quản lý Kho*\n\n"
        "Hướng dẫn nhanh:\n"
        "➕ Nhập: `/nhap Ten_SP So_Luong Ghi_Chu`\n"
        "➖ Xuất: `/xuat Ten_SP So_Luong`",
        reply_markup=reply_markup, parse_mode='Markdown'
    )

async def add_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Bạn không có quyền!")
        return

    try:
        name = context.args[0]
        qty = int(context.args[1])
        note = " ".join(context.args[2:]) if len(context.args) > 2 else ""
        
        cell = inventory_sheet.find(name)
        if cell:
            new_qty = int(inventory_sheet.cell(cell.row, 2).value) + qty
            inventory_sheet.update_cell(cell.row, 2, new_qty)
        else:
            inventory_sheet.append_row([name, qty])
            new_qty = qty

        history_sheet.append_row([str(datetime.now()), update.effective_user.first_name, "NHẬP", name, qty, note])
        await update.message.reply_text(f"✅ Đã nhập {qty} {name}. Tồn hiện tại: {new_qty}")
    except:
        await update.message.reply_text("❌ Lỗi! Cú pháp: `/nhap Ten 10 Ghi_chu`", parse_mode='Markdown')

async def remove_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return

    try:
        name = context.args[0]
        qty_to_remove = int(context.args[1])
        
        cell = inventory_sheet.find(name)
        if cell:
            current_qty = int(inventory_sheet.cell(cell.row, 2).value)
            if current_qty >= qty_to_remove:
                new_qty = current_qty - qty_to_remove
                inventory_sheet.update_cell(cell.row, 2, new_qty)
                history_sheet.append_row([str(datetime.now()), update.effective_user.first_name, "XUẤT", name, -qty_to_remove, "Xuất hàng"])
                await update.message.reply_text(f"✅ Đã xuất {qty_to_remove} {name}. Còn lại: {new_qty}")
            else:
                await update.message.reply_text(f"⚠️ Không đủ hàng! Hiện có: {current_qty}")
        else:
            await update.message.reply_text("❌ Sản phẩm không tồn tại.")
    except:
        await update.message.reply_text("❌ Lỗi! Cú pháp: `/xuat Ten 10`", parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'check_inv':
        data = inventory_sheet.get_all_records()
        msg = "📊 *TỒN KHO HIỆN TẠI:*\n" + "\n".join([f"- {r['Ten']}: {r['SoLuong']}" for r in data])
        await query.edit_message_text(msg, parse_mode='Markdown')

if __name__ == '__main__':
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nhap", add_inventory))
    app.add_handler(CommandHandler("xuat", remove_inventory))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot is running...")
    app.run_polling()
import os
import json
import re
from datetime import datetime
from dotenv import load_dotenv
import gspread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load biến môi trường
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
# Danh sách Admin ID (ngăn cách bởi dấu phẩy)
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# Kết nối Google Sheets
def get_sheets():
    gc = gspread.service_account_from_dict(GOOGLE_CREDS)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("DATA"), sh.worksheet("DANH_MUC")

# --- HÀM TÌM KIẾM THÔNG MINH ---
def find_product_by_name(search_term, dm_data):
    search_term = search_term.strip().lower()
    matches = []
    for row in dm_data:
        if len(row) < 3: continue
        ma_sp, ten_sp, rate = row[0], row[1], row[2]
        if search_term in ten_sp.lower() or search_term == ma_sp.lower():
            matches.append({"ma": ma_sp, "ten": ten_sp, "rate": int(rate)})
    return matches

# --- LỆNH NHẬP / XUẤT (Dành cho mọi người) ---
async def process_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    args = context.args
    user = update.message.from_user
    full_name = user.full_name if user.full_name else f"@{user.username}"
    
    if len(args) < 3:
        await update.message.reply_text(f"⚠️ Cú pháp: /{mode.lower()} [kho] [tên sp] [sl+t/c]\nVí dụ: /{mode.lower()} KHO1 BIA 10t")
        return

    try:
        kho = args[0].upper()
        sl_raw = args[-1].lower()
        search_term = " ".join(args[1:-1])
        
        ws_data, ws_dm = get_sheets()
        dm_data = ws_dm.get_all_values()[1:]
        
        products = find_product_by_name(search_term, dm_data)
        
        if not products:
            await update.message.reply_text(f"❌ Không thấy SP nào tên '{search_term}'")
            return
        if len(products) > 1:
            goi_y = "\n".join([f"• {p['ten']}" for p in products])
            await update.message.reply_text(f"🧐 Có nhiều loại, hãy nhập rõ hơn:\n{goi_y}")
            return
        
        p = products[0]
        # Quy đổi đơn vị
        if sl_raw.endswith('t'):
            qty = int(sl_raw[:-1]) * p['rate']
            don_vi = f"{sl_raw[:-1]} Thùng"
        elif sl_raw.endswith('c'):
            qty = int(sl_raw[:-1])
            don_vi = f"{sl_raw[:-1]} Chai"
        else:
            await update.message.reply_text("❌ Thiếu đơn vị! Thêm 't' (thùng) hoặc 'c' (chai).")
            return

        final_qty = qty if mode == "NHAP" else -abs(qty)
        row = [datetime.now().strftime("%d/%m/%Y %H:%M:%S"), kho, p['ma'], p['ten'], final_qty, mode, full_name, don_vi, str(user.id)]
        ws_data.append_row(row)
        
        icon = "📥" if mode == "NHAP" else "📤"
        await update.message.reply_text(f"{icon} **{mode} thành công!**\n📦 {p['ten']}\n🔢 Tổng: {abs(final_qty)} chai ({don_vi})", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

async def nhap(u, c): await process_transaction(u, c, "NHAP")
async def xuat(u, c): await process_transaction(u, c, "XUAT")

# --- QUẢN LÝ SẢN PHẨM (Chỉ Admin) ---
async def sanpham(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Quyền Admin mới được dùng lệnh này.")
        return
    
    args = context.args # /sp [tên] [tỷ lệ]
    if len(args) < 2:
        await update.message.reply_text("⚠️ Cú pháp: /sp [tên] [tỷ lệ]\nVí dụ: /sp Bia Saigon 24")
        return

    try:
        ty_le = args[-1]
        ten_sp = " ".join(args[:-1])
        ma_sp = re.sub(r'\s+', '_', ten_sp).upper()

        ws_data, ws_dm = get_sheets()
        dm_data = ws_dm.get_all_values()
        
        idx = next((i+1 for i, r in enumerate(dm_data) if r[1].lower() == ten_sp.lower()), -1)
        
        if idx != -1:
            ws_dm.update_cell(idx, 3, ty_le)
            msg = f"🔄 Cập nhật tỷ lệ: {ten_sp}"
        else:
            ws_dm.append_row([ma_sp, ten_sp, ty_le])
            msg = f"✨ Thêm mới SP: {ten_sp}"

        await update.message.reply_text(f"{msg}\n🔢 1 thùng = {ty_le} chai")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

# --- BÁO CÁO TỒN KHO (Chỉ Admin) ---
async def tonkho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Quyền Admin mới được xem tồn kho.")
        return
    try:
        ws_data, ws_dm = get_sheets()
        data = ws_data.get_all_values()[1:]
        dm = ws_dm.get_all_values()[1:]
        conv = {r[0]: int(r[2]) for r in dm}; name = {r[0]: r[1] for r in dm}
        
        inv = {}
        for r in data:
            k, m, q = r[1], r[2], int(r[4])
            if k not in inv: inv[k] = {}
            inv[k][m] = inv[k].get(m, 0) + q

        search_kho = context.args[0].upper() if context.args else None
        msg = "📊 **TỒN KHO**\n"
        for kho, items in inv.items():
            if search_kho and kho != search_kho: continue
            msg += f"\n🏢 **KHO: {kho}**\n"
            for ma, total in items.items():
                if total == 0: continue
                rate = conv.get(ma, 1)
                t, c = total // rate, total % rate
                res = (f"{t}t " if t > 0 else "") + (f"{c}c" if c > 0 else "")
                msg += f"• `{name.get(ma, ma)}`: {res} ({total} chai)\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

async def danhsach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, ws_dm = get_sheets()
    dm = ws_dm.get_all_values()[1:]
    msg = "📋 **DANH MỤC**\n" + "\n".join([f"• {r[1]} (1t={r[2]}c)" for r in dm])
    await update.message.reply_text(msg, parse_mode="Markdown")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("nhap", nhap))
    app.add_handler(CommandHandler("xuat", xuat))
    app.add_handler(CommandHandler("tonkho", tonkho))
    app.add_handler(CommandHandler("sp", sanpham))
    app.add_handler(CommandHandler("ds", danhsach))
    print("Bot is running...")
    app.run_polling()

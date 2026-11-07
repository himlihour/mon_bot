#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import random
import string
import threading
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

# Ensure UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ====================================================================
# CONFIGURATION (Loaded securely from Environment Variables on Render)
# ====================================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BAKONG_TOKEN = os.environ.get("BAKONG_TOKEN", "").strip()
BAKONG_ACCOUNT_ID = os.environ.get("BAKONG_ACCOUNT_ID", "himlihour@bkrt").strip()
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Him Lihour").strip()
MERCHANT_CITY = os.environ.get("MERCHANT_CITY", "Phnom Penh").strip()

PLAN_DAYS = int(os.environ.get("PLAN_DAYS", "10"))
PRICE_KHR = int(os.environ.get("PRICE_KHR", "1000"))
CURRENCY = os.environ.get("CURRENCY", "KHR").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PAYMENTS_DB_FILE = os.path.join(SCRIPT_DIR, "payments_database.json")
SUBSCRIBERS_FILE = os.path.join(SCRIPT_DIR, "bot_subscribers.json")

# Bakong KHQR SDK
try:
    from bakong_khqr import KHQR
    khqr_client = KHQR(bakong_token=BAKONG_TOKEN)
except Exception as e:
    print(f"[Warning] KHQR init: {e}")
    khqr_client = None

try:
    import qrcode
except Exception as e:
    print(f"[Warning] QRCode init: {e}")
    qrcode = None


# ====================================================================
# SUPABASE REST API
# ====================================================================
def supabase_request(endpoint: str, method: str = "GET", payload: Optional[dict] = None, timeout: int = 15):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else (resp.status, [])
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def generate_license_key_str() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = [''.join(random.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "LHR-" + "-".join(parts)


def load_subscribers() -> Dict[str, dict]:
    subscribers = {}
    if os.path.isfile(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                subscribers = json.load(f)
        except Exception:
            subscribers = {}

    # Seed from Supabase payments if local subscribers empty
    if not subscribers and SUPABASE_KEY:
        try:
            st, data = supabase_request("payments?select=telegram_id,name,username,created_at&limit=100")
            if isinstance(data, list):
                for r in data:
                    cid = r.get("telegram_id")
                    if cid and str(cid) not in subscribers:
                        subscribers[str(cid)] = {
                            "chat_id": cid,
                            "first_name": r.get("name", "User"),
                            "username": r.get("username", ""),
                            "last_active": r.get("created_at", "")
                        }
        except Exception:
            pass

    return subscribers


def record_subscriber(chat_id: int, user_info: dict):
    if not chat_id:
        return
    subscribers = load_subscribers()
    cid_str = str(chat_id)
    first_name = user_info.get("first_name", "")
    username = user_info.get("username", "")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if cid_str not in subscribers:
        subscribers[cid_str] = {
            "chat_id": chat_id,
            "first_name": first_name,
            "username": username,
            "joined_at": now_str,
            "last_active": now_str
        }
    else:
        subscribers[cid_str]["last_active"] = now_str
        if first_name:
            subscribers[cid_str]["first_name"] = first_name
        if username:
            subscribers[cid_str]["username"] = username

    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(subscribers, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_user_history_supabase(chat_id: int) -> List[dict]:
    history = []
    try:
        st, data = supabase_request(f"payments?telegram_id=eq.{chat_id}&order=created_at.desc&limit=10")
        if isinstance(data, list):
            history = data
    except Exception:
        history = []
    return history


def save_payment_record(record: dict):
    # 1. Save to Supabase payments table
    supabase_request("payments", method="POST", payload={
        "telegram_id": record.get("chat_id"),
        "name": record.get("user_name"),
        "username": record.get("username"),
        "amount": record.get("amount"),
        "currency": record.get("currency"),
        "plan": f"{PLAN_DAYS} Days",
        "payment_method": "Bakong KHQR",
        "md5_hash": record.get("md5"),
        "license_key": record.get("license_key")
    })

    # 2. Insert to Supabase licenses table (for Dubber Lite app verification)
    supabase_request("licenses", method="POST", payload={
        "license_key": record.get("license_key"),
        "status": "active",
        "max_devices": 1,
        "expires_at": record.get("expires_at")
    })


# ====================================================================
# TELEGRAM PAYMENT BOT CLASS
# ====================================================================
class DedicatedPaymentBot:
    def __init__(self, token: str):
        self.token = token
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._active_poll_threads: Dict[str, bool] = {}

    def api_post(self, method: str, payload: dict, timeout: int = 15):
        url = f"{self.api_base}/{method}"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def api_get(self, method: str, timeout: int = 25):
        url = f"{self.api_base}/{method}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.api_post("sendMessage", payload)

    def send_photo(self, chat_id: int, photo_bytes: bytes, caption: str, reply_markup: Optional[dict] = None):
        import mimetypes
        boundary = "----WebKitFormBoundary" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
        body = bytearray()

        def add_field(name, value):
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{value}\r\n".encode("utf-8"))

        add_field("chat_id", str(chat_id))
        add_field("caption", caption)
        add_field("parse_mode", "Markdown")
        if reply_markup:
            add_field("reply_markup", json.dumps(reply_markup))

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="photo"; filename="khqr.png"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: image/png\r\n\r\n")
        body.extend(photo_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            f"{self.api_base}/sendPhoto",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[Error sendPhoto]: {e}")
            return None

    def delete_message(self, chat_id: int, message_id: int):
        payload = {"chat_id": chat_id, "message_id": message_id}
        return self.api_post("deleteMessage", payload)

    def answer_callback(self, callback_id: str, text: Optional[str] = None, alert: bool = False):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        self.api_post("answerCallbackQuery", payload)

    # ================================================================
    # CLEAN, MINIMALIST UI (NO PERSONAL NAME OR ACCOUNT SHOWN)
    # ================================================================
    def send_welcome_menu(self, chat_id: int, user_name: str):
        text = (
            f"👋 សួស្តី *{user_name}*!\n\n"
            f"📦 *Dubber Lite License*\n"
            f"⏳ សុពលភាព: *{PLAN_DAYS} ថ្ងៃ*\n"
            f"💰 តម្លៃ: *{PRICE_KHR:,} ៛*"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": f"💳 ទិញ License ({PRICE_KHR:,}៛)", "callback_data": "buy_10d"}],
                [{"text": "📜 ប្រវត្តិទិញ (My Keys)", "callback_data": "my_history"}]
            ]
        }
        self.send_message(chat_id, text, reply_markup)

    def send_order_confirmation(self, chat_id: int, user_info: dict):
        text = (
            "🛒 *បញ្ជាក់ការបញ្ជាទិញ License*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 កញ្ចប់: *Dubber Lite ({PLAN_DAYS} ថ្ងៃ)*\n"
            f"💰 តម្លៃ: *{PRICE_KHR:,} ៛*\n"
            "🏦 វិធីទូទាត់: *Bakong KHQR*\n\n"
            "👉 _តើលោកអ្នកចង់បង្កើត QR Code ដើម្បីបង់ប្រាក់ឥឡូវនេះមែនទេ?_"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": f"✅ បញ្ជាក់បង្កើត QR ({PRICE_KHR:,}៛)", "callback_data": "confirm_buy"}],
                [{"text": "🔙 ត្រឡប់ក្រោយ", "callback_data": "menu"}]
            ]
        }
        self.send_message(chat_id, text, reply_markup)

    def show_my_history(self, chat_id: int):
        history = get_user_history_supabase(chat_id)
        if not history:
            text = "📭 លោកអ្នកមិនទាន់មានប្រវត្តិទិញ License នៅឡើយទេ។"
            reply_markup = {
                "inline_keyboard": [
                    [{"text": f"💳 ទិញ License ({PRICE_KHR:,}៛)", "callback_data": "buy_10d"}]
                ]
            }
            self.send_message(chat_id, text, reply_markup)
            return

        lines = ["📜 *ប្រវត្តិទិញ License របស់អ្នក:*", "━━━━━━━━━━━━━━━━━━"]
        for idx, item in enumerate(history[:10], 1):
            key = item.get("license_key", "N/A")
            date = (item.get("created_at") or "").split("T")[0]
            lines.append(f"{idx}. `{key}`\n   📅 {date} | 10 ថ្ងៃ\n")

        lines.append("_(ចុចលើ Key ដើម្បី Copy យកទៅ Paste ប្រើប្រាស់)_")
        text = "\n".join(lines)
        reply_markup = {
            "inline_keyboard": [
                [{"text": f"💳 ទិញបន្ថែម ({PRICE_KHR:,}៛)", "callback_data": "buy_10d"}],
                [{"text": "🔙 ត្រឡប់ក្រោយ", "callback_data": "menu"}]
            ]
        }
        self.send_message(chat_id, text, reply_markup)

    def handle_create_khqr_order(self, chat_id: int, user_info: dict):
        if not khqr_client:
            self.send_message(chat_id, "❌ Bakong KHQR error. Please try again.")
            return

        order_id = f"DL{int(time.time()) % 1000000:06d}"
        user_name = user_info.get("first_name", "Customer")
        if user_info.get("last_name"):
            user_name += f" {user_info.get('last_name')}"
        username = user_info.get("username", "")

        try:
            res = khqr_client.create_qr(
                amount=float(PRICE_KHR),
                account_id=BAKONG_ACCOUNT_ID,
                merchant_name=MERCHANT_NAME,
                merchant_city=MERCHANT_CITY,
                currency=CURRENCY,
                bill_number=order_id,
                store_label="Dubber Lite"
            )
            qr_string = res.qr if hasattr(res, "qr") else str(res)
            md5_hash = khqr_client.generate_md5(qr_string)

            # Generate pure square QR image in memory
            import io
            buf = io.BytesIO()
            if qrcode:
                qr = qrcode.QRCode(
                    version=None,
                    error_correction=qrcode.constants.ERROR_CORRECT_M,
                    box_size=10,
                    border=2,
                )
                qr.add_data(qr_string)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                img.save(buf, format="PNG")
                photo_bytes = buf.getvalue()
            else:
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(qr_string)}"
                with urllib.request.urlopen(qr_url, timeout=10) as r:
                    photo_bytes = r.read()

        except Exception as e:
            self.send_message(chat_id, f"❌ កំហុសបង្កើត QR: `{e}`")
            return

        order_data = {
            "order_id": order_id,
            "chat_id": chat_id,
            "user_name": user_name,
            "username": username,
            "amount": PRICE_KHR,
            "currency": CURRENCY,
            "md5": md5_hash,
            "created_at": time.time(),
            "status": "PENDING"
        }

        with self._lock:
            self.pending_orders[md5_hash] = order_data

        caption = (
            f"🇰🇭 *ស្កេនទូទាត់ប្រាក់ ({PRICE_KHR:,} ៛)*\n"
            f"📦 Dubber Lite (10 ថ្ងៃ)\n\n"
            f"_ស្កេនរួចចុច 'ពិនិត្យការទូទាត់' ដើម្បីទទួល Key:_"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "🔄 ពិនិត្យការទូទាត់", "callback_data": f"chk:{md5_hash}"}],
                [{"text": "❌ បោះបង់", "callback_data": f"cnc:{md5_hash}"}]
            ]
        }

        resp = self.send_photo(chat_id, photo_bytes, caption, reply_markup)
        if resp and resp.get("ok"):
            msg_id = resp.get("result", {}).get("message_id")
            with self._lock:
                if md5_hash in self.pending_orders:
                    self.pending_orders[md5_hash]["message_id"] = msg_id

        self._start_auto_checker(md5_hash)

    def _start_auto_checker(self, md5_hash: str):
        with self._lock:
            if self._active_poll_threads.get(md5_hash):
                return
            self._active_poll_threads[md5_hash] = True

        def worker():
            start_time = time.time()
            max_wait_seconds = 900
            check_interval = 4

            while time.time() - start_time < max_wait_seconds:
                with self._lock:
                    order = self.pending_orders.get(md5_hash)
                    if not order or order.get("status") != "PENDING":
                        break

                try:
                    status = khqr_client.check_payment(md5_hash)
                    if status == "PAID":
                        self.fulfill_paid_order(md5_hash)
                        break
                except Exception:
                    pass

                time.sleep(check_interval)

            with self._lock:
                self._active_poll_threads.pop(md5_hash, None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def fulfill_paid_order(self, md5_hash: str) -> bool:
        with self._lock:
            order = self.pending_orders.get(md5_hash)
            if not order:
                return False
            if order.get("status") == "PAID":
                return True
            order["status"] = "PAID"

        chat_id = order["chat_id"]
        user_name = order["user_name"]
        username = order["username"]

        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(days=PLAN_DAYS)
        exp_iso = exp_dt.isoformat()
        exp_display = exp_dt.strftime("%Y-%m-%d %H:%M UTC")

        cloud_key = generate_license_key_str()

        record = {
            "order_id": order["order_id"],
            "chat_id": chat_id,
            "user_name": user_name,
            "username": username,
            "amount": PRICE_KHR,
            "currency": CURRENCY,
            "license_key": cloud_key,
            "expires_at": exp_iso,
            "md5": md5_hash,
            "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_payment_record(record)

        delivery_text = (
            "🎉 *ទូទាត់ជោគជ័យ!*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔑 *License Key (10 ថ្ងៃ):*\n"
            f"`{cloud_key}`\n\n"
            f"⏳ សុពលភាព: *{PLAN_DAYS} ថ្ងៃ*\n"
            f"📅 ផុតកំណត់: `{exp_display}`\n\n"
            "👉 _ចុចលើ Key ដើម្បី Copy រួច Paste ចូលកម្មវិធី Dubber Lite!_"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "📜 ប្រវត្តិទិញ (My Keys)", "callback_data": "my_history"}],
                [{"text": "💳 ទិញបន្ថែម", "callback_data": "buy_10d"}]
            ]
        }
        self.send_message(chat_id, delivery_text, reply_markup)

        # Delete QR message to keep chat clean
        msg_id = order.get("message_id")
        if msg_id:
            try:
                self.delete_message(chat_id, msg_id)
            except Exception:
                pass

        # Broadcast purchase notification to all other bot users
        self.broadcast_purchase_notification(buyer_chat_id=chat_id)

        return True

    def broadcast_purchase_notification(self, buyer_chat_id: int):
        def _runner():
            time.sleep(1.5)
            subs = load_subscribers()
            text = (
                "🎉 *ដំណឹងការទិញថ្មី (New Purchase)!* 🚀\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "⚡ មានអតិថិជនទើបតែបានទិញ License (**10 ថ្ងៃ**) ជោគជ័យ!\n\n"
                f"💰 តម្លៃត្រឹមតែ: *{PRICE_KHR:,} ៛*\n"
                "👉 លោកអ្នកក៏អាចទិញប្រើប្រាស់បានភ្លាមៗដោយចុចប៊ូតុងខាងក្រោម:"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"💳 ទិញ License ({PRICE_KHR:,}៛)", "callback_data": "buy_10d"}]
                ]
            }

            count = 0
            for cid_str, info in subs.items():
                try:
                    cid = int(cid_str)
                    if cid == buyer_chat_id:
                        continue
                    res = self.send_message(cid, text, markup)
                    if res and res.get("ok"):
                        count += 1
                    time.sleep(0.06)
                except Exception as e:
                    print(f"[Broadcast Error]: {e}")

            print(f"[Broadcast] Alerted {count} user(s).")

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    def handle_callback_query(self, callback_query: dict):
        cb_id = callback_query.get("id")
        cb_data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        from_user = callback_query.get("from", {})

        if not chat_id:
            return

        record_subscriber(chat_id, from_user)

        if cb_data == "buy_10d":
            self.answer_callback(cb_id)
            self.send_order_confirmation(chat_id, from_user)

        elif cb_data == "confirm_buy":
            self.answer_callback(cb_id, "កំពុងបង្កើត QR Code...")
            self.handle_create_khqr_order(chat_id, from_user)

        elif cb_data == "my_history":
            self.answer_callback(cb_id)
            self.show_my_history(chat_id)

        elif cb_data == "menu":
            self.answer_callback(cb_id)
            self.send_welcome_menu(chat_id, from_user.get("first_name", "User"))

        elif cb_data.startswith("chk:"):
            md5 = cb_data.split(":", 1)[1]
            try:
                st = khqr_client.check_payment(md5) if khqr_client else "UNPAID"
                if st == "PAID":
                    self.answer_callback(cb_id, "✅ ទូទាត់ជោគជ័យ! កំពុងចេញ Key...")
                    self.fulfill_paid_order(md5)
                else:
                    self.answer_callback(
                        cb_id, 
                        "⏳ មិនទាន់ឃើញការផ្ទេរប្រាក់ទេ។ សូមស្កេនបង់រួចចុចម្តងទៀត!",
                        alert=True
                    )
            except Exception as e:
                self.answer_callback(cb_id, f"Error: {e}", alert=True)

        elif cb_data.startswith("cnc:"):
            md5 = cb_data.split(":", 1)[1]
            with self._lock:
                self.pending_orders.pop(md5, None)

            # Delete the QR code photo message so it disappears cleanly
            msg_id = message.get("message_id")
            if msg_id:
                try:
                    self.delete_message(chat_id, msg_id)
                except Exception:
                    pass

            self.answer_callback(cb_id, "ការបញ្ជាទិញត្រូវបានបោះបង់")

            cancel_text = (
                "❌ *ការបញ្ជាទិញត្រូវបានបោះបង់។*\n\n"
                "👉 លោកអ្នកអាចជ្រើសរើសទិញម្តងទៀតបានគ្រប់ពេល:"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"💳 ទិញ License ({PRICE_KHR:,}៛)", "callback_data": "buy_10d"}],
                    [{"text": "📜 ប្រវត្តិទិញ (My Keys)", "callback_data": "my_history"}]
                ]
            }
            self.send_message(chat_id, cancel_text, markup)

    def handle_message(self, message: dict):
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        from_user = message.get("from", {})
        user_name = from_user.get("first_name", "User")

        if not chat_id:
            return

        record_subscriber(chat_id, from_user)

        if text in ["/history", "/keys", "history"]:
            self.show_my_history(chat_id)
        elif text in ["/buy", "/pay", "ទិញ"]:
            self.handle_create_khqr_order(chat_id, from_user)
        else:
            self.send_welcome_menu(chat_id, user_name)

    def poll(self):
        print("=" * 60, flush=True)
        print("🤖 Dedicated KHQR Payment Bot is STARTING...", flush=True)
        print(f"• Plan: {PLAN_DAYS} Days @ {PRICE_KHR:,} KHR", flush=True)
        print("=" * 60, flush=True)

        try:
            self.api_get("deleteWebhook?drop_pending_updates=False")
        except Exception:
            pass

        print("👂 Listening for Telegram messages (24/7 Cloud)...", flush=True)

        while True:
            try:
                res = self.api_get(f"getUpdates?offset={self.offset}&timeout=20", timeout=30)
                if res and res.get("ok"):
                    for update in res.get("result", []):
                        self.offset = update["update_id"] + 1

                        if "callback_query" in update:
                            self.handle_callback_query(update["callback_query"])
                            continue

                        msg = update.get("message") or update.get("edited_message")
                        if msg:
                            self.handle_message(msg)
                elif res and not res.get("ok"):
                    desc = res.get("description", "")
                    if "webhook" in desc.lower():
                        self.api_get("deleteWebhook")
                    time.sleep(3)

            except Exception as e:
                time.sleep(3)


# ====================================================================
# HEALTH CHECK WEB SERVER (FOR RENDER WEB SERVICE 24/7)
# ====================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"Dubber Lite Payment Bot 24/7"}')

    def log_message(self, format, *args):
        pass  # Quiet logs


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌐 Health Check server listening on port {port} (Render ready!)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    # Start health check server in background thread for Render
    threading.Thread(target=start_health_server, daemon=True).start()

    # Start Telegram Bot polling in main thread
    bot = DedicatedPaymentBot(BOT_TOKEN)
    bot.poll()


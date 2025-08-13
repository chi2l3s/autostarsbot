import asyncio
import os
import threading
import time
import queue
from dataclasses import dataclass
from typing import Optional, Tuple, List

import tkinter as tk
from tkinter import ttk, messagebox

from telethon import TelegramClient, functions, types
from telethon.errors.rpcerrorlist import AuthKeyUnregisteredError
from telethon.tl.types import InputInvoiceStarGift, InputPeerSelf
from dotenv import load_dotenv

# ---------------------------
# Core logic
# ---------------------------

load_dotenv()

API_ID = int(os.getenv("TG_API_ID") or 0)
API_HASH = os.getenv("TG_API_HASH") or ""
DEFAULT_SESSION = os.getenv("TG_SESSION", "tg_gifts.session")


def stars_value(v):
    if hasattr(v, "amount"):
        return (v.amount or 0) + (getattr(v, "nanos", 0) or 0) / 1_000_000_000
    return int(v or 0)


@dataclass
class RunConfig:
    session: str
    recipient: str
    max_price_stars: int
    poll_interval: int


class GiftBuyer:
    def __init__(self, cfg: RunConfig, log_fn):
        self.cfg = cfg
        self.log = log_fn
        self._stop_event = asyncio.Event()
        self._client: Optional[TelegramClient] = None

    def stop(self):
        # Can be called from non-async thread via call_soon_threadsafe
        if self._client and self._client.loop.is_running():
            self._client.loop.call_soon_threadsafe(self._stop_event.set)

    async def run(self):
        if not API_ID or not API_HASH:
            raise RuntimeError("TG_API_ID/TG_API_HASH не заданы. Укажите их в .env")

        async with TelegramClient(self.cfg.session, API_ID, API_HASH) as client:
            self._client = client

            try:
                me = InputPeerSelf()
                status = await client(functions.payments.GetStarsStatusRequest(peer=me))
                balance = stars_value(getattr(status, "balance", 0))
                self.log(f"Баланс: {balance:.0f} ⭐")
            except AuthKeyUnregisteredError:
                self.log("Требуется авторизация — сейчас откроется окно входа в Telegram в консоли...")
                # Reopen to force login flow in the same session
                async with TelegramClient(self.cfg.session, API_ID, API_HASH) as _:
                    pass
                self.log("Авторизация завершена. Перезапустите покупатель.")
                return

            last_hash = 0
            to_peer = await client.get_input_entity(self.cfg.recipient)
            self.log(f"Получатель: {self.cfg.recipient}")

            while not self._stop_event.is_set():
                try:
                    gifts_resp = await client(functions.payments.GetStarGiftsRequest(hash=last_hash))
                except Exception as e:
                    self.log(f"Ошибка при получении списка подарков: {e}")
                    await asyncio.sleep(self.cfg.poll_interval)
                    continue

                if isinstance(gifts_resp, types.payments.StarGiftsNotModified):
                    await asyncio.sleep(self.cfg.poll_interval)
                    continue

                last_hash = getattr(gifts_resp, "hash", 0) or 0
                gifts = getattr(gifts_resp, "gifts", [])
                if not gifts:
                    await asyncio.sleep(self.cfg.poll_interval)
                    continue

                candidates: List[Tuple[float, types.StarGift]] = []
                for g in gifts:
                    limited = getattr(g, "limited", False)
                    sold_out = getattr(g, "sold_out", False)
                    remains = getattr(g, "availability_remains", None)
                    price = stars_value(getattr(g, "stars", 0))
                    if limited and not sold_out and (remains is None or remains > 0) and price <= self.cfg.max_price_stars:
                        candidates.append((price, g))

                candidates.sort(key=lambda x: x[0])

                for price, gift in candidates:
                    if self._stop_event.is_set():
                        break

                    # refresh balance before buy
                    status = await client(functions.payments.GetStarsStatusRequest(peer=InputPeerSelf()))
                    balance = stars_value(getattr(status, "balance", 0))

                    if balance < price:
                        self.log(f"Пропуск — не хватает Stars ({balance} < {price})")
                        continue

                    invoice = InputInvoiceStarGift(
                        peer=to_peer,
                        gift_id=gift.id,
                    )

                    try:
                        form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
                    except Exception as e:
                        self.log(f"Ошибка формы оплаты: {e}")
                        continue

                    if isinstance(form, (types.payments.PaymentFormStarGift, types.payments.PaymentFormStars)):
                        try:
                            result = await client(functions.payments.SendStarsFormRequest(
                                form_id=form.form_id,
                                invoice=invoice,
                            ))
                            self.log(f"✅ Куплено: {gift.id} за {price} ⭐")
                            return  # остановимся после первой покупки
                        except Exception as e:
                            self.log(f"Ошибка при оплате: {e}")
                    else:
                        self.log(f"Неожиданный тип формы оплаты: {type(form)}")

                await asyncio.sleep(self.cfg.poll_interval)


# ---------------------------
# Tkinter GUI
# ---------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telegram Stars Gifts — Покупатель (Telethon)")
        self.geometry("760x560")
        self.minsize(720, 520)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.current_runner: Optional[GiftBuyer] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Controls
        self._build_form()
        self._build_log()
        self._after_poll()

    def _build_form(self):
        frm = ttk.Frame(self)
        frm.pack(fill=tk.X, padx=12, pady=12)

        # Session
        ttk.Label(frm, text="Session файл:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)
        self.session_var = tk.StringVar(value=DEFAULT_SESSION)
        ttk.Entry(frm, textvariable=self.session_var, width=40).grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)

        # Recipient
        ttk.Label(frm, text="Получатель (@username / ID / me):").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        self.recipient_var = tk.StringVar(value=os.getenv("RECIPIENT", "me"))
        ttk.Entry(frm, textvariable=self.recipient_var, width=40).grid(row=1, column=1, sticky=tk.W, padx=4, pady=4)

        # Max price
        ttk.Label(frm, text="Макс. цена (⭐):").grid(row=2, column=0, sticky=tk.W, padx=4, pady=4)
        self.max_price_var = tk.IntVar(value=int(os.getenv("MAX_PRICE_STARS", "500")))
        ttk.Spinbox(frm, from_=1, to=10_000, textvariable=self.max_price_var, width=10).grid(row=2, column=1, sticky=tk.W, padx=4, pady=4)

        # Poll interval
        ttk.Label(frm, text="Интервал опроса (сек):").grid(row=3, column=0, sticky=tk.W, padx=4, pady=4)
        self.poll_var = tk.IntVar(value=int(os.getenv("POLL_INTERVAL", "15")))
        ttk.Spinbox(frm, from_=2, to=3600, textvariable=self.poll_var, width=10).grid(row=3, column=1, sticky=tk.W, padx=4, pady=4)

        # API status
        api_frame = ttk.Frame(frm)
        api_frame.grid(row=0, column=2, rowspan=4, padx=24, sticky=tk.NW)
        ttk.Label(api_frame, text="API статус:").pack(anchor=tk.W)
        self.api_lbl = ttk.Label(api_frame, text=self._api_status_text(), foreground=("green" if API_ID and API_HASH else "red"))
        self.api_lbl.pack(anchor=tk.W)

        # Buttons
        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=12)
        self.start_btn = ttk.Button(btns, text="Старт", command=self.on_start)
        self.start_btn.pack(side=tk.LEFT, padx=4, pady=4)
        self.stop_btn = ttk.Button(btns, text="Стоп", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4, pady=4)
        self.balance_btn = ttk.Button(btns, text="Проверить баланс", command=self.on_check_balance)
        self.balance_btn.pack(side=tk.LEFT, padx=4, pady=4)

    def _build_log(self):
        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        ttk.Label(frm, text="Логи:").pack(anchor=tk.W)
        self.text = tk.Text(frm, height=20, wrap=tk.WORD, state=tk.DISABLED)
        self.text.pack(fill=tk.BOTH, expand=True)

    def _after_poll(self):
        # Periodically drain log queue to Text widget
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._after_poll)

    def _append_log(self, line: str):
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, time.strftime("[%H:%M:%S] ") + line + "\n")
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _api_status_text(self):
        if API_ID and API_HASH:
            return f"TG_API_ID={API_ID}, TG_API_HASH=*** ок"
        return "TG_API_ID / TG_API_HASH не заданы (.env)"

    def log(self, line: str):
        self.log_queue.put(line)

    def on_start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Уже запущено", "Фоновой процесс уже работает.")
            return

        cfg = RunConfig(
            session=self.session_var.get().strip() or DEFAULT_SESSION,
            recipient=self.recipient_var.get().strip() or "me",
            max_price_stars=int(self.max_price_var.get()),
            poll_interval=int(self.poll_var.get()),
        )

        self.current_runner = GiftBuyer(cfg, self.log)
        self.loop = asyncio.new_event_loop()

        def worker():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.current_runner.run())
            finally:
                try:
                    pending = asyncio.all_tasks(loop=self.loop)
                    for t in pending:
                        t.cancel()
                except Exception:
                    pass
                self.loop.stop()
                self.loop.close()
                self.log("Фоновая задача завершена.")
                self.start_btn.configure(state=tk.NORMAL)
                self.stop_btn.configure(state=tk.DISABLED)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.log("Старт покупателя подарков…")

    def on_stop(self):
        if self.current_runner and self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.current_runner.stop)
            self.log("Остановка запрошена…")
        else:
            self.log("Нет активной задачи.")

    def on_check_balance(self):
        async def _check():
            try:
                async with TelegramClient(self.session_var.get().strip() or DEFAULT_SESSION, API_ID, API_HASH) as client:
                    me = InputPeerSelf()
                    status = await client(functions.payments.GetStarsStatusRequest(peer=me))
                    balance = stars_value(getattr(status, "balance", 0))
                    self.log(f"Баланс: {balance:.0f} ⭐")
            except Exception as e:
                self.log(f"Ошибка получения баланса: {e}")

        # Run one-off async task in a temp loop to avoid clashing with runner loop
        threading.Thread(target=lambda: asyncio.run(_check()), daemon=True).start()


if __name__ == "__main__":
    app = App()
    app.mainloop()

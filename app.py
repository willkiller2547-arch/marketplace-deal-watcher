from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import requests

import watcher


class SearchDialog(tk.Toplevel):
    def __init__(self, parent, search=None):
        super().__init__(parent)
        self.title("Marketplace Search")
        self.resizable(True, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        search = search or {}

        self.vars = {
            "name": tk.StringVar(value=search.get("name", "")),
            "url": tk.StringVar(value=search.get("url", "")),
            "required": tk.StringVar(value=", ".join(search.get("required_keywords", []))),
            "preferred": tk.StringVar(value=", ".join(search.get("preferred_keywords", []))),
            "excluded": tk.StringVar(value=", ".join(search.get("excluded_keywords", []))),
            "max_price": tk.StringVar(value=self._show_number(search.get("max_price"))),
            "estimated_value": tk.StringVar(value=self._show_number(search.get("estimated_value"))),
            "min_discount": tk.StringVar(value=str(search.get("min_discount_percent", 20))),
            "min_score": tk.StringVar(value=str(search.get("min_score", 55))),
            "auto_value": tk.BooleanVar(value=bool(search.get("auto_value", True))),
        }

        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        row = 0
        row = self.add_entry(frame, row, "Search name", "name")
        row = self.add_entry(frame, row, "Marketplace search URL", "url", width=72)
        row = self.add_entry(frame, row, "Required keywords (comma separated)", "required")
        row = self.add_entry(frame, row, "Preferred keywords", "preferred")
        row = self.add_entry(frame, row, "Excluded keywords", "excluded")
        row = self.add_entry(frame, row, "Maximum price (optional)", "max_price")
        row = self.add_entry(frame, row, "Manual estimated value (optional)", "estimated_value")

        ttk.Checkbutton(
            frame,
            text="Automatically estimate normal price from visible listings",
            variable=self.vars["auto_value"],
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 8))
        row += 1

        row = self.add_entry(frame, row, "Minimum discount %", "min_discount")
        row = self.add_entry(frame, row, "Minimum deal score (0-100)", "min_score")

        hint = (
            "Tip: make each Marketplace search fairly specific. "
            "Automatic pricing works best when the results are similar products."
        )
        ttk.Label(frame, text=hint, wraplength=650).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 10)
        )
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Save Search", command=self.save).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_visibility()
        self.focus_force()

    @staticmethod
    def _show_number(value):
        return "" if value in (None, "") else str(value)

    def add_entry(self, frame, row, label, key, width=50):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Entry(frame, textvariable=self.vars[key], width=width).grid(
            row=row, column=1, sticky="ew", pady=5
        )
        return row + 1

    @staticmethod
    def keyword_list(value):
        return [part.strip() for part in value.split(",") if part.strip()]

    @staticmethod
    def optional_float(value):
        value = value.strip()
        if not value:
            return None
        return float(value)

    def save(self):
        try:
            name = self.vars["name"].get().strip()
            url = self.vars["url"].get().strip()

            if not name:
                raise ValueError("Give the search a name.")
            if "facebook.com/marketplace" not in url.lower():
                raise ValueError("Paste a Facebook Marketplace search URL.")

            min_discount = max(0, float(self.vars["min_discount"].get().strip() or "0"))
            min_score = int(self.vars["min_score"].get().strip() or "55")
            if not 0 <= min_score <= 100:
                raise ValueError("Minimum deal score must be between 0 and 100.")

            self.result = {
                "name": name,
                "url": url,
                "required_keywords": self.keyword_list(self.vars["required"].get()),
                "preferred_keywords": self.keyword_list(self.vars["preferred"].get()),
                "excluded_keywords": self.keyword_list(self.vars["excluded"].get()),
                "max_price": self.optional_float(self.vars["max_price"].get()),
                "estimated_value": self.optional_float(self.vars["estimated_value"].get()),
                "auto_value": bool(self.vars["auto_value"].get()),
                "min_baseline_samples": 7,
                "min_discount_percent": min_discount,
                "min_score": min_score,
            }
        except ValueError as exc:
            messagebox.showerror("Invalid search", str(exc), parent=self)
            return

        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Marketplace Deal Watcher v2")
        self.geometry("980x720")
        self.minsize(860, 620)

        self.cfg = watcher.load_config()
        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.stop_event = threading.Event()

        self.webhook_var = tk.StringVar(value=self.cfg.get("discord_webhook_url", ""))
        self.interval_var = tk.StringVar(value=str(self.cfg.get("check_interval_minutes", 30)))
        self.headless_var = tk.BooleanVar(value=bool(self.cfg.get("headless", False)))
        self.first_scan_var = tk.BooleanVar(value=bool(self.cfg.get("alert_on_first_scan", True)))
        self.price_drop_var = tk.BooleanVar(value=bool(self.cfg.get("alert_on_price_drop", True)))

        self.build_ui()
        self.refresh_searches()
        self.after(150, self.drain_logs)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(
            title_row,
            text="Marketplace Deal Watcher v2",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        ttk.Button(title_row, text="Facebook Login", command=self.facebook_login).pack(side="right")

        settings = ttk.LabelFrame(outer, text="Notifications & Watcher", padding=10)
        settings.pack(fill="x", pady=(12, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Discord webhook").grid(row=0, column=0, sticky="w", padx=(0, 8))
        webhook_entry = ttk.Entry(settings, textvariable=self.webhook_var, show="•")
        webhook_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(settings, text="Test Discord", command=self.test_discord).grid(
            row=0, column=2, padx=(8, 0)
        )

        ttk.Label(settings, text="Check every").grid(row=1, column=0, sticky="w", pady=(10, 0))
        interval_frame = ttk.Frame(settings)
        interval_frame.grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Spinbox(
            interval_frame,
            from_=15,
            to=1440,
            width=7,
            textvariable=self.interval_var,
        ).pack(side="left")
        ttk.Label(interval_frame, text=" minutes (minimum 15)").pack(side="left", padx=(6, 0))

        options = ttk.Frame(settings)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            options, text="Run browser hidden", variable=self.headless_var
        ).pack(side="left")
        ttk.Checkbutton(
            options, text="Alert on first scan", variable=self.first_scan_var
        ).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(
            options, text="Alert when a listing price drops", variable=self.price_drop_var
        ).pack(side="left", padx=(16, 0))

        searches_frame = ttk.LabelFrame(outer, text="Marketplace Searches", padding=10)
        searches_frame.pack(fill="both", expand=False, pady=8)

        columns = ("name", "max", "value", "discount", "score")
        self.tree = ttk.Treeview(searches_frame, columns=columns, show="headings", height=8)
        self.tree.heading("name", text="Name")
        self.tree.heading("max", text="Max Price")
        self.tree.heading("value", text="Price Baseline")
        self.tree.heading("discount", text="Min Discount")
        self.tree.heading("score", text="Min Score")
        self.tree.column("name", width=220)
        self.tree.column("max", width=100, anchor="center")
        self.tree.column("value", width=170, anchor="center")
        self.tree.column("discount", width=110, anchor="center")
        self.tree.column("score", width=90, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(searches_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        search_buttons = ttk.Frame(searches_frame)
        search_buttons.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(search_buttons, text="Add Search", width=14, command=self.add_search).pack(pady=(0, 6))
        ttk.Button(search_buttons, text="Edit Search", width=14, command=self.edit_search).pack(pady=6)
        ttk.Button(search_buttons, text="Remove", width=14, command=self.remove_search).pack(pady=6)

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(4, 8))

        self.save_btn = ttk.Button(controls, text="Save Settings", command=self.save_settings)
        self.save_btn.pack(side="left")

        self.once_btn = ttk.Button(controls, text="Run Once", command=lambda: self.start_watcher(True))
        self.once_btn.pack(side="left", padx=(8, 0))

        self.start_btn = ttk.Button(controls, text="Start Watcher", command=lambda: self.start_watcher(False))
        self.start_btn.pack(side="left", padx=(8, 0))

        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop_watcher, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        ttk.Button(controls, text="Clear Log", command=self.clear_log).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="Live Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log("Ready. Add a search, test Discord, then click Facebook Login.")

    def log(self, message):
        self.log_queue.put(str(message))

    def drain_logs(self):
        changed = False
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                changed = True
        except queue.Empty:
            pass

        if self.worker_thread and not self.worker_thread.is_alive():
            self.worker_thread = None
            self.set_running(False)

        self.after(150, self.drain_logs)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def refresh_searches(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for idx, search in enumerate(self.cfg.get("searches", [])):
            max_price = search.get("max_price")
            max_text = "Any" if max_price in (None, "") else f"${float(max_price):,.0f}"

            manual = search.get("estimated_value")
            if manual not in (None, ""):
                value_text = f"${float(manual):,.0f} manual"
            elif search.get("auto_value", True):
                value_text = "Automatic"
            else:
                value_text = "None"

            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    search.get("name", ""),
                    max_text,
                    value_text,
                    f"{float(search.get('min_discount_percent', 0)):.0f}%",
                    search.get("min_score", 55),
                ),
            )

    def selected_index(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def add_search(self):
        dialog = SearchDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.cfg.setdefault("searches", []).append(dialog.result)
            self.refresh_searches()
            self.save_settings(silent=True)

    def edit_search(self):
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo("Edit Search", "Select a search first.")
            return

        dialog = SearchDialog(self, self.cfg["searches"][idx])
        self.wait_window(dialog)
        if dialog.result:
            self.cfg["searches"][idx] = dialog.result
            self.refresh_searches()
            self.save_settings(silent=True)

    def remove_search(self):
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo("Remove Search", "Select a search first.")
            return

        name = self.cfg["searches"][idx].get("name", "this search")
        if messagebox.askyesno("Remove Search", f"Remove '{name}'?"):
            del self.cfg["searches"][idx]
            self.refresh_searches()
            self.save_settings(silent=True)

    def collect_settings(self):
        try:
            interval = max(15, int(self.interval_var.get().strip()))
        except ValueError:
            raise ValueError("Check interval must be a number.")

        self.cfg["discord_webhook_url"] = self.webhook_var.get().strip()
        self.cfg["check_interval_minutes"] = interval
        self.cfg["headless"] = bool(self.headless_var.get())
        self.cfg["alert_on_first_scan"] = bool(self.first_scan_var.get())
        self.cfg["alert_on_price_drop"] = bool(self.price_drop_var.get())
        self.cfg.setdefault("max_alerts_per_scan", 5)
        self.cfg.setdefault("scroll_count", 4)
        self.cfg.setdefault("price_drop_percent", 10)

    def save_settings(self, silent=False):
        try:
            self.collect_settings()
            watcher.save_config(self.cfg)
            if not silent:
                self.log("Settings saved.")
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc))

    def test_discord(self):
        webhook = self.webhook_var.get().strip()
        if not webhook:
            messagebox.showerror("Discord", "Paste your Discord webhook first.")
            return

        self.log("Testing Discord webhook...")

        def worker():
            try:
                watcher.test_discord(webhook)
                self.log("Discord test sent successfully ✅")
            except requests.RequestException as exc:
                self.log(f"Discord test failed: {exc}")
            except Exception as exc:
                self.log(f"Discord test failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def facebook_login(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Facebook Login", "Stop the watcher before opening the login browser.")
            return

        self.log("Starting Facebook login browser...")

        def worker():
            try:
                watcher.facebook_login(self.log)
            except Exception as exc:
                self.log(f"Facebook login error: {type(exc).__name__}: {exc}")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.once_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def start_watcher(self, once):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Watcher", "Something is already running.")
            return

        try:
            self.collect_settings()
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc))
            return

        if not self.cfg.get("searches"):
            messagebox.showerror("Watcher", "Add at least one Marketplace search.")
            return

        watcher.save_config(self.cfg)
        self.stop_event = threading.Event()
        self.set_running(True)
        self.log("Starting one scan..." if once else "Watcher started.")

        cfg_copy = dict(self.cfg)
        cfg_copy["searches"] = [dict(s) for s in self.cfg["searches"]]

        def worker():
            watcher.run_watcher(
                cfg_copy,
                log=self.log,
                stop_event=self.stop_event,
                once=once,
            )
            self.log("Run finished." if once else "Watcher stopped.")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop_watcher(self):
        self.log("Stopping watcher...")
        self.stop_event.set()

    def on_close(self):
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

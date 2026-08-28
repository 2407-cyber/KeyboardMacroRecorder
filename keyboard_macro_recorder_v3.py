# -*- coding: utf-8 -*-
"""
键盘快捷键录制器 v3 - 真实按下/松开时间版
Windows 10/11

核心：
- 全局低级键盘钩子
- 记录每次 KEYDOWN / KEYUP 的相对时间
- 回放时尽量复现真实按键持续时间和事件间隔
- 支持同时按住多个键（组合键）
- 支持手动修改事件等待时间
- 支持播放/停止全局热键
- 保存/加载 JSON
"""

import ctypes
from ctypes import wintypes
import json
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
HC_ACTION = 0
LLKHF_INJECTED = 0x00000010

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

HOTKEY_PLAY = 6001
HOTKEY_STOP = 6002

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("ki", KEYBDINPUT),
    ]

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

VK_NAMES = {
    8:"Backspace", 9:"Tab", 13:"Enter", 16:"Shift", 17:"Ctrl", 18:"Alt",
    19:"Pause", 20:"CapsLock", 27:"Esc", 32:"Space", 33:"PageUp",
    34:"PageDown", 35:"End", 36:"Home", 37:"Left", 38:"Up", 39:"Right",
    40:"Down", 45:"Insert", 46:"Delete", 91:"Win", 92:"Win", 93:"Menu",
    144:"NumLock", 145:"ScrollLock",
    160:"Left Shift", 161:"Right Shift", 162:"Left Ctrl", 163:"Right Ctrl",
    164:"Left Alt", 165:"Right Alt",
    186:";", 187:"=", 188:",", 189:"-", 190:".", 191:"/",
    192:"`", 219:"[", 220:"\\", 221:"]", 222:"'",
}
for i in range(1, 13):
    VK_NAMES[0x6F + i] = f"F{i}"
for i in range(10):
    VK_NAMES[0x30 + i] = str(i)
    VK_NAMES[0x60 + i] = f"Num{i}"
for i in range(26):
    VK_NAMES[0x41 + i] = chr(65+i)

NAME_TO_VK = {v.upper(): k for k, v in VK_NAMES.items()}
NAME_TO_VK.update({"CONTROL":17, "CTRL":17, "SHIFT":16, "ALT":18,
                   "WIN":91, "WINDOWS":91, "RETURN":13, "SPACEBAR":32,
                   "ESC":27})

def key_name(vk):
    return VK_NAMES.get(vk, f"VK_{vk}")

def send_key(vk, down):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def parse_hotkey(text):
    parts = [x.strip() for x in text.split("+") if x.strip()]
    mods, vk = 0, None
    for p in parts:
        u = p.upper()
        if u in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif u == "SHIFT":
            mods |= MOD_SHIFT
        elif u == "ALT":
            mods |= MOD_ALT
        elif u in ("WIN", "WINDOWS"):
            mods |= MOD_WIN
        else:
            if vk is not None:
                raise ValueError("一个全局热键只能有一个主按键。")
            if u not in NAME_TO_VK:
                raise ValueError(f"无法识别按键：{p}")
            vk = NAME_TO_VK[u]
    if vk is None:
        raise ValueError("请设置一个主按键，例如 F8 或 Ctrl+F8。")
    return mods | MOD_NOREPEAT, vk

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("键盘快捷键录制器 v3")
        self.root.geometry("930x650")
        self.root.minsize(800, 570)

        # 事件：{"vk": int, "scan": int, "name": str, "action": "按下"/"松开",
        #        "time_ms": float, "wait_ms": float}
        self.events = []
        self.recording = False
        self.playing = False
        self.stop_event = threading.Event()

        self.hook_thread = None
        self.hook_proc = None
        self.hook_handle = None
        self.hotkey_thread = None

        self.last_event_time = None
        self.down_keys = set()

        self.default_wait = tk.IntVar(value=50)
        self.play_count = tk.StringVar(value="1")
        self.play_hotkey = tk.StringVar(value="F8")
        self.stop_hotkey = tk.StringVar(value="F9")
        self.status = tk.StringVar(value="就绪")

        self._build_ui()
        self._start_hotkey_thread()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        ttk.Label(
            self.root, text="键盘快捷键录制器 v3",
            font=("Microsoft YaHei UI", 17, "bold")
        ).pack(anchor="w", padx=14, pady=(10, 2))

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=14, pady=(0, 8))
        ttk.Label(bar, textvariable=self.status).pack(side="left")

        box = ttk.LabelFrame(self.root, text="真实键盘事件")
        box.pack(fill="both", expand=True, padx=14, pady=5)

        cols = ("no", "action", "key", "wait", "time")
        self.tree = ttk.Treeview(box, columns=cols, show="headings")
        self.tree.heading("no", text="序号")
        self.tree.heading("action", text="动作")
        self.tree.heading("key", text="按键")
        self.tree.heading("wait", text="与上一事件间隔(ms)")
        self.tree.heading("time", text="累计时间(ms)")
        self.tree.column("no", width=60, anchor="center")
        self.tree.column("action", width=100, anchor="center")
        self.tree.column("key", width=250, anchor="center")
        self.tree.column("wait", width=180, anchor="center")
        self.tree.column("time", width=180, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        controls = ttk.Frame(self.root)
        controls.pack(fill="x", padx=14, pady=7)
        self.rec_btn = ttk.Button(controls, text="● 开始录制", command=self.toggle_record)
        self.rec_btn.pack(side="left")
        ttk.Button(controls, text="清空", command=self.clear).pack(side="left", padx=7)
        self.play_btn = ttk.Button(controls, text="▶ 播放", command=self.start_play)
        self.play_btn.pack(side="left", padx=7)
        ttk.Button(controls, text="■ 停止", command=self.stop_play).pack(side="left", padx=7)
        ttk.Label(controls, text="默认等待(ms)：").pack(side="left", padx=(20, 5))
        ttk.Entry(controls, textvariable=self.default_wait, width=8).pack(side="left")
        ttk.Button(controls, text="应用到全部", command=self.apply_wait).pack(side="left", padx=6)
        ttk.Button(controls, text="修改选中", command=self.edit_wait).pack(side="left", padx=6)

        settings = ttk.LabelFrame(self.root, text="全局播放控制")
        settings.pack(fill="x", padx=14, pady=5)
        ttk.Label(settings, text="播放热键：").grid(row=0, column=0, padx=8, pady=7)
        ttk.Entry(settings, textvariable=self.play_hotkey, width=14).grid(row=0, column=1)
        ttk.Label(settings, text="停止热键：").grid(row=0, column=2, padx=8)
        ttk.Entry(settings, textvariable=self.stop_hotkey, width=14).grid(row=0, column=3)
        ttk.Button(settings, text="应用热键", command=self.register_hotkeys).grid(row=0, column=4, padx=8)
        ttk.Label(settings, text="播放次数（0=无限）：").grid(row=0, column=5, padx=8)
        ttk.Entry(settings, textvariable=self.play_count, width=10).grid(row=0, column=6)
        ttk.Button(settings, text="保存", command=self.save_config).grid(row=1, column=1, pady=7)
        ttk.Button(settings, text="加载", command=self.load_config).grid(row=1, column=2, pady=7)

        ttk.Label(
            self.root,
            text="录制时会保存真实 KEYDOWN/KEYUP 时间。按住 Ctrl/Shift/Alt/Win 再按其他键，会自然形成组合键；双击事件可调整等待时间。",
            wraplength=880
        ).pack(anchor="w", padx=14, pady=(5, 10))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        total = 0.0
        for i, e in enumerate(self.events, 1):
            total += float(e["wait_ms"])
            self.tree.insert("", "end", iid=str(i-1), values=(
                i, e["action"], e["name"], f'{e["wait_ms"]:.1f}', f'{total:.1f}'
            ))

    def toggle_record(self):
        self.stop_play()
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        self.events.clear()
        self.refresh()
        self.last_event_time = None
        self.down_keys.clear()
        self.recording = True
        self.status.set("● 正在录制……请直接操作键盘")
        self.rec_btn.config(text="■ 停止录制")
        self.hook_thread = threading.Thread(target=self._hook_worker, daemon=True)
        self.hook_thread.start()

    def stop_record(self):
        self.recording = False
        self.status.set(f"录制完成：{len(self.events)} 个事件")
        self.rec_btn.config(text="● 开始录制")
        if self.hook_thread and self.hook_thread.ident:
            try:
                user32.PostThreadMessageW(self.hook_thread.ident, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _hook_worker(self):
        self.hook_proc = HOOKPROC(self._hook_callback)
        self.hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self.hook_proc, kernel32.GetModuleHandleW(None), 0
        )
        if not self.hook_handle:
            self.root.after(0, lambda: messagebox.showerror(
                "错误", "无法安装全局键盘钩子，请尝试以管理员身份运行。"))
            self.root.after(0, self.stop_record)
            return
        msg = wintypes.MSG()
        while self.recording and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self.hook_handle)
        self.hook_handle = None

    def _hook_callback(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.recording:
            data = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if data.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if down or up:
                vk = int(data.vkCode)
                # 防止极少数重复 WM_KEYDOWN 造成重复记录
                if down and vk in self.down_keys:
                    return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

                now = time.perf_counter()
                wait = 0.0 if self.last_event_time is None else (now - self.last_event_time) * 1000
                self.last_event_time = now

                if down:
                    self.down_keys.add(vk)
                elif up:
                    self.down_keys.discard(vk)

                e = {
                    "vk": vk,
                    "scan": int(data.scanCode),
                    "name": key_name(vk),
                    "action": "按下" if down else "松开",
                    "wait_ms": round(wait, 3),
                }
                self.events.append(e)
                self.root.after(0, self.refresh)
        return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

    def apply_wait(self):
        try:
            v = max(0, min(int(self.default_wait.get()), 60000))
        except ValueError:
            messagebox.showwarning("提示", "请输入 0～60000 的整数。")
            return
        for e in self.events:
            e["wait_ms"] = v
        self.refresh()

    def edit_wait(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个事件。")
            return
        idx = int(sel[0])
        win = tk.Toplevel(self.root)
        win.title("修改事件间隔")
        win.resizable(False, False)
        ttk.Label(win, text="该事件执行前等待时间（ms）：").pack(padx=15, pady=12)
        var = tk.StringVar(value=str(self.events[idx]["wait_ms"]))
        ent = ttk.Entry(win, textvariable=var, width=14)
        ent.pack()
        ent.focus_set()
        def ok():
            try:
                v = max(0, min(float(var.get()), 60000))
            except ValueError:
                messagebox.showwarning("提示", "请输入数字。", parent=win)
                return
            self.events[idx]["wait_ms"] = v
            self.refresh()
            win.destroy()
        ttk.Button(win, text="确定", command=ok).pack(pady=12)
        win.bind("<Return>", lambda e: ok())

    def clear(self):
        self.stop_play()
        self.events.clear()
        self.refresh()
        self.status.set("已清空")

    def start_play(self):
        if self.recording:
            self.stop_record()
        if not self.events:
            messagebox.showinfo("提示", "请先录制键盘操作。")
            return
        if self.playing:
            return
        try:
            count = int(self.play_count.get())
            if count < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "播放次数请输入 0 或正整数。")
            return
        self.playing = True
        self.stop_event.clear()
        self.play_btn.config(state="disabled")
        self.status.set("▶ 正在播放")
        threading.Thread(target=self._play_worker, args=(count,), daemon=True).start()

    def _sleep_interruptible(self, sec):
        end = time.perf_counter() + sec
        while self.playing and not self.stop_event.is_set():
            left = end - time.perf_counter()
            if left <= 0:
                return True
            time.sleep(min(left, 0.005))
        return False

    def _play_worker(self, count):
        loops = 0
        held = set()
        try:
            while self.playing and (count == 0 or loops < count):
                for e in list(self.events):
                    if not self.playing or self.stop_event.is_set():
                        break
                    wait = max(0.0, float(e.get("wait_ms", 0))) / 1000
                    if wait and not self._sleep_interruptible(wait):
                        break
                    vk = int(e["vk"])
                    if e["action"] == "按下":
                        send_key(vk, True)
                        held.add(vk)
                    else:
                        send_key(vk, False)
                        held.discard(vk)
                loops += 1
        finally:
            for vk in list(held):
                send_key(vk, False)
            self.root.after(0, self._play_finished)

    def _play_finished(self):
        self.playing = False
        self.stop_event.set()
        self.play_btn.config(state="normal")
        self.status.set("就绪")

    def stop_play(self):
        if self.playing:
            self.playing = False
            self.stop_event.set()
            self.status.set("已停止")

    def _start_hotkey_thread(self):
        self.hotkey_thread = threading.Thread(target=self._hotkey_worker, daemon=True)
        self.hotkey_thread.start()

    def stop_hotkey_thread(self):
        if self.hotkey_thread and self.hotkey_thread.ident:
            try:
                user32.PostThreadMessageW(self.hotkey_thread.ident, WM_QUIT, 0, 0)
            except Exception:
                pass
        time.sleep(0.05)

    def register_hotkeys(self):
        self.stop_hotkey_thread()
        self._start_hotkey_thread()
        self.status.set("热键正在更新")

    def _hotkey_worker(self):
        try:
            pm, pv = parse_hotkey(self.play_hotkey.get())
            sm, sv = parse_hotkey(self.stop_hotkey.get())
        except ValueError as e:
            self.root.after(0, lambda: messagebox.showwarning("热键错误", str(e)))
            return

        ok1 = user32.RegisterHotKey(None, HOTKEY_PLAY, pm, pv)
        ok2 = user32.RegisterHotKey(None, HOTKEY_STOP, sm, sv)
        if not ok1 or not ok2:
            if ok1: user32.UnregisterHotKey(None, HOTKEY_PLAY)
            if ok2: user32.UnregisterHotKey(None, HOTKEY_STOP)
            self.root.after(0, lambda: messagebox.showwarning(
                "热键注册失败", "该热键可能已被 Windows 或其他程序占用，请更换。"))
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_PLAY:
                    self.root.after(0, self.start_play)
                elif msg.wParam == HOTKEY_STOP:
                    self.root.after(0, self.stop_play)

        user32.UnregisterHotKey(None, HOTKEY_PLAY)
        user32.UnregisterHotKey(None, HOTKEY_STOP)

    def save_config(self):
        path = filedialog.asksaveasfilename(
            title="保存配置", defaultextension=".json",
            filetypes=[("JSON 配置", "*.json")]
        )
        if not path:
            return
        data = {
            "version": 3,
            "play_hotkey": self.play_hotkey.get(),
            "stop_hotkey": self.stop_hotkey.get(),
            "play_count": self.play_count.get(),
            "default_wait": self.default_wait.get(),
            "events": self.events,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.status.set("配置已保存")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def load_config(self):
        path = filedialog.askopenfilename(
            title="加载配置", filetypes=[("JSON 配置", "*.json")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.events = data.get("events", [])
            self.play_hotkey.set(data.get("play_hotkey", "F8"))
            self.stop_hotkey.set(data.get("stop_hotkey", "F9"))
            self.play_count.set(str(data.get("play_count", "1")))
            self.default_wait.set(int(data.get("default_wait", 50)))
            self.refresh()
            self.register_hotkeys()
            self.status.set("配置已加载")
        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    def close(self):
        self.recording = False
        self.stop_play()
        self.stop_hotkey_thread()
        try:
            if self.hook_handle:
                user32.UnhookWindowsHookEx(self.hook_handle)
        except Exception:
            pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()

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
import sys
from pathlib import Path
from ctypes import wintypes
import json
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 64-bit Windows/ctypes compatibility: explicitly declare pointer-sized types.
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD
]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
]
user32.GetMessageW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

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
        self.root.title("键盘宏")
        self.root.geometry("980x720")
        self.root.minsize(900, 650)

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
        self.profile_var = tk.StringVar()
        self.profiles = {}
        self.profile_file = self._profile_path()

        self._setup_style()
        self._build_ui()
        self._load_profiles()
        self._start_hotkey_thread()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _profile_path(self):
        try:
            base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        except Exception:
            base = Path.cwd()
        return base / "keyboard_macro_profiles.json"

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        self.font = ("Microsoft YaHei UI", 10)
        self.big_font = ("Microsoft YaHei UI", 11)
        self.title_font = ("Microsoft YaHei UI", 20, "bold")
        style.configure("App.TFrame", padding=0)
        style.configure("Title.TLabel", font=self.title_font)
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Action.TButton", font=self.big_font, padding=(16, 9))
        style.configure("SmallAction.TButton", font=self.font, padding=(12, 6))
        style.configure("Section.TLabelframe", padding=10)
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=30)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=6)
        style.configure("Profile.TCombobox", font=self.big_font)

    def _build_ui(self):
        outer = ttk.Frame(self.root, style="App.TFrame")
        outer.pack(fill="both", expand=True, padx=24, pady=18)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 18))
        ttk.Label(header, text="键盘宏", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right", pady=7)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 16))
        self.rec_btn = ttk.Button(actions, text="●  开始录制", style="Action.TButton", command=self.toggle_record)
        self.rec_btn.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.play_btn = ttk.Button(actions, text="▶  播放", style="Action.TButton", command=self.start_play)
        self.play_btn.pack(side="left", fill="x", expand=True, padx=10)
        self.stop_btn = ttk.Button(actions, text="■  停止", style="Action.TButton", command=self.stop_play)
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(actions, text="⌫  清空", style="Action.TButton", command=self.clear).pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(actions, text="⚙  设置", style="Action.TButton", command=self.open_settings).pack(side="left", fill="x", expand=True, padx=(10, 0))

        profile_box = ttk.LabelFrame(outer, text="快捷键配置", style="Section.TLabelframe")
        profile_box.pack(fill="x", pady=(0, 16))
        profile_box.columnconfigure(1, weight=1)
        ttk.Label(profile_box, text="当前快捷键配置：", font=self.big_font).grid(row=0, column=0, padx=(4, 12), pady=5, sticky="w")
        self.profile_combo = ttk.Combobox(profile_box, textvariable=self.profile_var, state="readonly", style="Profile.TCombobox")
        self.profile_combo.grid(row=0, column=1, padx=(0, 12), pady=5, sticky="ew", ipady=3)
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        ttk.Button(profile_box, text="📁  新建", style="SmallAction.TButton", command=self.new_profile).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(profile_box, text="✎  重命名", style="SmallAction.TButton", command=self.rename_profile).grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(profile_box, text="🗑  删除", style="SmallAction.TButton", command=self.delete_profile).grid(row=0, column=4, padx=5, pady=5)
        ttk.Button(profile_box, text="💾  保存", style="SmallAction.TButton", command=self.save_current_profile).grid(row=0, column=5, padx=(5, 0), pady=5)

        list_box = ttk.LabelFrame(outer, text="当前宏动作", style="Section.TLabelframe")
        list_box.pack(fill="both", expand=True)
        cols = ("no", "action", "key", "state", "wait", "total")
        self.tree = ttk.Treeview(list_box, columns=cols, show="headings", selectmode="browse")
        headers = {"no":"序号", "action":"动作", "key":"按键", "state":"按键状态", "wait":"与上一动作间隔 (ms)", "total":"累计时间 (ms)"}
        widths = {"no":65, "action":90, "key":150, "state":110, "wait":190, "total":160}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="center", stretch=(c in ("key", "wait", "total")))
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        scroll = ttk.Scrollbar(list_box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_wait())

        hint = ttk.Frame(outer)
        hint.pack(fill="x", pady=(8, 0))
        ttk.Label(hint, text="双击动作列表中的项目，可以修改该动作与上一动作之间的间隔。", font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Button(hint, text="修改选中间隔", style="SmallAction.TButton", command=self.edit_wait).pack(side="right")

    def _load_profiles(self):
        try:
            if self.profile_file.exists():
                data = json.loads(self.profile_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.profiles = data.get("profiles", {})
        except Exception:
            self.profiles = {}
        if not self.profiles:
            self.profiles = {"默认配置": self._snapshot()}
            self._save_profiles()
        self._refresh_profile_combo()
        first = next(iter(self.profiles))
        self.profile_var.set(first)
        self._apply_snapshot(self.profiles[first])

    def _snapshot(self):
        return {
            "version": 4,
            "play_hotkey": self.play_hotkey.get(),
            "stop_hotkey": self.stop_hotkey.get(),
            "play_count": self.play_count.get(),
            "default_wait": self.default_wait.get(),
            "events": self.events,
        }

    def _apply_snapshot(self, data):
        self.events = list(data.get("events", []))
        self.play_hotkey.set(data.get("play_hotkey", "F8"))
        self.stop_hotkey.set(data.get("stop_hotkey", "F9"))
        self.play_count.set(str(data.get("play_count", "1")))
        try:
            self.default_wait.set(int(data.get("default_wait", 50)))
        except Exception:
            self.default_wait.set(50)
        self.refresh()

    def _save_profiles(self):
        try:
            self.profile_file.write_text(json.dumps({"version": 4, "profiles": self.profiles}, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存配置：\n{e}")
            return False

    def _refresh_profile_combo(self):
        names = list(self.profiles.keys())
        self.profile_combo["values"] = names
        if self.profile_var.get() not in names and names:
            self.profile_var.set(names[0])

    def save_current_profile(self, quiet=False):
        name = self.profile_var.get().strip()
        if not name:
            self.new_profile()
            return
        self.profiles[name] = self._snapshot()
        if self._save_profiles() and not quiet:
            self.status.set(f"已保存：{name}")

    def new_profile(self):
        name = self._ask_text("新建快捷键配置", "请输入配置名称：", "新配置")
        if not name:
            return
        if name in self.profiles:
            messagebox.showwarning("提示", "这个名称已经存在，请换一个名称。")
            return
        self.profiles[name] = self._snapshot()
        self.profile_var.set(name)
        self._refresh_profile_combo()
        self.profile_var.set(name)
        self._save_profiles()
        self.status.set(f"已创建：{name}")

    def rename_profile(self):
        old = self.profile_var.get().strip()
        if not old:
            return
        name = self._ask_text("重命名配置", "请输入新的配置名称：", old)
        if not name or name == old:
            return
        if name in self.profiles:
            messagebox.showwarning("提示", "这个名称已经存在，请换一个名称。")
            return
        self.profiles[name] = self.profiles.pop(old)
        self.profile_var.set(name)
        self._refresh_profile_combo()
        self.profile_var.set(name)
        self._save_profiles()
        self.status.set(f"已重命名为：{name}")

    def delete_profile(self):
        name = self.profile_var.get().strip()
        if not name or len(self.profiles) <= 1:
            messagebox.showinfo("提示", "至少需要保留一个快捷键配置。")
            return
        if not messagebox.askyesno("删除配置", f"确定删除“{name}”吗？"):
            return
        del self.profiles[name]
        new_name = next(iter(self.profiles))
        self.profile_var.set(new_name)
        self._refresh_profile_combo()
        self.profile_var.set(new_name)
        self._apply_snapshot(self.profiles[new_name])
        self._save_profiles()
        self.status.set(f"已删除：{name}")

    def on_profile_selected(self, _event=None):
        name = self.profile_var.get()
        if name in self.profiles:
            self.stop_play()
            self._apply_snapshot(self.profiles[name])
            self.status.set(f"已切换：{name}")

    def _ask_text(self, title, prompt, initial=""):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=prompt, font=self.big_font).pack(padx=22, pady=(20, 8))
        var = tk.StringVar(value=initial)
        ent = ttk.Entry(win, textvariable=var, width=28, font=self.big_font)
        ent.pack(padx=22, pady=5)
        ent.focus_set(); ent.select_range(0, "end")
        result = {"value": None}
        def ok():
            v = var.get().strip()
            if v:
                result["value"] = v
                win.destroy()
        ttk.Button(win, text="确定", command=ok).pack(pady=(8, 18))
        win.bind("<Return>", lambda e: ok())
        self.root.wait_window(win)
        return result["value"]

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("560x300")
        win.resizable(False, False)
        win.transient(self.root)
        box = ttk.LabelFrame(win, text="播放设置", style="Section.TLabelframe")
        box.pack(fill="both", expand=True, padx=18, pady=18)
        for i in range(2): box.columnconfigure(i, weight=1)
        ttk.Label(box, text="播放热键", font=self.big_font).grid(row=0, column=0, padx=15, pady=12, sticky="w")
        ttk.Entry(box, textvariable=self.play_hotkey, font=self.big_font).grid(row=0, column=1, padx=15, pady=12, sticky="ew")
        ttk.Label(box, text="停止热键", font=self.big_font).grid(row=1, column=0, padx=15, pady=12, sticky="w")
        ttk.Entry(box, textvariable=self.stop_hotkey, font=self.big_font).grid(row=1, column=1, padx=15, pady=12, sticky="ew")
        ttk.Label(box, text="播放次数（0 = 无限）", font=self.big_font).grid(row=2, column=0, padx=15, pady=12, sticky="w")
        ttk.Entry(box, textvariable=self.play_count, font=self.big_font).grid(row=2, column=1, padx=15, pady=12, sticky="ew")
        ttk.Label(box, text="默认间隔（ms）", font=self.big_font).grid(row=3, column=0, padx=15, pady=12, sticky="w")
        ttk.Entry(box, textvariable=self.default_wait, font=self.big_font).grid(row=3, column=1, padx=15, pady=12, sticky="ew")
        def apply_and_close():
            try:
                self.default_wait.set(max(0, min(int(self.default_wait.get()), 60000)))
                c = int(self.play_count.get())
                if c < 0: raise ValueError
            except Exception:
                messagebox.showwarning("提示", "播放次数请输入 0 或正整数，默认间隔请输入 0～60000。", parent=win)
                return
            self.register_hotkeys()
            self.save_current_profile(quiet=True)
            win.destroy()
            self.status.set("设置已保存")
        ttk.Button(win, text="保存设置", style="Action.TButton", command=apply_and_close).pack(pady=(0, 18))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        total = 0.0
        for i, e in enumerate(self.events, 1):
            total += float(e.get("wait_ms", 0))
            action = e.get("action", "")
            state = action
            self.tree.insert("", "end", iid=str(i-1), values=(i, action, e.get("name", ""), state, f'{float(e.get("wait_ms",0)):.1f}', f'{total:.1f}'))

    def toggle_record(self):
        self.stop_play()
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        self.events.clear(); self.refresh()
        self.last_event_time = None; self.down_keys.clear(); self.recording = True
        self.status.set("● 正在录制……请直接操作键盘")
        self.rec_btn.config(text="■  停止录制")
        self.hook_thread = threading.Thread(target=self._hook_worker, daemon=True)
        self.hook_thread.start()

    def stop_record(self):
        self.recording = False
        self.status.set(f"录制完成，共 {len(self.events)} 个动作")
        self.rec_btn.config(text="●  开始录制")
        if self.hook_thread and self.hook_thread.ident:
            try: user32.PostThreadMessageW(self.hook_thread.ident, WM_QUIT, 0, 0)
            except Exception: pass
        self.save_current_profile(quiet=True)

    def _hook_worker(self):
        self.hook_proc = HOOKPROC(self._hook_callback)
        self.hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.hook_proc, kernel32.GetModuleHandleW(None), 0)
        if not self.hook_handle:
            err = ctypes.get_last_error()
            self.root.after(0, lambda: messagebox.showerror("错误", f"无法安装全局键盘钩子。\nWindows 错误代码：{err}"))
            self.root.after(0, self.stop_record); return
        msg = wintypes.MSG()
        while self.recording and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self.hook_handle); self.hook_handle = None

    def _hook_callback(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.recording:
            data = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if data.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN); up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if down or up:
                vk = int(data.vkCode)
                if down and vk in self.down_keys:
                    return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)
                now = time.perf_counter(); wait = 0.0 if self.last_event_time is None else (now-self.last_event_time)*1000
                self.last_event_time = now
                if down: self.down_keys.add(vk)
                else: self.down_keys.discard(vk)
                self.events.append({"vk":vk,"scan":int(data.scanCode),"name":key_name(vk),"action":"按下" if down else "松开","wait_ms":round(wait,3)})
                self.root.after(0, self.refresh)
        return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

    def apply_wait(self):
        try: v=max(0,min(int(self.default_wait.get()),60000))
        except ValueError:
            messagebox.showwarning("提示","请输入 0～60000 的整数。"); return
        for e in self.events: e["wait_ms"]=v
        self.refresh(); self.save_current_profile(quiet=True)

    def edit_wait(self):
        sel=self.tree.selection()
        if not sel:
            messagebox.showinfo("提示","请先选择一个动作。"); return
        idx=int(sel[0]); win=tk.Toplevel(self.root); win.title("修改间隔"); win.resizable(False,False); win.transient(self.root)
        ttk.Label(win,text="该动作与上一动作之间的间隔（ms）：",font=self.big_font).pack(padx=20,pady=(18,10))
        var=tk.StringVar(value=str(self.events[idx].get("wait_ms",0))); ent=ttk.Entry(win,textvariable=var,width=16,font=self.big_font); ent.pack(padx=20); ent.focus_set()
        def ok():
            try: v=max(0,min(float(var.get()),60000))
            except ValueError:
                messagebox.showwarning("提示","请输入数字。",parent=win); return
            self.events[idx]["wait_ms"]=v; self.refresh(); self.save_current_profile(quiet=True); win.destroy()
        ttk.Button(win,text="确定",command=ok).pack(pady=16); win.bind("<Return>",lambda e:ok())

    def clear(self):
        self.stop_play(); self.events.clear(); self.refresh(); self.status.set("已清空"); self.save_current_profile(quiet=True)

    def start_play(self):
        if self.recording: self.stop_record()
        if not self.events:
            messagebox.showinfo("提示","请先录制键盘操作。"); return
        if self.playing: return
        try:
            count=int(self.play_count.get());
            if count<0: raise ValueError
        except ValueError:
            messagebox.showwarning("提示","播放次数请输入 0 或正整数。"); return
        self.playing=True; self.stop_event.clear(); self.play_btn.config(state="disabled"); self.status.set("▶ 正在播放")
        threading.Thread(target=self._play_worker,args=(count,),daemon=True).start()

    def _sleep_interruptible(self,sec):
        end=time.perf_counter()+sec
        while self.playing and not self.stop_event.is_set():
            left=end-time.perf_counter()
            if left<=0:return True
            time.sleep(min(left,0.005))
        return False

    def _play_worker(self,count):
        loops=0; held=set()
        try:
            while self.playing and (count==0 or loops<count):
                for e in list(self.events):
                    if not self.playing or self.stop_event.is_set(): break
                    wait=max(0,float(e.get("wait_ms",0)))/1000
                    if wait and not self._sleep_interruptible(wait): break
                    vk=int(e["vk"])
                    if e["action"]=="按下": send_key(vk,True); held.add(vk)
                    else: send_key(vk,False); held.discard(vk)
                loops+=1
        finally:
            for vk in list(held): send_key(vk,False)
            self.root.after(0,self._play_finished)

    def _play_finished(self):
        self.playing=False; self.stop_event.set(); self.play_btn.config(state="normal"); self.status.set("就绪")

    def stop_play(self):
        if self.playing:
            self.playing=False; self.stop_event.set(); self.status.set("已停止")

    def _start_hotkey_thread(self):
        self.hotkey_thread=threading.Thread(target=self._hotkey_worker,daemon=True); self.hotkey_thread.start()

    def stop_hotkey_thread(self):
        if self.hotkey_thread and self.hotkey_thread.ident:
            try: user32.PostThreadMessageW(self.hotkey_thread.ident,WM_QUIT,0,0)
            except Exception: pass
        time.sleep(0.05)

    def register_hotkeys(self):
        self.stop_hotkey_thread(); self._start_hotkey_thread(); self.status.set("热键正在更新")

    def _hotkey_worker(self):
        try: pm,pv=parse_hotkey(self.play_hotkey.get()); sm,sv=parse_hotkey(self.stop_hotkey.get())
        except ValueError as e:
            self.root.after(0,lambda:messagebox.showwarning("热键错误",str(e))); return
        ok1=user32.RegisterHotKey(None,HOTKEY_PLAY,pm,pv); ok2=user32.RegisterHotKey(None,HOTKEY_STOP,sm,sv)
        if not ok1 or not ok2:
            if ok1:user32.UnregisterHotKey(None,HOTKEY_PLAY)
            if ok2:user32.UnregisterHotKey(None,HOTKEY_STOP)
            self.root.after(0,lambda:messagebox.showwarning("热键注册失败","该热键可能已被其他程序占用，请更换。")); return
        msg=wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            if msg.message==WM_HOTKEY:
                if msg.wParam==HOTKEY_PLAY:self.root.after(0,self.start_play)
                elif msg.wParam==HOTKEY_STOP:self.root.after(0,self.stop_play)
        user32.UnregisterHotKey(None,HOTKEY_PLAY); user32.UnregisterHotKey(None,HOTKEY_STOP)

    def close(self):
        self.save_current_profile(quiet=True)
        self.recording=False; self.stop_play(); self.stop_hotkey_thread()
        try:
            if self.hook_handle:user32.UnhookWindowsHookEx(self.hook_handle)
        except Exception: pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()

import os
# -*- coding: utf-8 -*-
"""
键盘快捷键录制器 v3.1 - 真实按下/松开时间版
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
from tkinter import ttk, filedialog, messagebox, simpledialog

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

# Windows INPUT/KEYBDINPUT must use ULONG_PTR and the real INPUT union layout.
# The previous version used POINTER(ULONG), which changes alignment on 64-bit
# Python and can make SendInput silently fail.
ULONG_PTR = ctypes.c_size_t

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION),
    ]

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

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
    inp.ki = KEYBDINPUT(
        int(vk), 0, 0 if down else KEYEVENTF_KEYUP, 0, 0
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return sent == 1

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
    """键盘宏 v4：按最终 UI 效果图重做的界面。"""
    def __init__(self, root):
        self.root = root
        self.root.title("键盘宏")
        self.root.geometry("1536x1024")
        self.root.minsize(1000, 760)
        self.root.configure(bg="#ffffff")

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
        self.current_config = tk.StringVar(value="")
        self.config_names = []

        self.base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
        os.makedirs(self.base_dir, exist_ok=True)

        self._build_ui()
        self._load_config_names()
        self._start_hotkey_thread()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(250, self._refresh_status)

    def _font(self, size=14, bold=False):
        return ("Microsoft YaHei UI", size, "bold" if bold else "normal")

    def _build_ui(self):
        # 整体白底，接近效果图的轻量 Windows 软件风格。
        outer = tk.Frame(self.root, bg="#ffffff", highlightthickness=0)
        outer.pack(fill="both", expand=True, padx=22, pady=18)

        # 标题栏
        title = tk.Frame(outer, bg="#ffffff", height=70)
        title.pack(fill="x")
        title.pack_propagate(False)
        tk.Label(title, text="🪶  键盘宏", bg="#ffffff", fg="#111111",
                 font=self._font(25, True)).pack(side="left", padx=4, pady=10)
        tk.Label(title, textvariable=self.status, bg="#ffffff", fg="#111111",
                 font=self._font(13)).pack(side="right", padx=12)

        # 顶部五个大按钮
        actions = tk.Frame(outer, bg="#ffffff")
        actions.pack(fill="x", pady=(10, 22))
        self.rec_btn = self._big_button(actions, "🔴  开始录制", self.toggle_record)
        self.rec_btn.pack(side="left", fill="x", expand=True, padx=(0, 14))
        self.play_btn = self._big_button(actions, "▶  播放", self.start_play)
        self.play_btn.pack(side="left", fill="x", expand=True, padx=7)
        self.stop_btn = self._big_button(actions, "⬛  停止", self.stop_play)
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=7)
        self.clear_btn = self._big_button(actions, "🧹  清空", self.clear)
        self.clear_btn.pack(side="left", fill="x", expand=True, padx=7)
        self.settings_btn = self._big_button(actions, "⚙  设置", self.open_settings)
        self.settings_btn.pack(side="left", fill="x", expand=True, padx=(14, 0))

        # 配置栏
        cfg = tk.Frame(outer, bg="#ffffff")
        cfg.pack(fill="x", pady=(0, 22))
        tk.Label(cfg, text="当前快捷键配置：", bg="#ffffff", fg="#111111",
                 font=self._font(18)).pack(side="left", padx=(10, 18))
        self.config_combo = ttk.Combobox(cfg, textvariable=self.current_config,
                                         state="readonly", font=self._font(15), height=12)
        self.config_combo.pack(side="left", fill="x", expand=True, ipady=9)
        self.config_combo.bind("<<ComboboxSelected>>", lambda e: self._switch_config())
        self._medium_button(cfg, "📁  新建", self.new_config).pack(side="left", padx=(18, 10), ipadx=15, ipady=8)
        self._medium_button(cfg, "✎  重命名", self.rename_config).pack(side="left", padx=5, ipadx=15, ipady=8)
        self._medium_button(cfg, "🗑  删除", self.delete_config).pack(side="left", padx=(10, 0), ipadx=15, ipady=8)

        # 动作列表
        list_box = tk.Frame(outer, bg="#ffffff", highlightbackground="#d9dfe7",
                            highlightthickness=1)
        list_box.pack(fill="both", expand=True, pady=(0, 22))
        list_title = tk.Label(list_box, text="动作列表（共 0 个动作）", bg="#ffffff", fg="#111111",
                              font=self._font(18), anchor="w")
        list_title.pack(fill="x", padx=20, pady=(15, 10))
        self.list_title = list_title

        table_frame = tk.Frame(list_box, bg="#ffffff")
        table_frame.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        cols = ("no", "action", "key", "state", "wait", "total")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        headings = [
            ("no", "序号", 90), ("action", "动作", 170), ("key", "按键", 220),
            ("state", "按键状态", 220), ("wait", "与上一动作间隔 (ms)", 350),
            ("total", "累计时间 (ms)", 300)
        ]
        for col, text, width in headings:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=70, anchor="center", stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_wait())

        # 底部设置区
        bottom = tk.Frame(outer, bg="#ffffff", highlightbackground="#d9dfe7", highlightthickness=1)
        bottom.pack(fill="x")
        fields = tk.Frame(bottom, bg="#ffffff")
        fields.pack(fill="x", padx=28, pady=20)
        self._field(fields, "播放热键：", self.play_hotkey, 0)
        self._field(fields, "停止热键：", self.stop_hotkey, 1)
        self._field(fields, "播放次数：", self.play_count, 2, suffix="次")
        self._field(fields, "默认间隔：", self.default_wait, 3, suffix="ms")

        # ttk 样式
        style = ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Treeview", font=self._font(14), rowheight=48,
                        background="#ffffff", fieldbackground="#ffffff", foreground="#111111",
                        borderwidth=0)
        style.configure("Treeview.Heading", font=self._font(14, True),
                        background="#f8fafc", foreground="#111111", relief="flat", padding=8)
        style.map("Treeview", background=[("selected", "#e7f1fb")], foreground=[("selected", "#111111")])
        style.configure("TCombobox", font=self._font(15), padding=8)

    def _big_button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, font=self._font(17),
                         bg="#ffffff", fg="#111111", activebackground="#f2f5f8",
                         activeforeground="#111111", relief="solid", bd=1,
                         highlightthickness=0, cursor="hand2", padx=10, pady=14)

    def _medium_button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, font=self._font(15),
                         bg="#ffffff", fg="#111111", activebackground="#f2f5f8",
                         activeforeground="#111111", relief="solid", bd=1,
                         highlightthickness=0, cursor="hand2")

    def _field(self, parent, label, variable, index, suffix=""):
        box = tk.Frame(parent, bg="#ffffff")
        box.grid(row=0, column=index, sticky="ew", padx=18)
        parent.grid_columnconfigure(index, weight=1)
        tk.Label(box, text=label, bg="#ffffff", fg="#111111", font=self._font(15)).pack(anchor="w", pady=(0,8))
        row = tk.Frame(box, bg="#ffffff")
        row.pack(fill="x")
        ent = ttk.Entry(row, textvariable=variable, font=self._font(15))
        ent.pack(side="left", fill="x", expand=True, ipady=8)
        if suffix:
            tk.Label(row, text=suffix, bg="#ffffff", fg="#111111", font=self._font(15)).pack(side="left", padx=(10,0))

    def _refresh_status(self):
        if self.recording:
            self.status.set(f"录制中 · 共 {len(self.events)} 个动作")
        elif self.playing:
            self.status.set("正在播放")
        elif self.status.get() == "正在播放":
            self.status.set("就绪")
        self.root.after(250, self._refresh_status)

    def _load_config_names(self):
        names = []
        for fn in os.listdir(self.base_dir):
            if fn.lower().endswith(".json"):
                names.append(os.path.splitext(fn)[0])
        names.sort(key=str.lower)
        if not names:
            names = ["默认配置"]
            self._save_named_config("默认配置")
        self.config_names = names
        self.config_combo["values"] = names
        self.current_config.set(names[0])
        self._load_named_config(names[0], silent=True)

    def _config_path(self, name):
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
        return os.path.join(self.base_dir, (safe or "未命名配置") + ".json")

    def _save_named_config(self, name):
        data = {"version": 4, "play_hotkey": self.play_hotkey.get(), "stop_hotkey": self.stop_hotkey.get(),
                "play_count": self.play_count.get(), "default_wait": self.default_wait.get(), "events": self.events}
        try:
            with open(self._config_path(name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self.root)
            return False

    def _load_named_config(self, name, silent=False):
        path = self._config_path(name)
        if not os.path.exists(path):
            self.events = []
            self.refresh()
            return
        try:
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
            self.events = data.get("events", [])
            self.play_hotkey.set(data.get("play_hotkey", "F8"))
            self.stop_hotkey.set(data.get("stop_hotkey", "F9"))
            self.play_count.set(str(data.get("play_count", "1")))
            self.default_wait.set(int(data.get("default_wait", 50)))
            self.refresh()
            if not silent: self.register_hotkeys()
        except Exception as e:
            if not silent: messagebox.showerror("加载失败", str(e), parent=self.root)

    def _switch_config(self):
        name = self.current_config.get()
        if name:
            self._load_named_config(name)
            self.status.set(f"已加载：{name}")

    def new_config(self):
        name = simpledialog.askstring("新建配置", "请输入配置名称：", parent=self.root)
        if not name: return
        name = name.strip()
        if not name: return
        if name in self.config_names:
            messagebox.showwarning("提示", "这个配置名称已经存在。", parent=self.root); return
        self._save_named_config(name)
        self.config_names.append(name); self.config_names.sort(key=str.lower)
        self.config_combo["values"] = self.config_names
        self.current_config.set(name)
        self.events = []; self.refresh(); self._save_named_config(name)
        self.status.set(f"已新建：{name}")

    def rename_config(self):
        old = self.current_config.get()
        if not old: return
        name = simpledialog.askstring("重命名", "请输入新的配置名称：", initialvalue=old, parent=self.root)
        if not name: return
        name = name.strip()
        if not name or name == old: return
        if name in self.config_names:
            messagebox.showwarning("提示", "这个配置名称已经存在。", parent=self.root); return
        old_path = self._config_path(old); new_path = self._config_path(name)
        try: os.replace(old_path, new_path)
        except Exception as e: messagebox.showerror("重命名失败", str(e), parent=self.root); return
        self.config_names.remove(old); self.config_names.append(name); self.config_names.sort(key=str.lower)
        self.config_combo["values"] = self.config_names; self.current_config.set(name)
        self.status.set(f"已重命名：{name}")

    def delete_config(self):
        name = self.current_config.get()
        if not name or len(self.config_names) <= 1:
            messagebox.showinfo("提示", "至少保留一个配置。", parent=self.root); return
        if not messagebox.askyesno("删除配置", f"确定删除“{name}”？", parent=self.root): return
        try: os.remove(self._config_path(name))
        except FileNotFoundError: pass
        self.config_names.remove(name); self.config_combo["values"] = self.config_names
        self.current_config.set(self.config_names[0]); self._load_named_config(self.config_names[0])
        self.status.set("配置已删除")

    def _auto_save(self):
        name = self.current_config.get() or "默认配置"
        self._save_named_config(name)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        total = 0.0
        for i, e in enumerate(self.events, 1):
            wait = float(e.get("wait_ms", 0)); total += wait
            action = e.get("action", "")
            state = action
            self.tree.insert("", "end", iid=str(i-1), values=(i, action, e.get("name", ""), state,
                              "-" if i == 1 else f"{wait:.1f}", f"{total:.1f}"))
        self.list_title.config(text=f"动作列表（共 {len(self.events)} 个动作）")

    def toggle_record(self):
        self.stop_play()
        if self.recording: self.stop_record()
        else: self.start_record()

    def start_record(self):
        self.events.clear(); self.refresh(); self.last_event_time = None; self.down_keys.clear()
        self.recording = True; self.rec_btn.config(text="⏹  停止录制")
        self.status.set("录制中")
        self.hook_thread = threading.Thread(target=self._hook_worker, daemon=True); self.hook_thread.start()

    def stop_record(self):
        self.recording = False; self.rec_btn.config(text="🔴  开始录制")
        self.status.set(f"录制完成 · 共 {len(self.events)} 个动作")
        self._auto_save()
        if self.hook_thread and self.hook_thread.ident:
            try: user32.PostThreadMessageW(self.hook_thread.ident, WM_QUIT, 0, 0)
            except Exception: pass

    def _hook_worker(self):
        self.hook_proc = HOOKPROC(self._hook_callback)
        self.hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.hook_proc, kernel32.GetModuleHandleW(None), 0)
        if not self.hook_handle:
            err = ctypes.get_last_error()
            self.root.after(0, lambda: messagebox.showerror("错误", f"无法安装全局键盘钩子。\nWindows 错误代码：{err}", parent=self.root))
            self.root.after(0, self.stop_record); return
        msg = wintypes.MSG()
        while self.recording and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        try: user32.UnhookWindowsHookEx(self.hook_handle)
        except Exception: pass
        self.hook_handle = None

    def _hook_callback(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.recording:
            data = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if data.flags & LLKHF_INJECTED: return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN); up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if down or up:
                vk = int(data.vkCode)
                if down and vk in self.down_keys: return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)
                now = time.perf_counter(); wait = 0 if self.last_event_time is None else (now-self.last_event_time)*1000; self.last_event_time = now
                if down: self.down_keys.add(vk)
                else: self.down_keys.discard(vk)
                self.events.append({"vk":vk,"scan":int(data.scanCode),"name":key_name(vk),"action":"按下" if down else "松开","wait_ms":round(wait,3)})
                self.root.after(0, self.refresh)
        return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

    def start_play(self):
        if self.recording: self.stop_record()
        if not self.events: messagebox.showinfo("提示", "请先录制键盘操作。", parent=self.root); return
        if self.playing: return
        try:
            count = int(self.play_count.get());
            if count < 0: raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "播放次数请输入 0 或正整数。", parent=self.root); return
        self.playing=True; self.stop_event.clear(); self.play_btn.config(state="disabled"); self.status.set("正在播放")
        threading.Thread(target=self._play_worker,args=(count,),daemon=True).start()

    def _sleep_interruptible(self, sec):
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
                    if not self._sleep_interruptible(max(0,float(e.get("wait_ms",0)))/1000): break
                    vk=int(e["vk"])
                    if not send_key(vk,e["action"]=="按下"): raise RuntimeError(f"SendInput 发送失败，Windows 错误代码：{ctypes.get_last_error()}")
                    if e["action"]=="按下": held.add(vk)
                    else: held.discard(vk)
                loops+=1
        except Exception as e:
            self.root.after(0,lambda err=str(e):messagebox.showerror("播放失败",err,parent=self.root))
        finally:
            for vk in list(held): send_key(vk,False)
            self.root.after(0,self._play_finished)

    def _play_finished(self):
        self.playing=False; self.stop_event.set(); self.play_btn.config(state="normal"); self.status.set("就绪")

    def stop_play(self):
        if self.playing: self.playing=False; self.stop_event.set(); self.status.set("已停止")

    def clear(self):
        self.stop_play(); self.events.clear(); self.refresh(); self._auto_save(); self.status.set("已清空")

    def edit_wait(self):
        sel=self.tree.selection()
        if not sel: return
        idx=int(sel[0]); win=tk.Toplevel(self.root); win.title("修改事件间隔"); win.resizable(False,False)
        tk.Label(win,text="该事件执行前等待时间（ms）：",font=self._font(12)).pack(padx=15,pady=12)
        var=tk.StringVar(value=str(self.events[idx].get("wait_ms",0))); ent=ttk.Entry(win,textvariable=var,width=16); ent.pack(); ent.focus_set()
        def ok():
            try:v=max(0,min(float(var.get()),60000))
            except ValueError: messagebox.showwarning("提示","请输入数字。",parent=win); return
            self.events[idx]["wait_ms"]=v; self.refresh(); self._auto_save(); win.destroy()
        ttk.Button(win,text="确定",command=ok).pack(pady=12); win.bind("<Return>",lambda e:ok())

    def open_settings(self):
        win=tk.Toplevel(self.root); win.title("设置"); win.resizable(False,False); win.transient(self.root); win.grab_set()
        frame=ttk.Frame(win,padding=18); frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="设置",font=self._font(16,True)).grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,14))
        ttk.Label(frame,text="默认间隔(ms)：").grid(row=1,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=self.default_wait,width=12).grid(row=1,column=1)
        ttk.Button(frame,text="应用到全部事件",command=self.apply_wait).grid(row=1,column=2,padx=8)
        ttk.Label(frame,text="播放次数(0=无限)：").grid(row=2,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=self.play_count,width=12).grid(row=2,column=1)
        ttk.Button(frame,text="保存",command=lambda:(self._auto_save(),win.destroy())).grid(row=3,column=1,pady=12,sticky="e")

    def apply_wait(self):
        try:v=max(0,min(int(self.default_wait.get()),60000))
        except ValueError: messagebox.showwarning("提示","请输入 0～60000 的整数。",parent=self.root); return
        for e in self.events:e["wait_ms"]=v
        self.refresh(); self._auto_save(); self.status.set("已应用默认间隔")

    def stop_hotkey_thread(self):
        if self.hotkey_thread and self.hotkey_thread.ident:
            try:user32.PostThreadMessageW(self.hotkey_thread.ident,WM_QUIT,0,0)
            except Exception:pass
        time.sleep(0.05)

    def _start_hotkey_thread(self):
        self.hotkey_thread=threading.Thread(target=self._hotkey_worker,daemon=True); self.hotkey_thread.start()

    def register_hotkeys(self):
        self.stop_hotkey_thread(); self._start_hotkey_thread(); self._auto_save(); self.status.set("热键已更新")

    def _hotkey_worker(self):
        try:pm,pv=parse_hotkey(self.play_hotkey.get()); sm,sv=parse_hotkey(self.stop_hotkey.get())
        except ValueError as e:self.root.after(0,lambda:messagebox.showwarning("热键错误",str(e),parent=self.root)); return
        ok1=user32.RegisterHotKey(None,HOTKEY_PLAY,pm,pv); ok2=user32.RegisterHotKey(None,HOTKEY_STOP,sm,sv)
        if not ok1 or not ok2:
            if ok1:user32.UnregisterHotKey(None,HOTKEY_PLAY)
            if ok2:user32.UnregisterHotKey(None,HOTKEY_STOP)
            self.root.after(0,lambda:messagebox.showwarning("热键注册失败","该热键可能已被占用，请更换。",parent=self.root)); return
        msg=wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            if msg.message==WM_HOTKEY:
                if msg.wParam==HOTKEY_PLAY:self.root.after(0,self.start_play)
                elif msg.wParam==HOTKEY_STOP:self.root.after(0,self.stop_play)
        user32.UnregisterHotKey(None,HOTKEY_PLAY); user32.UnregisterHotKey(None,HOTKEY_STOP)

    def close(self):
        self.recording=False; self.stop_play(); self._auto_save(); self.stop_hotkey_thread()
        try:
            if self.hook_handle:user32.UnhookWindowsHookEx(self.hook_handle)
        except Exception:pass
        self.root.destroy()

if __name__ == "__main__":
    import os
    from tkinter import simpledialog
    root=tk.Tk()
    App(root)
    root.mainloop()

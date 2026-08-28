# -*- coding: utf-8 -*-
"""
键盘宏 - 极简配置版
Windows 10/11
功能：
- 全局录制键盘 KEYDOWN / KEYUP
- 不限制按键数量
- 支持组合键
- 每个动作独立间隔
- 保存多个“快捷键配置”，可自行命名
- 下次打开自动恢复配置
- 主界面仅保留录制/播放/停止/清空
- 播放/停止热键、播放次数、默认间隔放进“设置”
"""

import ctypes
from ctypes import wintypes
import json
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HC_ACTION = 0
LLKHF_INJECTED = 0x00000010

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

HOTKEY_PLAY = 6101
HOTKEY_STOP = 6102

ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUTUNION),
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
    VK_NAMES[0x70 + i - 1] = f"F{i}"
for i in range(10):
    VK_NAMES[0x30 + i] = str(i)
    VK_NAMES[0x60 + i] = f"Num{i}"
for i in range(26):
    VK_NAMES[0x41 + i] = chr(65 + i)

NAME_TO_VK = {v.upper(): k for k, v in VK_NAMES.items()}
NAME_TO_VK.update({
    "CONTROL": 17, "CTRL": 17, "SHIFT": 16, "ALT": 18,
    "WIN": 91, "WINDOWS": 91, "RETURN": 13, "SPACEBAR": 32, "ESC": 27,
})


def key_name(vk):
    return VK_NAMES.get(vk, f"VK_{vk}")


def send_key(vk, scan, down):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    # 使用虚拟键码，兼容性最好；scan 仅用于记录。
    inp.ki = KEYBDINPUT(
        wVk=int(vk),
        wScan=0,
        dwFlags=0 if down else KEYEVENTF_KEYUP,
        time=0,
        dwExtraInfo=0,
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        return ctypes.get_last_error()
    return 0


def parse_hotkey(text):
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("快捷键不能为空")
    mods = 0
    vk = None
    for p in parts:
        n = p.upper()
        if n in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif n == "SHIFT":
            mods |= MOD_SHIFT
        elif n == "ALT":
            mods |= MOD_ALT
        elif n in ("WIN", "WINDOWS"):
            mods |= MOD_WIN
        else:
            if vk is not None:
                raise ValueError("快捷键只能包含一个主按键")
            vk = NAME_TO_VK.get(n)
            if vk is None:
                raise ValueError(f"无法识别按键：{p}")
    if vk is None:
        raise ValueError("请包含一个主按键，例如 Ctrl+F8")
    return mods | MOD_NOREPEAT, vk


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("键盘宏")
        self.root.geometry("760x610")
        self.root.minsize(700, 560)

        self.profiles = {}
        self.current_profile = ""
        self.events = []  # [{"vk": int, "scan": int, "name": str, "down": bool, "interval": int}]
        self.recording = False
        self.playing = False
        self.stop_event = threading.Event()

        self.hook_thread = None
        self.hook_proc = None
        self.hook_handle = None
        self.hotkey_thread = None

        self.play_hotkey = tk.StringVar(value="F8")
        self.stop_hotkey = tk.StringVar(value="F9")
        self.play_count = tk.StringVar(value="1")
        self.default_interval = tk.IntVar(value=50)
        self.status = tk.StringVar(value="就绪")
        self.profile_var = tk.StringVar(value="")

        self.data_path = os.path.join(
            os.getenv("APPDATA") or os.path.expanduser("~"),
            "KeyboardMacro",
            "profiles.json",
        )

        self._build_ui()
        self._load_local()
        self._start_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ---------- 数据 ----------
    def _load_local(self):
        try:
            os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
            if os.path.exists(self.data_path):
                with open(self.data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.play_hotkey.set(data.get("play_hotkey", "F8"))
                self.stop_hotkey.set(data.get("stop_hotkey", "F9"))
                self.play_count.set(str(data.get("play_count", "1")))
                self.default_interval.set(int(data.get("default_interval", 50)))
                self.profiles = data.get("profiles", {})
        except Exception:
            self.profiles = {}

        if self.profiles:
            first = next(iter(self.profiles))
            self._select_profile(first)
        else:
            self._select_profile("")

    def _save_local(self):
        try:
            os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
            data = {
                "version": 4,
                "play_hotkey": self.play_hotkey.get().strip(),
                "stop_hotkey": self.stop_hotkey.get().strip(),
                "play_count": self.play_count.get().strip(),
                "default_interval": int(self.default_interval.get()),
                "profiles": self.profiles,
            }
            tmp = self.data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_path)
        except Exception as e:
            self.status.set("保存失败")

    def _sync_current(self):
        if self.current_profile:
            self.profiles[self.current_profile] = {
                "events": self.events,
            }

    def _select_profile(self, name):
        self._sync_current()
        self.current_profile = name or ""
        self.profile_var.set(self.current_profile)
        if self.current_profile and self.current_profile in self.profiles:
            raw = self.profiles[self.current_profile].get("events", [])
            self.events = list(raw)
        else:
            self.events = []
        self.refresh()

    # ---------- UI ----------
    def _build_ui(self):
        root = self.root

        top = ttk.Frame(root)
        top.pack(fill="x", padx=12, pady=(12, 8))

        ttk.Label(
            top, text="键盘宏",
            font=("Microsoft YaHei UI", 18, "bold")
        ).pack(side="left")

        ttk.Label(top, textvariable=self.status).pack(side="right")

        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=12, pady=5)

        self.rec_btn = ttk.Button(
            actions, text="● 开始录制", command=self.toggle_record
        )
        self.rec_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.play_btn = ttk.Button(
            actions, text="▶ 播放", command=self.start_play
        )
        self.play_btn.pack(side="left", fill="x", expand=True, padx=6)

        ttk.Button(
            actions, text="■ 停止", command=self.stop_play
        ).pack(side="left", fill="x", expand=True, padx=6)

        ttk.Button(
            actions, text="清空", command=self.clear
        ).pack(side="left", fill="x", expand=True, padx=6)

        ttk.Button(
            actions, text="设置", command=self.open_settings
        ).pack(side="left", fill="x", expand=True, padx=(6, 0))

        profile_box = ttk.LabelFrame(root, text="快捷键配置")
        profile_box.pack(fill="x", padx=12, pady=8)

        ttk.Label(profile_box, text="当前配置：").pack(side="left", padx=(10, 5), pady=10)

        self.profile_combo = ttk.Combobox(
            profile_box,
            textvariable=self.profile_var,
            state="readonly",
            width=28,
        )
        self.profile_combo.pack(side="left", padx=5, pady=10)
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Button(
            profile_box, text="新建", command=self.new_profile
        ).pack(side="left", padx=4)

        ttk.Button(
            profile_box, text="重命名", command=self.rename_profile
        ).pack(side="left", padx=4)

        ttk.Button(
            profile_box, text="删除", command=self.delete_profile
        ).pack(side="left", padx=4)

        list_box = ttk.LabelFrame(root, text="当前快捷键")
        list_box.pack(fill="both", expand=True, padx=12, pady=8)

        columns = ("no", "action", "key", "interval")
        self.tree = ttk.Treeview(
            list_box, columns=columns, show="headings", height=15
        )
        self.tree.heading("no", text="序号")
        self.tree.heading("action", text="动作")
        self.tree.heading("key", text="按键")
        self.tree.heading("interval", text="间隔(ms)")
        self.tree.column("no", width=60, anchor="center")
        self.tree.column("action", width=100, anchor="center")
        self.tree.column("key", width=250, anchor="center")
        self.tree.column("interval", width=120, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(list_box, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self.edit_interval)

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=12, pady=(0, 12))

        ttk.Label(bottom, text="双击列表项目可修改间隔").pack(side="left")
        ttk.Button(bottom, text="保存当前配置", command=self.save_current).pack(side="right")

        self._refresh_profiles()

    def _refresh_profiles(self):
        names = list(self.profiles.keys())
        self.profile_combo["values"] = names
        if self.current_profile in names:
            self.profile_var.set(self.current_profile)
        elif names:
            self._select_profile(names[0])
        else:
            self.profile_var.set("")

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.events, 1):
            self.tree.insert(
                "", "end", iid=str(i - 1),
                values=(
                    i,
                    "按下" if e.get("down", True) else "松开",
                    e.get("name", ""),
                    e.get("interval", 50),
                )
            )

    # ---------- 配置 ----------
    def new_profile(self):
        name = simpledialog.askstring("新建快捷键", "请输入配置名称：", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.profiles:
            messagebox.showwarning("提示", "这个名称已经存在。", parent=self.root)
            return
        self._sync_current()
        self.profiles[name] = {"events": []}
        self._select_profile(name)
        self._refresh_profiles()
        self._save_local()

    def rename_profile(self):
        if not self.current_profile:
            messagebox.showinfo("提示", "请先选择一个快捷键配置。", parent=self.root)
            return
        name = simpledialog.askstring(
            "重命名", "请输入新的配置名称：",
            initialvalue=self.current_profile, parent=self.root
        )
        if not name:
            return
        name = name.strip()
        if not name or name == self.current_profile:
            return
        if name in self.profiles:
            messagebox.showwarning("提示", "这个名称已经存在。", parent=self.root)
            return
        self._sync_current()
        self.profiles[name] = self.profiles.pop(self.current_profile)
        self.current_profile = name
        self.profile_var.set(name)
        self._refresh_profiles()
        self._save_local()

    def delete_profile(self):
        if not self.current_profile:
            return
        if not messagebox.askyesno(
            "删除配置", f"确定删除“{self.current_profile}”？", parent=self.root
        ):
            return
        self.profiles.pop(self.current_profile, None)
        self.current_profile = ""
        self.events = []
        self.profile_var.set("")
        self._refresh_profiles()
        if self.profiles:
            self._select_profile(next(iter(self.profiles)))
        else:
            self.refresh()
        self._save_local()

    def on_profile_selected(self, _event=None):
        self._select_profile(self.profile_var.get())

    def save_current(self):
        if not self.current_profile:
            self.new_profile()
            return
        self._sync_current()
        self._save_local()
        self.status.set("已保存")

    # ---------- 录制 ----------
    def toggle_record(self):
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        self.stop_play()
        if not self.current_profile:
            self.new_profile()
            if not self.current_profile:
                return
        self.events.clear()
        self.refresh()
        self.recording = True
        self.status.set("● 正在录制")
        self.rec_btn.config(text="■ 停止录制")
        self.hook_thread = threading.Thread(target=self._hook_worker, daemon=True)
        self.hook_thread.start()

    def stop_record(self):
        self.recording = False
        self.rec_btn.config(text="● 开始录制")
        self.status.set(f"录制完成，共 {len(self.events)} 个动作")
        self._sync_current()
        self._save_local()
        if self.hook_thread and self.hook_thread.ident:
            try:
                user32.PostThreadMessageW(self.hook_thread.ident, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _hook_worker(self):
        self.hook_proc = HOOKPROC(self._hook_callback)
        self.hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self.hook_proc,
            kernel32.GetModuleHandleW(None),
            0,
        )
        if not self.hook_handle:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "错误", "无法安装全局键盘钩子，请尝试以管理员身份运行。"
                ),
            )
            self.root.after(0, self.stop_record)
            return

        msg = wintypes.MSG()
        while self.recording:
            result = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if result <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self.hook_handle:
            user32.UnhookWindowsHookEx(self.hook_handle)
            self.hook_handle = None

    def _hook_callback(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.recording:
            data = ctypes.cast(
                lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)
            ).contents

            if data.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(
                    self.hook_handle, nCode, wParam, lParam
                )

            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                interval = max(0, min(int(self.default_interval.get()), 60000))
                event = {
                    "vk": int(data.vkCode),
                    "scan": int(data.scanCode),
                    "name": key_name(int(data.vkCode)),
                    "down": is_down,
                    "interval": interval,
                }
                self.events.append(event)
                self.root.after(0, self.refresh)

        return user32.CallNextHookEx(
            self.hook_handle, nCode, wParam, lParam
        )

    # ---------- 播放 ----------
    def start_play(self):
        if self.recording:
            self.stop_record()
        if not self.events:
            messagebox.showinfo("提示", "当前配置没有录制内容。", parent=self.root)
            return
        if self.playing:
            return

        try:
            count = int(self.play_count.get())
            if count < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "播放次数请输入 0 或正整数。", parent=self.root)
            return

        self.playing = True
        self.stop_event.clear()
        self.status.set("▶ 正在播放")
        self.play_btn.config(state="disabled")
        threading.Thread(
            target=self._play_worker, args=(count,), daemon=True
        ).start()

    def _play_worker(self, count):
        loops = 0
        last_error = 0
        try:
            while self.playing and (count == 0 or loops < count):
                for e in list(self.events):
                    if not self.playing or self.stop_event.is_set():
                        break

                    last_error = send_key(
                        e["vk"], e.get("scan", 0), e.get("down", True)
                    )
                    if last_error:
                        break

                    interval = max(0, int(e.get("interval", 50)))
                    if interval:
                        time.sleep(interval / 1000.0)

                if last_error:
                    break
                loops += 1
        finally:
            self.root.after(0, lambda: self._play_finished(last_error))

    def _play_finished(self, error_code=0):
        self.playing = False
        self.stop_event.set()
        self.play_btn.config(state="normal")
        if error_code:
            self.status.set(f"播放失败：Windows 错误 {error_code}")
        elif self.status.get() != "已停止":
            self.status.set("就绪")

    def stop_play(self):
        if self.playing:
            self.playing = False
            self.stop_event.set()
            self.status.set("已停止")

    # ---------- 间隔 ----------
    def edit_interval(self, _event=None):
        item = self.tree.focus()
        if not item:
            return
        idx = int(item)
        old = int(self.events[idx].get("interval", 50))

        win = tk.Toplevel(self.root)
        win.title("修改间隔")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="该动作之后等待（毫秒）：").pack(
            padx=18, pady=(16, 8)
        )
        var = tk.StringVar(value=str(old))
        ent = ttk.Entry(win, textvariable=var, width=14)
        ent.pack(padx=18)
        ent.focus_set()

        def ok():
            try:
                value = max(0, min(int(var.get()), 60000))
            except ValueError:
                messagebox.showwarning(
                    "提示", "请输入 0～60000 的整数。", parent=win
                )
                return
            self.events[idx]["interval"] = value
            self._sync_current()
            self.refresh()
            self._save_local()
            win.destroy()

        ttk.Button(win, text="确定", command=ok).pack(pady=14)
        win.bind("<Return>", lambda _e: ok())

    # ---------- 清空 ----------
    def clear(self):
        self.stop_play()
        self.events.clear()
        self.refresh()
        self._sync_current()
        self._save_local()
        self.status.set("已清空")

    # ---------- 设置 ----------
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding=18)
        frame.pack()

        ttk.Label(frame, text="播放热键").grid(row=0, column=0, sticky="w", pady=6)
        play_ent = ttk.Entry(frame, textvariable=self.play_hotkey, width=16)
        play_ent.grid(row=0, column=1, padx=(15, 0))

        ttk.Label(frame, text="停止热键").grid(row=1, column=0, sticky="w", pady=6)
        stop_ent = ttk.Entry(frame, textvariable=self.stop_hotkey, width=16)
        stop_ent.grid(row=1, column=1, padx=(15, 0))

        ttk.Label(frame, text="播放次数").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.play_count, width=16).grid(
            row=2, column=1, padx=(15, 0)
        )
        ttk.Label(frame, text="0 = 无限循环").grid(row=2, column=2, padx=8)

        ttk.Label(frame, text="默认间隔").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.default_interval, width=16).grid(
            row=3, column=1, padx=(15, 0)
        )
        ttk.Label(frame, text="ms").grid(row=3, column=2, padx=8)

        def save():
            try:
                v = max(0, min(int(self.default_interval.get()), 60000))
                self.default_interval.set(v)
                parse_hotkey(self.play_hotkey.get().strip())
                parse_hotkey(self.stop_hotkey.get().strip())
                int(self.play_count.get())
            except Exception as e:
                messagebox.showwarning("设置错误", str(e), parent=win)
                return

            self.stop_hotkey_thread()
            self._start_hotkeys()
            self._save_local()
            self.status.set("设置已保存")
            win.destroy()

        ttk.Button(frame, text="保存", command=save).grid(
            row=4, column=0, columnspan=3, pady=(16, 0)
        )
        win.bind("<Return>", lambda _e: save())

    # ---------- 全局热键 ----------
    def _start_hotkeys(self):
        self.hotkey_thread = threading.Thread(
            target=self._hotkey_worker, daemon=True
        )
        self.hotkey_thread.start()

    def stop_hotkey_thread(self):
        if self.hotkey_thread and self.hotkey_thread.ident:
            try:
                user32.PostThreadMessageW(
                    self.hotkey_thread.ident, WM_QUIT, 0, 0
                )
            except Exception:
                pass
        time.sleep(0.08)

    def _hotkey_worker(self):
        try:
            pm, pv = parse_hotkey(self.play_hotkey.get().strip())
            sm, sv = parse_hotkey(self.stop_hotkey.get().strip())
        except Exception:
            return

        ok1 = user32.RegisterHotKey(None, HOTKEY_PLAY, pm, pv)
        ok2 = user32.RegisterHotKey(None, HOTKEY_STOP, sm, sv)

        if not ok1 or not ok2:
            if ok1:
                user32.UnregisterHotKey(None, HOTKEY_PLAY)
            if ok2:
                user32.UnregisterHotKey(None, HOTKEY_STOP)
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

    def close(self):
        self.recording = False
        self.stop_play()
        self._sync_current()
        self._save_local()
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

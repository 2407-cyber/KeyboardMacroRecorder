@echo off
chcp 65001 >nul
py -m pip install --upgrade pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name 键盘宏 keyboard_macro_recorder_v4.py
pause

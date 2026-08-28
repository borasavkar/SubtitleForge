# -*- mode: python ; coding: utf-8 -*-
import datetime
import os
import subprocess
import sys

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

# --- SÜRÜM BİLGİSİ ---
# Sürüm numarasının tek kaynağı surum.py. Buradan okuyoruz; run.py'yi import
# etmek gerekseydi derleme sırasında tkinter/torch da yüklenirdi.
sys.path.insert(0, SPECPATH)
from surum import SURUM

DERLEME_TARIHI = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
try:
    DERLEME_COMMIT = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=SPECPATH,
        stderr=subprocess.DEVNULL, text=True).strip()
except Exception:
    DERLEME_COMMIT = ""          # git yoksa ya da depo değilse sorun değil

# run.py bu modülü import etmeye çalışıyor; yoksa "kaynaktan çalışıyor" diyor.
# Böylece exe'nin hangi tarihte, hangi commit'ten üretildiği arayüzde görünüyor
# ve "elimdeki exe güncel mi?" sorusu sürüm numarasına bakmadan da cevaplanıyor.
with open(os.path.join(SPECPATH, "_derleme_bilgisi.py"), "w", encoding="utf-8") as _f:
    _f.write("# run.spec tarafından derleme anında üretilir; elle düzenlemeyin.\n")
    _f.write(f"DERLEME_TARIHI = {DERLEME_TARIHI!r}\n")
    _f.write(f"DERLEME_COMMIT = {DERLEME_COMMIT!r}\n")

# Windows'ta exe'ye sağ tıklayıp Özellikler > Ayrıntılar'da görünen sürüm kaydı.
# filevers/prodvers 4'lü tam sayı demeti olmak zorunda: "1.1.0" -> (1, 1, 0, 0).
_sayilar = tuple(int(p) for p in SURUM.split("."))
_sayilar = (_sayilar + (0, 0, 0, 0))[:4]
surum_kaydi = None
try:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo)
except ImportError as _hata:
    # Bu modül yalnızca Windows'ta içe aktarılabiliyor (win32api'ye bağlı).
    # Sürüm kaydı da zaten yalnızca Windows exe'sinde anlamlı; derlemeyi
    # durdurmuyoruz ama sessiz de geçmiyoruz.
    print(f"[run.spec] UYARI: exe sürüm kaydı atlandı ({_hata}). "
          f"Windows'ta derlemiyorsanız bu normal.")
else:
    # Buradaki bir hata BİLEREK derlemeyi durduruyor: sessizce sürümsüz bir exe
    # üretmek, sürüm koymanın bütün amacını ortadan kaldırırdı.
    surum_kaydi = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_sayilar, prodvers=_sayilar,
                          mask=0x3F, flags=0x0, OS=0x40004,
                          fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable('040904B0', [
                StringStruct('CompanyName', 'Bora Savkar'),
                StringStruct('FileDescription',
                             'SubtitleForge - WhisperX altyazı üretici ve çevirmen'),
                StringStruct('FileVersion', SURUM),
                StringStruct('InternalName', 'SubtitleForge'),
                StringStruct('OriginalFilename', 'SubtitleForge.exe'),
                StringStruct('ProductName', 'SubtitleForge'),
                StringStruct('ProductVersion', f'{SURUM} ({DERLEME_TARIHI})'),
            ])]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])]),
        ],
    )

print(f"[run.spec] SubtitleForge v{SURUM} · {DERLEME_TARIHI} · {DERLEME_COMMIT or 'commit yok'}")

datas = [('subs.ico', '.')]
binaries = []
hiddenimports = ['torch', 'numpy', 'deep_translator']

# NOT: ffmpeg BİLEREK paketlenmiyor.
# Gömülü olsaydı her ffmpeg güncellemesi için exe'yi yeniden derlemek gerekirdi
# ve pakete ~100-210 MB eklerdi. Bunun yerine program çalışma anında sistemdeki
# ffmpeg'i kullanıyor; bulunamazsa arayüzdeki "FFmpeg Yolu / Gözat" ile elle
# seçiliyor ve seçim settings.json'a yazılıyor.

datas += copy_metadata('torchcodec')
datas += copy_metadata('transformers')
tmp_ret = collect_all('whisperx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyannote.audio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# UPX KAPALI OLMALI -- açmayın.
# UPX, ctranslate2.dll / torch_cpu.dll gibi büyük yerel kütüphaneleri sıkıştırınca
# bozulmalarına ve exe'nin açılışta çökmesine yol açıyor. Şu an makinede UPX kurulu
# olmadığı için "upx=True" sessizce yok sayılıyordu; biri UPX kurduğu gün build
# hiçbir uyarı vermeden bozulurdu. Eski faster-whisper projesinde de --noupx
# kullanılmasının sebebi buydu.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # Uygulamanın adı SubtitleForge; exe'nin giriş betiği run.py diye
    # 'run.exe' olarak çıkıyordu. Klasör adı (COLLECT) 'run' kalıyor.
    name='SubtitleForge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['subs.ico'],
    version=surum_kaydi,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='run',
)

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata


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
    name='run',
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

import os
import sys
import traceback
import threading
import time
import json
import gc
import queue
import shutil
import subprocess
import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

from deep_translator import GoogleTranslator

# Ağır kütüphaneler (whisperx + torch) ilk "BAŞLAT"a kadar yüklenmiyor:
# import'ları modül seviyesinde yapmak pencerenin açılmasını ~10 sn geciktiriyordu.
whisperx = None
torch = None
np = None

# Uygulama dosyasının bulunduğu klasör (CWD'den bağımsız)
_APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)


def _tasinabilir_model_klasoru():
    """Uygulamanın yanında 'models' klasörü varsa modeller oradan okunur.

    Böylece exe'yi model önbelleğiyle birlikte kopyalayıp internetsiz bir
    makinede de çalıştırmak mümkün oluyor. Klasör yoksa hiçbir şey değişmiyor:
    modeller her zamanki gibi %USERPROFILE%\\.cache altına iner.

    Bu ayarların torch/huggingface import edilmeden ÖNCE yapılması şart;
    o kütüphaneler önbellek yolunu import anında okuyor.
    """
    klasor = os.path.join(_APP_DIR, "models")
    if not os.path.isdir(klasor):
        return None
    os.environ["HF_HOME"] = klasor                       # Whisper + wav2vec2 (HF)
    os.environ["TORCH_HOME"] = klasor                    # torchaudio hizalama modelleri
    nltk_klasoru = os.path.join(klasor, "nltk_data")
    if os.path.isdir(nltk_klasoru):
        os.environ["NLTK_DATA"] = nltk_klasoru
    return klasor


_MODEL_KLASORU = _tasinabilir_model_klasoru()

# Pencereli (pythonw / console=False) çalışırken ffmpeg'in siyah konsol
# penceresi açmasını engelleyen bayrak.
_KONSOLSUZ = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --- SİHİRLİ HATA YAKALAYICI ---
def hata_yakalayici(exctype, value, tb):
    hata_mesaji = "".join(traceback.format_exception(exctype, value, tb))
    with open(os.path.join(_APP_DIR, "HATA_RAPORU.txt"), "w", encoding="utf-8") as f:
        f.write(hata_mesaji)
    print("\n" + "=" * 55)
    print("❌ KRİTİK BİR ÇÖKME YAŞANDI! ❌")
    print(hata_mesaji)
    input("\nPencereyi kapatmak için ENTER tuşuna bas...")


sys.excepthook = hata_yakalayici


# --- ÇEVİRİ AYARLARI ---
# Google'ın ücretsiz uç noktası istek başına ~5000 karaktere izin veriyor ve
# deep_translator'ın translate_batch'i aslında satır başına AYRI bir istek atıyor.
# Satırları tek pakette birleştirip gönderiyoruz: ~1200 satırlık bir filmde
# ~1200 istek yerine ~40 istek yapılıyor. Hem çok daha hızlı, hem de
# "429 Too Many Requests" yüzünden satır kaybetme riski neredeyse sıfırlanıyor.
CEVIRI_MAX_KARAKTER = 1200   # tek istekte gönderilecek en fazla karakter
CEVIRI_MAX_SATIR = 40        # tek istekte gönderilecek en fazla satır
CEVIRI_ISCI_SAYISI = 2       # eşzamanlı istek sayısı (Google'ı kızdırmamak için düşük)

# --- HALÜSİNASYON DÖNGÜSÜ KORUMASI ---
# Whisper, uzun ve konuşma içeriği az seslerde (özellikle dil yanlış algılandığında)
# tekrar döngüsüne girip aynı metni yüzlerce kez üretebiliyor. Aynı metin arka arkaya
# bu sayıdan fazla çıkarsa gerisi ayıklanır.
#
# Sınır neden 25: eski (faster-whisper) sürümde ölçülen GERÇEK tekrarların en uzunu 19
# ardışık bloktu ("Ah !", 11 saniyeye yayılmış). Bozulan dosyadaki döngü ise 92 ardışık
# bloktu. 25, gerçek tekrarların hepsinin üstünde ve döngünün çok altında kalıyor.
TEKRAR_LIMITI = 25

# --- DİL TESPİTİ ---
# Whisper dili yalnızca ilk 30 saniyeden tahmin ediyor. Başında müzik/konuşmasız bölüm
# olan videolarda bu, dilin tamamen yanlış seçilmesine yol açıyor -- ölçülen bir örnekte
# ilk pencere fr:%51.9 / it:%37.4 verip orada durmuş, oysa filmin geri kalanı %90-99
# İtalyanca'ydı. Yanlış dil ise halüsinasyon/tekrar döngüsünü tetikliyor.
# Çözüm: filmin geneline yayılmış birkaç pencereye bakıp olasılık ağırlıklı oy vermek.
DIL_PENCERE_SAYISI = 8       # örneklenecek 30 sn'lik pencere sayısı
DIL_GUVEN_ESIGI = 0.75       # bunun altında kullanıcı uyarılır

# --- ALTYAZI BLOK KURALLARI ---
BLOK_MAX_KARAKTER = 75       # bir altyazı bloğunun üst sınırı
BLOK_MIN_KARAKTER = 25       # cümle bitse bile bu uzunluğun altında blok kapatılmaz
KELIME_ARASI_BOSLUK = 1.0    # bu kadar saniyelik sessizlik yeni blok başlatır
SATIR_MAX_KARAKTER = 42      # dosyaya yazarken satır kırma sınırı (altyazı standardı)


class KullaniciIptali(Exception):
    """İptal butonuna basıldığında whisperx'in içinden çıkmak için kullanılıyor.
    whisperx.transcribe/align tek parça bloklayan çağrılar; iptali ancak
    ilerleme geri çağrısından istisna fırlatarak yakalayabiliyoruz."""
    pass


class WhisperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Bora Şavkar - AI Altyazı Stüdyosu v3 (WhisperX / CPU)")
        # Pencere yeniden boyutlandırılabilir: alt sınır, ayar kutularının hepsinin
        # sığdığı yükseklik (bunun altında terminal alanı ezilir).
        self.root.minsize(700, 860)

        # --- ERİŞİLEBİLİR MODERN FLUENT TASARIM ---
        bg_color = "#1e1e1e"        # Koyu antrasit zemin (Göz yormaz)
        fg_color = "#ffffff"        # Tam beyaz okunaklı metin
        input_bg = "#333333"        # Belirgin input arka planı
        accent_color = "#4cc2ff"    # Odaklanma rengi (Açık okunaklı mavi)

        self.root.configure(bg=bg_color)

        style = ttk.Style()
        style.theme_use('clam')

        default_font = ("Segoe UI", 11)
        bold_font = ("Segoe UI", 11, "bold")

        style.configure(".", background=bg_color, foreground=fg_color, font=default_font)
        style.configure("TFrame", background=bg_color)

        style.configure("TLabelframe", background=bg_color, bordercolor="#555555", borderwidth=2)
        style.configure("TLabelframe.Label", background=bg_color, foreground="#4cc2ff", font=("Segoe UI", 12, "bold"))

        style.configure("TLabel", background=bg_color, foreground=fg_color, font=default_font)
        style.configure("TCheckbutton", background=bg_color, foreground=fg_color, font=bold_font)
        style.map("TCheckbutton", background=[("active", bg_color)], foreground=[("active", accent_color)])

        style.configure("TCombobox", fieldbackground=input_bg, background="#444444", foreground="white", arrowcolor="white", bordercolor="#555555", padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", input_bg)], selectbackground=[("readonly", accent_color)], selectforeground=[("readonly", "black")])

        style.configure("TEntry", fieldbackground=input_bg, foreground="white", bordercolor="#555555", insertcolor="white", padding=5)

        style.configure("TButton", background="#333333", foreground="white", font=default_font, bordercolor="#555555", padding=5)
        style.map("TButton", background=[("active", "#555555")])

        style.configure("Islem.Horizontal.TProgressbar", troughcolor="#333333", bordercolor="#555555",
                        background=accent_color, lightcolor=accent_color, darkcolor=accent_color)

        self.root.option_add('*TCombobox*Listbox.background', input_bg)
        self.root.option_add('*TCombobox*Listbox.foreground', 'white')
        self.root.option_add('*TCombobox*Listbox.selectBackground', accent_color)
        self.root.option_add('*TCombobox*Listbox.selectForeground', 'black')
        self.root.option_add('*TCombobox*Listbox.font', default_font)

        # --- İPTAL SİNYALİ VE DEĞİŞKENLER ---
        self.is_cancelled = False
        self.pipeline = None            # whisperx FasterWhisperPipeline
        self.yuklu_model_anahtari = ""  # (model, hassasiyet, çekirdek) üçlüsü
        self.align_model = None
        self.align_meta = None
        self.yuklu_align_dili = ""

        # Log mesajları kuyrukta toplanır, ~100ms'de bir toplu basılır (UI'ı boğmamak için)
        self._log_queue = queue.Queue()

        # --- KAYITLI AYARLAR ---
        self.ayar_dosyasi = os.path.join(_APP_DIR, "settings.json")
        ayarlar = {}
        try:
            with open(self.ayar_dosyasi, "r", encoding="utf-8") as f:
                ayarlar = json.load(f)
        except Exception:
            ayarlar = {}

        x = ayarlar.get("x", 100)
        y = ayarlar.get("y", 100)
        self.root.geometry(f"700x1025+{x}+{y}")

        self.root.report_callback_exception = self.tk_hata_yakalayici
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        try:
            self.root.iconbitmap(self.resource_path("subs.ico"))
        except Exception:
            pass

        self.lang_map = {
            "Otomatik Algıla (Auto)": "auto",
            "Türkçe (tr)": "tr",
            "İngilizce (en)": "en",
            "Fransızca (fr)": "fr",
            "Almanca (de)": "de",
            "İtalyanca (it)": "it",
            "Rusça (ru)": "ru",
            "İspanyolca (es)": "es"
        }

        # CPU'da mantıklı varsayılan: fiziksel çekirdek sayısı (SMT iş parçacıkları
        # ctranslate2'de fayda getirmiyor, aksine birbirini yavaşlatıyor).
        mantiksal = os.cpu_count() or 4
        varsayilan_cekirdek = max(1, mantiksal // 2)

        self.video_path = tk.StringVar(value="")
        self.model_size = tk.StringVar(value=ayarlar.get("model", "large-v3-turbo"))
        self.source_lang = tk.StringVar(value=ayarlar.get("dil", "Otomatik Algıla (Auto)"))
        self.task_type = tk.StringVar(value=ayarlar.get("islem", "Aynı Dilde Yaz (Transcribe)"))
        self.subtitle_style = tk.StringVar(value=ayarlar.get("stil", "Cümle Cümle (Klasik Youtube/Web - Max 2 Satır)"))
        self.compute_type = tk.StringVar(value=ayarlar.get("hassasiyet", "int8 (En Hızlı - Önerilen)"))
        self.batch_mode = tk.StringVar(value=ayarlar.get("hiz", "Dengeli (Batch 8 - Önerilen)"))
        self.vad_mode = tk.StringVar(value=ayarlar.get("vad", "Normal (Önerilen)"))
        self.cpu_threads = tk.StringVar(value=str(ayarlar.get("cekirdek", varsayilan_cekirdek)))
        self.do_align = tk.BooleanVar(value=ayarlar.get("hizalama", True))
        self.auto_translate_tr = tk.BooleanVar(value=ayarlar.get("turkce", True))

        # Boş bırakılırsa sistemdeki ffmpeg (PATH) kullanılır. Elle bir yol
        # seçilirse o kullanılır. ffmpeg'i uygulamayla paketlemiyoruz: her ffmpeg
        # güncellemesinde exe'yi yeniden derlemek gerekirdi.
        self.ffmpeg_path = tk.StringVar(value=ayarlar.get("ffmpeg", ""))

        self.compute_map = {
            "int8 (En Hızlı - Önerilen)": "int8",
            "int8_float32 (Dengeli)": "int8_float32",
            "float32 (En Yüksek Kalite - Çok Yavaş)": "float32",
        }
        self.batch_map = {
            "Düşük RAM (Batch 4)": 4,
            "Dengeli (Batch 8 - Önerilen)": 8,
            "Hızlı (Batch 16 - Bol RAM)": 16,
        }
        # WhisperX'te sessizlik filtresi kapatılamıyor: ses zaten VAD'in bulduğu
        # konuşma parçalarına bölünerek modele veriliyor. Ayarlanan şey filtrenin
        # ne kadar seçici olduğu (onset = konuşma sayılma eşiği).
        self.vad_map = {
            "Hassas (Fısıltıları da yakala)": (0.35, 0.25),
            "Normal (Önerilen)": (0.500, 0.363),
            "Agresif (Sadece net konuşma)": (0.65, 0.50),
        }

        # --- ARAYÜZ ELEMANLARI ---
        frame_file = ttk.LabelFrame(root, text="1. Video Dosyası Seçimi", padding=15)
        frame_file.pack(fill="x", padx=15, pady=10)

        self.entry_path = ttk.Entry(frame_file, textvariable=self.video_path, width=52)
        self.entry_path.pack(side="left", padx=(0, 10))
        ttk.Button(frame_file, text="Gözat 📂", command=self.select_file).pack(side="left")

        frame_settings = ttk.LabelFrame(root, text="2. İşlem Ayarları", padding=15)
        frame_settings.pack(fill="x", padx=15, pady=5)

        ttk.Label(frame_settings, text="Model Gücü:").grid(row=0, column=0, sticky="w", pady=6)
        combo_model = ttk.Combobox(frame_settings, textvariable=self.model_size, state="readonly", width=38)
        combo_model['values'] = ("small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3")
        combo_model.grid(row=0, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="Videonun Dili:").grid(row=1, column=0, sticky="w", pady=6)
        combo_lang = ttk.Combobox(frame_settings, textvariable=self.source_lang, state="readonly", width=38)
        combo_lang['values'] = list(self.lang_map.keys())
        combo_lang.grid(row=1, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="İşlem Türü:").grid(row=2, column=0, sticky="w", pady=6)
        combo_task = ttk.Combobox(frame_settings, textvariable=self.task_type, state="readonly", width=38)
        combo_task['values'] = ("Aynı Dilde Yaz (Transcribe)", "İngilizceye Çevir (Translate)")
        combo_task.grid(row=2, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="Hesaplama Hassasiyeti:").grid(row=3, column=0, sticky="w", pady=6)
        combo_compute = ttk.Combobox(frame_settings, textvariable=self.compute_type, state="readonly", width=38)
        combo_compute['values'] = list(self.compute_map.keys())
        combo_compute.grid(row=3, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="İşlem Hızı (Toplu İşleme):").grid(row=4, column=0, sticky="w", pady=6)
        combo_batch = ttk.Combobox(frame_settings, textvariable=self.batch_mode, state="readonly", width=38)
        combo_batch['values'] = list(self.batch_map.keys())
        combo_batch.grid(row=4, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="CPU Çekirdek Sayısı:").grid(row=5, column=0, sticky="w", pady=6)
        combo_threads = ttk.Combobox(frame_settings, textvariable=self.cpu_threads, state="readonly", width=38)
        combo_threads['values'] = tuple(str(i) for i in range(1, mantiksal + 1))
        combo_threads.grid(row=5, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="Sessizlik Filtresi:").grid(row=6, column=0, sticky="w", pady=6)
        combo_vad = ttk.Combobox(frame_settings, textvariable=self.vad_mode, state="readonly", width=38)
        combo_vad['values'] = list(self.vad_map.keys())
        combo_vad.grid(row=6, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="Altyazı Stili:").grid(row=7, column=0, sticky="w", pady=6)
        combo_style = ttk.Combobox(frame_settings, textvariable=self.subtitle_style, state="readonly", width=38)
        combo_style['values'] = ("Cümle Cümle (Klasik Youtube/Web - Max 2 Satır)", "Kelime Kelime (Reels/Shorts Tarzı)")
        combo_style.grid(row=7, column=1, padx=10, pady=6)

        ttk.Label(frame_settings, text="FFmpeg Yolu:").grid(row=8, column=0, sticky="w", pady=6)
        frame_ffmpeg = tk.Frame(frame_settings, bg=bg_color)
        frame_ffmpeg.grid(row=8, column=1, padx=10, pady=6, sticky="w")
        self.entry_ffmpeg = ttk.Entry(frame_ffmpeg, textvariable=self.ffmpeg_path, width=26)
        self.entry_ffmpeg.pack(side="left", padx=(0, 5))
        ttk.Button(frame_ffmpeg, text="Gözat 📂", width=9,
                   command=self.select_ffmpeg).pack(side="left", padx=(0, 4))
        ttk.Button(frame_ffmpeg, text="Otomatik", width=9,
                   command=self.ffmpeg_otomatik).pack(side="left")

        self.lbl_ffmpeg = tk.Label(frame_settings, text="FFmpeg aranıyor...", justify="left",
                                   bg=bg_color, fg="#aaaaaa", font=("Segoe UI", 9), anchor="w")
        self.lbl_ffmpeg.grid(row=9, column=0, columnspan=2, sticky="w", padx=5)

        ttk.Checkbutton(frame_settings, text=" 🎯 Kelime Zamanlaması (WhisperX Hizalama - Önerilen)",
                        variable=self.do_align).grid(row=10, column=0, columnspan=2, sticky="w", padx=5, pady=(12, 2))

        ttk.Checkbutton(frame_settings, text=" 🇹🇷 İşlem Bitince Ekstra Türkçe (.srt) Dosyası Üret",
                        variable=self.auto_translate_tr).grid(row=11, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 10))

        tk.Label(frame_settings,
                 text="💡 İPUCU: Ekran kartı kullanılmıyor, tüm iş işlemcide dönüyor. Filmler için en dengeli ayar\n"
                      "     large-v3-turbo + int8'dir. Hizalama kapatılırsa hız artar, zamanlama kabalaşır.",
                 justify="left", bg=bg_color, fg="#aaaaaa", font=("Segoe UI", 9, "italic")
                 ).grid(row=12, column=0, columnspan=2, sticky="w", padx=5)

        # --- YÜKSEK KONTRASTLI ANA BUTONLAR ---
        frame_buttons = tk.Frame(root, bg=bg_color)
        frame_buttons.pack(fill="x", padx=15, pady=10)

        self.btn_start = tk.Button(frame_buttons, text="🚀 İŞLEMİ BAŞLAT", bg="#0078D4", fg="white",
                                   font=("Segoe UI", 13, "bold"), relief="flat", activebackground="#005A9E",
                                   activeforeground="white", cursor="hand2", command=self.start_thread)
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=8)

        self.btn_cancel = tk.Button(frame_buttons, text="🛑 İPTAL ET", bg="#D13438", fg="white",
                                    font=("Segoe UI", 13, "bold"), relief="flat", activebackground="#A80000",
                                    activeforeground="white", cursor="hand2", state="disabled", command=self.cancel_process)
        self.btn_cancel.pack(side="right", fill="x", expand=True, padx=(8, 0), ipady=8)

        # --- İLERLEME ÇUBUĞU ---
        # WhisperX tek parça çalıştığı için eski sürümdeki "satır satır akan" geri
        # bildirim kayboluyor; onun yerine yüzde + kalan süre gösteriliyor.
        frame_prog = tk.Frame(root, bg=bg_color)
        frame_prog.pack(fill="x", padx=15, pady=(0, 5))

        self.progress_bar = ttk.Progressbar(frame_prog, style="Islem.Horizontal.TProgressbar",
                                            orient="horizontal", mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x")
        self.lbl_progress = tk.Label(frame_prog, text="Hazır.", bg=bg_color, fg="#aaaaaa",
                                     font=("Segoe UI", 9), anchor="w")
        self.lbl_progress.pack(fill="x", pady=(3, 0))

        # --- DAHA OKUNAKLI TERMİNAL EKRANI ---
        frame_log = ttk.LabelFrame(root, text="İşlem Durumu (Terminal)", padding=10)
        frame_log.pack(fill="both", expand=True, padx=15, pady=(5, 15))

        self.txt_log = scrolledtext.ScrolledText(frame_log, height=7, state='disabled', font=("Consolas", 10),
                                                 bg="#000000", fg="#E0E0E0", relief="flat", padx=10, pady=10)
        self.txt_log.pack(fill="both", expand=True)

        self.root.after(100, self._flush_logs)

        # FFmpeg durumunu mainloop basladiktan SONRA kontrol et.
        # __init__ icinde thread baslatmak yaris yaratiyor: thread root.after'i
        # mainloop henuz calismadan cagirirsa "main thread is not in main loop"
        # ile sessizce oluyor ve etiket "araniyor..." kaliyordu.
        self.root.after(200, self._ffmpeg_durumu_guncelle)

        if _MODEL_KLASORU:
            self.log(f"📦 Modeller program klasöründen okunuyor: {os.path.basename(_MODEL_KLASORU)}")

    # ------------------------------------------------------------------
    # ARAYÜZ YARDIMCILARI
    # ------------------------------------------------------------------

    def tk_hata_yakalayici(self, exc, val, tb):
        hata_mesaji = "".join(traceback.format_exception(exc, val, tb))
        with open(os.path.join(_APP_DIR, "HATA_RAPORU_UI.txt"), "w", encoding="utf-8") as f:
            f.write(hata_mesaji)
        self.log("❌ ARAYÜZ HATASI: HATA_RAPORU_UI.txt dosyasına bakınız.")

    def resource_path(self, relative_path):
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = _APP_DIR
        return os.path.join(base_path, relative_path)

    def on_closing(self):
        try:
            with open(self.ayar_dosyasi, "w", encoding="utf-8") as f:
                json.dump({
                    "x": self.root.winfo_x(),
                    "y": self.root.winfo_y(),
                    "model": self.model_size.get(),
                    "dil": self.source_lang.get(),
                    "islem": self.task_type.get(),
                    "stil": self.subtitle_style.get(),
                    "hassasiyet": self.compute_type.get(),
                    "hiz": self.batch_mode.get(),
                    "vad": self.vad_mode.get(),
                    "cekirdek": int(self.cpu_threads.get()),
                    "ffmpeg": self.ffmpeg_path.get(),
                    "hizalama": self.do_align.get(),
                    "turkce": self.auto_translate_tr.get(),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        self.root.destroy()
        os._exit(0)

    def select_file(self):
        try:
            file_path = filedialog.askopenfilename(
                filetypes=[("Medya Dosyaları", "*.mp4;*.mkv;*.avi;*.mov;*.mp3;*.wav;*.flac;*.ts;*.m4a;*.webm")])
            if file_path:
                self.video_path.set(file_path)
                self.log(f"Dosya seçildi: {os.path.basename(file_path)}")
        except Exception as e:
            self.log(f"❌ Dosya Seçme Hatası: {e}")

    # ------------------------------------------------------------------
    # FFMPEG
    # ------------------------------------------------------------------

    def _ffmpeg_coz(self, elle=None):
        """Kullanılacak ffmpeg'i belirler: elle seçilen yol varsa o, yoksa PATH.

        ffmpeg uygulamayla PAKETLENMİYOR. Sebebi: ffmpeg sık güncelleniyor ve
        gömülü olsaydı her güncelleme için exe'yi yeniden derlemek gerekirdi.
        Kullanıcı kendi kurulumunu kullanır, istisnai durumda elle yol seçer.

        'elle' verilmezse Tk değişkeninden okunur. Arka plan thread'lerinden
        çağrılırken değer DIŞARIDAN verilmeli: Tk değişkenleri yalnızca ana
        thread'den güvenle okunabiliyor (mainloop başlamadan okumak
        "main thread is not in main loop" hatası veriyor).
        """
        if elle is None:
            elle = self.ffmpeg_path.get() or ""
        elle = elle.strip().strip('"').strip()
        if elle:
            if os.path.isfile(elle):
                return elle
            return None                      # elle verilmiş ama yol geçersiz
        return shutil.which("ffmpeg")        # sistemdeki (PATH)

    def _ffmpeg_surumu(self, yol):
        """ffmpeg'in sürüm satırını döner; çalıştırılamıyorsa None."""
        try:
            sonuc = subprocess.run([yol, "-version"], capture_output=True, timeout=10,
                                   creationflags=_KONSOLSUZ)
            if sonuc.returncode != 0:
                return None
            ilk = sonuc.stdout.decode(errors="replace").splitlines()[0]
            return ilk.strip()
        except Exception:
            return None

    def _ffmpeg_durumu_guncelle(self):
        """Durum etiketini günceller. Sürüm sorgusu ayrı bir thread'de yapılıyor:
        yol ağ sürücüsündeyse veya dosya bozuksa pencere donmasın."""
        # Tk değişkeni BURADA, ana thread'de okunuyor; thread'e düz metin gidiyor.
        elle = (self.ffmpeg_path.get() or "").strip()

        def _kontrol():
            yol = self._ffmpeg_coz(elle)
            if not yol:
                if elle:
                    metin, renk = f"❌ Seçilen yol bulunamadı: {elle}", "#ff6b6b"
                else:
                    metin, renk = ("❌ FFmpeg sistemde bulunamadı — 'Gözat' ile seçin "
                                   "ya da kurun (winget install Gyan.FFmpeg)"), "#ff6b6b"
            else:
                surum = self._ffmpeg_surumu(yol)
                if surum is None:
                    metin, renk = f"❌ FFmpeg çalıştırılamadı: {yol}", "#ff6b6b"
                else:
                    kisa = surum.replace("ffmpeg version ", "").split(" Copyright")[0]
                    nereden = "elle seçildi" if elle else "sistemden"
                    metin, renk = f"✅ FFmpeg {kisa}  ({nereden})", "#7ddc7d"
            try:
                self.root.after(0, lambda: self.lbl_ffmpeg.config(text=metin, fg=renk))
            except RuntimeError:
                pass          # pencere kapanmis olabilir

        threading.Thread(target=_kontrol, daemon=True).start()

    def select_ffmpeg(self):
        try:
            yol = filedialog.askopenfilename(
                title="ffmpeg.exe dosyasını seçin",
                filetypes=[("ffmpeg", "ffmpeg.exe"), ("Tüm dosyalar", "*.*")])
            if not yol:
                return
            if self._ffmpeg_surumu(yol) is None:
                messagebox.showerror("Geçersiz FFmpeg",
                                     "Seçilen dosya çalıştırılamadı ya da ffmpeg değil.\n\n"
                                     "ffmpeg.exe dosyasını seçtiğinizden emin olun "
                                     "(ffplay.exe / ffprobe.exe değil).")
                return
            self.ffmpeg_path.set(yol)
            self.log(f"🎬 FFmpeg yolu ayarlandı: {yol}")
            self._ffmpeg_durumu_guncelle()
        except Exception as e:
            self.log(f"❌ FFmpeg seçme hatası: {e}")

    def ffmpeg_otomatik(self):
        """Elle seçilen yolu temizler; tekrar sistemdeki ffmpeg kullanılır."""
        self.ffmpeg_path.set("")
        self.log("🎬 FFmpeg yolu temizlendi, sistemdeki kurulum kullanılacak.")
        self._ffmpeg_durumu_guncelle()

    def _sesi_coz(self, video_file, ffmpeg_yolu):
        """Videodan 16 kHz mono float32 ses çıkarır (whisperx.load_audio ile aynı).

        whisperx'in kendi load_audio'su komutu düz "ffmpeg" olarak çağırıyor,
        yani yalnızca PATH'e bakıyor -- elle seçilen yolu kullanamıyordu.
        Ayrıca burada konsol penceresi de bastırılıyor.
        """
        cmd = [ffmpeg_yolu, "-nostdin", "-threads", "0", "-i", video_file,
               "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", "16000", "-"]
        sonuc = subprocess.run(cmd, capture_output=True, creationflags=_KONSOLSUZ)
        if sonuc.returncode != 0:
            hata = sonuc.stderr.decode(errors="replace").strip().splitlines()
            son = "\n".join(hata[-5:]) if hata else "(çıktı yok)"
            raise RuntimeError(f"FFmpeg sesi çözemedi:\n{son}")
        return np.frombuffer(sonuc.stdout, np.int16).flatten().astype(np.float32) / 32768.0

    def log(self, message):
        # Worker thread'den güvenle çağrılır: sadece kuyruğa atar, UI'a dokunmaz.
        self._log_queue.put(message)

    def _flush_logs(self):
        # Ana thread'de periyodik çalışır: biriken tüm satırları tek seferde basar.
        lines = []
        try:
            while True:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass

        if lines:
            self.txt_log.config(state='normal')
            self.txt_log.insert(tk.END, "\n".join(lines) + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state='disabled')

        self.root.after(100, self._flush_logs)

    def ilerleme(self, yuzde, etiket):
        """Worker thread'den çağrılır; çubuğu ve altındaki yazıyı günceller."""
        def _uygula():
            self.progress_bar['value'] = max(0, min(100, yuzde))
            self.lbl_progress.config(text=etiket)
        self.root.after(0, _uygula)

    def cancel_process(self):
        if self.btn_start['state'] == 'disabled':
            self.is_cancelled = True
            self.btn_cancel.config(state="disabled", text="⏳ Durduruluyor...", bg="#7A7A7A")
            self.log("\n⚠️ İPTAL SİNYALİ GÖNDERİLDİ! Mevcut işlem güvenlice sonlandırılıyor, lütfen bekleyin...\n")

    def start_thread(self):
        if not self.video_path.get():
            self.log("⚠️ HATA: Lütfen önce bir video dosyası seçin!")
            return

        self.is_cancelled = False
        self.btn_start.config(state="disabled", text="⏳ İşleniyor...", bg="#555555", fg="white")
        self.btn_cancel.config(state="normal", text="🛑 İPTAL ET", bg="#D13438")
        threading.Thread(target=self.run_process, daemon=True).start()

    # ------------------------------------------------------------------
    # ZAMAN / METİN YARDIMCILARI
    # ------------------------------------------------------------------

    def format_timestamp(self, seconds):
        seconds = max(0.0, float(seconds))
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = seconds % 60
        milliseconds = int(round((seconds - int(seconds)) * 1000))
        if milliseconds == 1000:      # 12.9999 -> 13.000 (yuvarlama taşması)
            seconds = int(seconds) + 1
            milliseconds = 0
        return f"{hours:02d}:{minutes:02d}:{int(seconds):02d},{milliseconds:03d}"

    def _sure_metni(self, saniye):
        saniye = int(max(0, saniye))
        return f"{saniye // 60}dk {saniye % 60}sn"

    def _sayi(self, deger):
        """None / NaN gelen zaman damgalarını tek yerde eliyor.
        whisperx, hizalanamayan kelimeler için 'start'/'end' anahtarını hiç
        koymuyor ya da NaN bırakabiliyor."""
        if deger is None:
            return None
        try:
            deger = float(deger)
        except (TypeError, ValueError):
            return None
        return None if deger != deger else deger   # NaN kontrolü

    def _satir_kir(self, metin):
        """Uzun bir altyazı satırını en fazla 2 satıra böler.
        Bölme yalnızca dosyaya yazarken yapılıyor: metin bellekte tek satır kalırsa
        Google'a paket halinde gönderilen çeviri hizalaması bozulmuyor."""
        metin = " ".join(metin.split())
        if len(metin) <= SATIR_MAX_KARAKTER:
            return metin

        kelimeler = metin.split(" ")
        if len(kelimeler) < 2:
            return metin

        # Ortaya en yakın boşluktan böl: iki satır da mümkün olduğunca eşit olsun.
        en_iyi_i, en_iyi_fark = 1, None
        uzunluk = 0
        for i in range(len(kelimeler) - 1):
            uzunluk += len(kelimeler[i]) + 1
            fark = abs(uzunluk - (len(metin) - uzunluk))
            if en_iyi_fark is None or fark < en_iyi_fark:
                en_iyi_fark, en_iyi_i = fark, i + 1

        return " ".join(kelimeler[:en_iyi_i]) + "\n" + " ".join(kelimeler[en_iyi_i:])

    # ------------------------------------------------------------------
    # DİL TESPİTİ
    # ------------------------------------------------------------------

    def _dil_oyla(self, ses):
        """Filmin geneline yayılmış 30 sn'lik pencerelerden olasılık ağırlıklı oy vererek
        dili belirler. Whisper'ın "ilk 30 saniyeye bak" davranışının aksine tek bir
        kararsız pencere tüm dosyanın kaderini belirlemez.

        (dil, guven, oylar) döner. Başarısızlıkta (None, 0.0, {}) -- bu durumda çağıran
        taraf whisperx'in kendi tespitine geri düşer."""
        try:
            from faster_whisper.vad import collect_chunks, get_speech_timestamps, VadOptions

            # Dili konuşmadan tespit etmek gerekiyor; sessizlik/müzik penceresi çöp oy üretir.
            konusma = ses
            try:
                zaman_damgalari = get_speech_timestamps(ses, VadOptions())
                parcalar, _ = collect_chunks(ses, zaman_damgalari)
                if parcalar:
                    birlesik = np.concatenate(parcalar, axis=0)
                    if len(birlesik) >= 16000 * 30:
                        konusma = birlesik
            except Exception:
                pass  # VAD başarısız olursa ham sesle devam et

            pencere_boyu = 16000 * 30
            toplam_pencere = len(konusma) // pencere_boyu
            if toplam_pencere < 1:
                return None, 0.0, {}

            adet = min(DIL_PENCERE_SAYISI, toplam_pencere)
            indeksler = sorted({i * toplam_pencere // adet for i in range(adet)})

            oylar = {}
            olasiliklar = {}
            for sira, i in enumerate(indeksler, 1):
                if self.is_cancelled:
                    raise KullaniciIptali()
                self.ilerleme(sira * 100 / len(indeksler), f"Dil tespiti: {sira}/{len(indeksler)} pencere")
                dilim = konusma[i * pencere_boyu:(i + 1) * pencere_boyu]
                if len(dilim) < 16000:
                    continue
                dil, olasilik, _ = self.pipeline.model.detect_language(audio=dilim)
                oylar[dil] = oylar.get(dil, 0.0) + olasilik
                olasiliklar.setdefault(dil, []).append(olasilik)

            if not oylar:
                return None, 0.0, {}

            kazanan = max(oylar, key=oylar.get)
            guven = sum(olasiliklar[kazanan]) / len(olasiliklar[kazanan])
            return kazanan, guven, oylar

        except KullaniciIptali:
            raise
        except Exception as e:
            self.log(f"⚠️ Oylamalı dil tespiti yapılamadı ({e}). Standart tespite geçiliyor.")
            return None, 0.0, {}

    # ------------------------------------------------------------------
    # BLOK KURMA
    # ------------------------------------------------------------------

    def _kelimeleri_topla(self, segment):
        """Hizalanmış bir segmentteki kelimeleri {text,start,end} listesine çevirir.
        whisperx bazı kelimelere zaman damgası koyamıyor (sözlükte olmayan karakter,
        çok kısa ses vb.); bunlar komşularının arasına eşit aralıklarla yerleştiriliyor.
        Eskiden zamansız kelimeler komple düşüyor, altyazıda kelime kaybı oluyordu."""
        kelimeler = []
        for w in (segment.get("words") or []):
            metin = (w.get("word") or "").strip()
            if not metin:
                continue
            kelimeler.append({"text": metin,
                              "start": self._sayi(w.get("start")),
                              "end": self._sayi(w.get("end"))})
        if not kelimeler:
            return []

        seg_bas = self._sayi(segment.get("start")) or 0.0
        seg_son = self._sayi(segment.get("end"))
        if seg_son is None or seg_son <= seg_bas:
            seg_son = seg_bas + 0.5 * len(kelimeler)

        # Uçları sabitle ki aradaki boşluklar her zaman iki bilinen nokta arasında kalsın.
        if kelimeler[0]["start"] is None:
            kelimeler[0]["start"] = seg_bas
        if kelimeler[-1]["end"] is None:
            kelimeler[-1]["end"] = seg_son

        # Eksik zamanları, bilinen iki damga arasına eşit dağıtarak doldur.
        i = 0
        while i < len(kelimeler):
            if kelimeler[i]["start"] is not None and kelimeler[i]["end"] is not None:
                i += 1
                continue

            bas_i = i
            while i < len(kelimeler) and (kelimeler[i]["start"] is None or kelimeler[i]["end"] is None):
                i += 1
            son_i = i - 1

            onceki = kelimeler[bas_i - 1]["end"] if bas_i > 0 else seg_bas
            sonraki = kelimeler[i]["start"] if i < len(kelimeler) else seg_son
            if onceki is None:
                onceki = seg_bas
            if sonraki is None or sonraki < onceki:
                sonraki = max(onceki, seg_son)

            adet = son_i - bas_i + 1
            adim = (sonraki - onceki) / adet if adet else 0.0
            for k in range(adet):
                kelime = kelimeler[bas_i + k]
                if kelime["start"] is None:
                    kelime["start"] = onceki + adim * k
                if kelime["end"] is None:
                    kelime["end"] = onceki + adim * (k + 1)

        # Sırayı ve pozitif süreyi garantile
        onceki_son = None
        for kelime in kelimeler:
            if onceki_son is not None and kelime["start"] < onceki_son:
                kelime["start"] = onceki_son
            if kelime["end"] < kelime["start"]:
                kelime["end"] = kelime["start"] + 0.05
            onceki_son = kelime["end"]

        return kelimeler

    def _sozde_kelimeler(self, segment):
        """Hizalama yapılmadığında kullanılır: segment metnini kelimelere bölüp süreyi
        karakter sayısına orantılı dağıtır. Zamanlar tahminidir, ama blok kurucu
        (ve dolayısıyla altyazı biçimi) hizalamalı yolla birebir aynı kalır."""
        metin = (segment.get("text") or "").strip()
        if not metin:
            return []

        parcalar = metin.split()
        bas = self._sayi(segment.get("start")) or 0.0
        son = self._sayi(segment.get("end"))
        if son is None or son <= bas:
            son = bas + 0.35 * len(parcalar)

        toplam_karakter = sum(len(p) + 1 for p in parcalar)
        sure = son - bas
        kelimeler = []
        imlec = bas
        for p in parcalar:
            pay = sure * (len(p) + 1) / toplam_karakter
            kelimeler.append({"text": p, "start": imlec, "end": imlec + pay})
            imlec += pay
        kelimeler[-1]["end"] = son
        return kelimeler

    def _bloklari_kur(self, kelimeler, word_level):
        """Kelime listesinden SRT bloklarını üretir.
        Kurallar (eski sürümden birebir): 75 karakteri geçince, cümle bitip 25 karakteri
        aşınca ya da kelimeler arasında 1 saniyeden uzun sessizlik olunca blok kapanır."""
        bloklar = []
        if not kelimeler:
            return bloklar

        if word_level:
            for kelime in kelimeler:
                bloklar.append({"start": kelime["start"], "end": kelime["end"], "text": kelime["text"]})
            return bloklar

        mevcut = []
        blok_bas = None
        blok_son = None
        karakter = 0
        onceki_son = None

        for kelime in kelimeler:
            metin = kelime["text"]

            if onceki_son is not None and (kelime["start"] - onceki_son) > KELIME_ARASI_BOSLUK and mevcut:
                bloklar.append({"start": blok_bas, "end": onceki_son, "text": " ".join(mevcut)})
                mevcut, blok_bas, karakter = [], None, 0

            if blok_bas is None:
                blok_bas = kelime["start"]
            mevcut.append(metin)
            blok_son = kelime["end"]
            onceki_son = kelime["end"]
            karakter += len(metin) + 1

            cumle_sonu = metin.endswith(('.', '?', '!', '…', '؟'))
            if karakter >= BLOK_MAX_KARAKTER or (cumle_sonu and karakter > BLOK_MIN_KARAKTER):
                bloklar.append({"start": blok_bas, "end": blok_son, "text": " ".join(mevcut)})
                mevcut, blok_bas, karakter, onceki_son = [], None, 0, None

        if mevcut:
            bloklar.append({"start": blok_bas, "end": blok_son, "text": " ".join(mevcut)})

        return bloklar

    def _tekrar_filtrele(self, bloklar):
        """Arka arkaya TEKRAR_LIMITI'nden fazla tekrar eden blokları ayıklar.
        (temiz_bloklar, atilan_sayisi, en_uzun_tekrar_dizisi) döner."""
        temiz = []
        onceki_anahtar = None
        ardisik = 0
        atilan = 0
        en_uzun = 0

        for blok in bloklar:
            # Boşluk ve büyük/küçük harf farkları tekrar sayılmasını engellemesin
            anahtar = " ".join(blok["text"].split()).casefold()

            if anahtar == onceki_anahtar:
                ardisik += 1
            else:
                onceki_anahtar = anahtar
                ardisik = 1

            en_uzun = max(en_uzun, ardisik)

            if ardisik <= TEKRAR_LIMITI:
                temiz.append(blok)
            else:
                atilan += 1

        return temiz, atilan, en_uzun

    def _srt_yaz(self, yol, bloklar, metinler=None):
        with open(yol, "w", encoding="utf-8") as f:
            for i, blok in enumerate(bloklar):
                metin = metinler.get(i, blok["text"]) if metinler is not None else blok["text"]
                bas = self.format_timestamp(blok["start"])
                son = self.format_timestamp(blok["end"])
                f.write(f"{i + 1}\n{bas} --> {son}\n{self._satir_kir(metin)}\n\n")

    # ------------------------------------------------------------------
    # ÇEVİRİ YARDIMCILARI
    # ------------------------------------------------------------------

    def _bekleme_suresi(self, deneme):
        # Üstel geri çekilme: 1.5 → 3 → 6 → 12 sn.
        # Google 429 verdiğinde limit birkaç saniye sürüyor; sabit bekleme
        # (ya da hiç beklememe) yüzünden arka arkaya satırlar düşüyordu.
        return min(1.5 * (2 ** deneme), 12)

    def _satir_gruplari(self, metinler):
        """Satır indekslerini, karakter/satır sınırını aşmayan gruplara böler."""
        grup, uzunluk = [], 0
        for i, metin in enumerate(metinler):
            satir_uzunlugu = len(metin) + 1
            if grup and (uzunluk + satir_uzunlugu > CEVIRI_MAX_KARAKTER or len(grup) >= CEVIRI_MAX_SATIR):
                yield grup
                grup, uzunluk = [], 0
            grup.append(i)
            uzunluk += satir_uzunlugu
        if grup:
            yield grup

    def _tek_satir_cevir(self, translator, metin):
        """Tek satırı çevirir. Kalıcı olarak başarısız olursa None döner
        (çağıran taraf o satırda orijinal metni korur)."""
        for deneme in range(4):
            if self.is_cancelled:
                return None
            try:
                sonuc = translator.translate(metin)
                # deep_translator, çeviri kaynakla birebir aynı çıktığında None
                # döndürebiliyor (ör. "...", "- -" gibi sadece noktalama içeren
                # satırlar). Bu bir hata değil; eskiden bu None doğrudan dosyaya
                # yazılıp altyazıda "None" olarak görünüyordu.
                if sonuc is None or not sonuc.strip():
                    return metin
                return sonuc
            except Exception:
                time.sleep(self._bekleme_suresi(deneme))
        return None

    def _grup_cevir(self, grup_metinleri, kaynak_dil):
        """Bir grup satırı TEK istekte çevirir; hizalama bozulursa satır satır çevirir.
        Her zaman len(grup_metinleri) uzunluğunda liste döner; çevrilemeyen satırlar None."""
        # Not: GoogleTranslator nesnesi thread-safe DEĞİL (istek parametrelerini
        # kendi üzerinde tutuyor), bu yüzden her görev kendi nesnesini kurar.
        translator = GoogleTranslator(source=kaynak_dil, target='tr')

        paket = "\n".join(grup_metinleri)
        for deneme in range(3):
            if self.is_cancelled:
                return [None] * len(grup_metinleri)
            try:
                sonuc = translator.translate(paket)
                if sonuc:
                    parcalar = sonuc.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    # Google bazen satırları birleştirip/bölerek farklı sayıda satır
                    # döndürür. Sayı tutmazsa hizalama kayar (o gruptan sonraki TÜM
                    # altyazılar yanlış metinle eşleşir), bu yüzden yalnızca birebir
                    # eşleşmeyi kabul ediyoruz.
                    if len(parcalar) == len(grup_metinleri):
                        return parcalar
                break  # hizalama bozuk: tekrar denemek düzeltmez, satır satıra geç
            except Exception:
                time.sleep(self._bekleme_suresi(deneme))

        # YEDEK PLAN: paket çevirisi tutmadı → hizalamayı garantilemek için satır satır
        sonuclar = []
        for metin in grup_metinleri:
            sonuclar.append(self._tek_satir_cevir(translator, metin))
            time.sleep(0.25)
        return sonuclar

    # ------------------------------------------------------------------
    # MODEL YÖNETİMİ
    # ------------------------------------------------------------------

    def _kutuphaneleri_yukle(self):
        global whisperx, torch, np
        if whisperx is not None:
            return
        self.log("📚 Kütüphaneler yükleniyor (whisperx + torch)... İlk açılışta ~10 sn sürer.")
        self.ilerleme(0, "Kütüphaneler yükleniyor...")

        # pyannote, import anında torchcodec'i deneyip uzun bir uyarı basıyor.
        # Sesi biz zaten ffmpeg ile çözüp belleğe verdiğimiz için torchcodec hiç
        # kullanılmıyor; uyarı yalnızca gürültü.
        import warnings
        warnings.filterwarnings("ignore", message=".*torchcodec.*")

        import numpy as _np
        import torch as _torch
        import whisperx as _whisperx
        np, torch, whisperx = _np, _torch, _whisperx

    def _modeli_hazirla(self, model_adi, hassasiyet, cekirdek, vad_onset, vad_offset):
        """Modeli gerekiyorsa yükler, gerekmiyorsa bellektekini kullanır.
        VAD eşikleri model yeniden yüklenmeden değiştirilebiliyor: whisperx eşikleri
        modele değil, konuşma parçalarının birleştirme adımına uyguluyor."""
        anahtar = f"{model_adi}|{hassasiyet}|{cekirdek}"

        if anahtar != self.yuklu_model_anahtari or self.pipeline is None:
            if self.pipeline is not None:
                self.log("♻️ Ayar değişti, model yeniden yükleniyor...")
                self.pipeline = None
                gc.collect()

            self.log(f"🧠 Yapay Zeka Modeli Yükleniyor: {model_adi} ({hassasiyet}, {cekirdek} çekirdek)")
            self.log("   (Model ilk kez kullanılıyorsa indirilecek; sonraki açılışlarda anında gelir.)")
            self.ilerleme(0, "Model yükleniyor...")

            torch.set_num_threads(cekirdek)
            self.pipeline = whisperx.load_model(
                model_adi,
                device="cpu",
                compute_type=hassasiyet,
                threads=cekirdek,
                vad_method="pyannote",
                vad_options={"chunk_size": 30, "vad_onset": vad_onset, "vad_offset": vad_offset},
                asr_options={"beam_size": 5},
            )
            self.yuklu_model_anahtari = anahtar
            self.log("✅ Model belleğe (RAM) yüklendi!")
        else:
            self.log("⚡ Model zaten bellekte, beklemeden işleme geçiliyor!")
            self.pipeline._vad_params["vad_onset"] = vad_onset
            self.pipeline._vad_params["vad_offset"] = vad_offset

    def _align_modeli_hazirla(self, dil):
        """Dile özel hizalama (wav2vec2) modelini yükler. Model yoksa None döner."""
        if self.align_model is not None and self.yuklu_align_dili == dil:
            return True

        self.log(f"📐 Hizalama modeli yükleniyor ({dil})... İlk kullanımda indirilir.")
        self.ilerleme(0, f"Hizalama modeli yükleniyor ({dil})...")
        try:
            self.align_model, self.align_meta = whisperx.load_align_model(language_code=dil, device="cpu")
            self.yuklu_align_dili = dil
            return True
        except Exception as e:
            self.align_model, self.align_meta, self.yuklu_align_dili = None, None, ""
            self.log(f"⚠️ '{dil}' dili için hizalama modeli bulunamadı ({e}).")
            self.log("   Kelime zamanlaması olmadan, segment zamanlarıyla devam ediliyor.")
            return False

    def _ilerleme_geri_cagrisi(self, asama, baslangic):
        """whisperx'e verilecek ilerleme fonksiyonunu üretir.
        İptal sinyalini de burada yakalıyoruz: whisperx'in içine başka türlü
        müdahale edilemiyor."""
        durum = {"son_log": -5.0, "son_ui": 0.0}

        def geri_cagri(yuzde):
            if self.is_cancelled:
                raise KullaniciIptali()

            simdi = time.time()
            gecen = simdi - baslangic

            if simdi - durum["son_ui"] >= 0.2:
                durum["son_ui"] = simdi
                kalan = ""
                if yuzde > 1:
                    tahmin = gecen * (100 - yuzde) / yuzde
                    kalan = f" | Kalan ~{self._sure_metni(tahmin)}"
                self.ilerleme(yuzde, f"{asama}: %{yuzde:.1f} | Geçen {self._sure_metni(gecen)}{kalan}")

            if yuzde - durum["son_log"] >= 5:
                durum["son_log"] = yuzde
                tahmin = gecen * (100 - yuzde) / yuzde if yuzde > 1 else 0
                self.log(f"   ⏳ {asama} %{yuzde:.0f}  (geçen {self._sure_metni(gecen)}, kalan ~{self._sure_metni(tahmin)})")

        return geri_cagri

    # ------------------------------------------------------------------
    # ANA İŞ AKIŞI
    # ------------------------------------------------------------------

    def run_process(self):
        islem_basarili = False
        baslangic_zamani = time.time()
        try:
            video_file = self.video_path.get()
            model_name = self.model_size.get()
            hassasiyet = self.compute_map.get(self.compute_type.get(), "int8")
            batch_size = self.batch_map.get(self.batch_mode.get(), 8)
            cekirdek = int(self.cpu_threads.get())
            vad_onset, vad_offset = self.vad_map.get(self.vad_mode.get(), (0.500, 0.363))

            selected_label = self.source_lang.get()
            lang_code = self.lang_map.get(selected_label, "auto")

            task = "translate" if "İngilizceye" in self.task_type.get() else "transcribe"
            word_level = "Kelime Kelime" in self.subtitle_style.get()
            isim_eki = "_Reels" if word_level else ""

            if not os.path.isfile(video_file):
                self.log("❌ HATA: Seçilen dosya bulunamadı.")
                return

            ffmpeg_yolu = self._ffmpeg_coz()
            if not ffmpeg_yolu:
                elle = (self.ffmpeg_path.get() or "").strip()
                self.log("❌ HATA: FFmpeg bulunamadı. Ses FFmpeg ile çözülüyor, onsuz devam edilemez.")
                if elle:
                    self.log(f"   Ayarlardaki yol geçersiz: {elle}")
                    self.log("   'Otomatik' düğmesine basıp sistemdekine dönebilirsiniz.")
                else:
                    self.log("   Sisteminizde FFmpeg kurulu değil (PATH'te bulunamadı).")
                    self.log("   Kurulum:  winget install Gyan.FFmpeg")
                    self.log("        ya da: scoop install ffmpeg")
                    self.log("   Kuruluysa 'Gözat' ile ffmpeg.exe dosyasını elle seçebilirsiniz.")

                def _ffmpeg_uyarisi():
                    messagebox.showerror(
                        "FFmpeg Bulunamadı",
                        "Bu program sesi çözmek için FFmpeg kullanıyor ve sisteminizde bulunamadı.\n\n"
                        "Çözüm 1: FFmpeg kurun\n"
                        "    winget install Gyan.FFmpeg\n\n"
                        "Çözüm 2: Zaten kuruluysa 'FFmpeg Yolu' satırındaki 'Gözat' düğmesiyle\n"
                        "ffmpeg.exe dosyasını elle seçin.")
                self.root.after(0, _ffmpeg_uyarisi)
                return

            if "distil" in model_name.lower() and lang_code not in ("en", "auto"):
                self.log("⚠️ KRİTİK UYARI: 'distil' modelleri sadece İNGİLİZCE için eğitilmiştir!")

                def _distil_uyarisi():
                    messagebox.showerror("Model Uygunsuz", "'distil-large-v3' modeli sadece İngilizce destekler.")
                self.root.after(0, _distil_uyarisi)
                return

            self.log("=" * 45)
            self._kutuphaneleri_yukle()

            # --- 1) SES ÇÖZME ---
            self.log(f"🎬 Ses çözülüyor ({os.path.basename(ffmpeg_yolu)})...")
            self.ilerleme(0, "Ses çözülüyor...")
            ses = self._sesi_coz(video_file, ffmpeg_yolu)
            sure = len(ses) / 16000
            self.log(f"   Süre: {self._sure_metni(sure)}")

            if self.is_cancelled:
                raise KullaniciIptali()

            # --- 2) MODEL ---
            self._modeli_hazirla(model_name, hassasiyet, cekirdek, vad_onset, vad_offset)

            # --- 3) DİL TESPİTİ ---
            # Yalnızca "Otomatik Algıla" seçiliyken devreye girer; kullanıcı dili elle
            # seçtiyse ona dokunmuyoruz.
            secilen_dil = None
            oy_guveni = 0.0

            if lang_code == "auto":
                self.log("🔎 Dil tespit ediliyor (filmin geneline yayılmış örneklerle)...")
                secilen_dil, oy_guveni, oylar = self._dil_oyla(ses)
                if secilen_dil:
                    siralama = sorted(oylar.items(), key=lambda x: x[1], reverse=True)
                    ozet = "  ".join(f"{d.upper()}:{a:.2f}" for d, a in siralama[:4])
                    self.log(f"   Oylar (ağırlık): {ozet}")
                    self.log(f"✅ Dil (oylama): {secilen_dil.upper()} (Güven: %{oy_guveni * 100:.1f})")
                    if oy_guveni < DIL_GUVEN_ESIGI:
                        self.log("=" * 45)
                        self.log(f"⚠️ DİL TESPİTİ ZAYIF (%{oy_guveni * 100:.1f})!")
                        self.log("   Yanlış dil, altyazının bozuk çıkmasına ve tekrar döngüsüne yol açar.")
                        self.log("   💡 Dili biliyorsanız 'Videonun Dili' kutusundan elle seçip tekrar çalıştırın.")
                        self.log("=" * 45)
            else:
                secilen_dil = lang_code
                self.log(f"✅ Dil elle seçildi: {lang_code.upper()}")

            if self.is_cancelled:
                raise KullaniciIptali()

            # --- 4) SESİ METNE ÇEVİR ---
            self.log(f"🎙️ Sesler Metne Dönüştürülüyor (batch {batch_size}, {cekirdek} çekirdek)...")
            self.log("   ⚠️ İşlemci üzerinde çalışıyor; uzun videolarda bu adım saatler sürebilir.")
            t_transkript = time.time()
            try:
                sonuc = self.pipeline.transcribe(
                    ses,
                    batch_size=batch_size,
                    language=secilen_dil,      # None ise whisperx ilk 30 sn'den kendi tespit eder
                    task=task,
                    chunk_size=30,
                    progress_callback=self._ilerleme_geri_cagrisi("Metne dönüştürme", t_transkript),
                )
            finally:
                # transcribe yarıda kalırsa tokenizer eski dilde takılı kalıyor;
                # bir sonraki çalıştırma yanlış dille başlamasın diye sıfırlıyoruz.
                if getattr(self.pipeline, "preset_language", None) is None:
                    self.pipeline.tokenizer = None

            detected_lang = sonuc.get("language") or secilen_dil or "auto"
            segmentler = sonuc.get("segments") or []
            self.log(f"✅ Metne dönüştürme bitti: {len(segmentler)} segment "
                     f"({self._sure_metni(time.time() - t_transkript)})")

            if lang_code == "auto" and not secilen_dil:
                self.log(f"   (Dil whisperx tarafından ilk 30 sn'den seçildi: {detected_lang.upper()})")

            if self.is_cancelled:
                raise KullaniciIptali()

            # --- 5) KELİME HİZALAMA (WhisperX'in asıl işi) ---
            hizalandi = False
            hizalama_istendi = self.do_align.get()

            if hizalama_istendi and task == "translate":
                # Metin İngilizceye çevrilmiş ama ses hâlâ orijinal dilde. Hizalama
                # modeli sesteki fonemleri metinle eşleştirdiği için bu kombinasyonda
                # çalışmaz; zorlanırsa zamanlar tamamen kayar.
                self.log("ℹ️ 'İngilizceye Çevir' modunda kelime hizalaması yapılamaz (ses ile metnin dili farklı).")
                hizalama_istendi = False

            if hizalama_istendi and segmentler:
                if self._align_modeli_hazirla(detected_lang):
                    self.log("📐 Kelimeler sese hizalanıyor...")
                    t_align = time.time()
                    hizali = whisperx.align(
                        segmentler,
                        self.align_model,
                        self.align_meta,
                        ses,
                        "cpu",
                        return_char_alignments=False,
                        progress_callback=self._ilerleme_geri_cagrisi("Hizalama", t_align),
                    )
                    segmentler = hizali.get("segments") or []
                    hizalandi = True
                    self.log(f"✅ Hizalama bitti: {len(hizali.get('word_segments') or [])} kelime "
                             f"({self._sure_metni(time.time() - t_align)})")
            elif not hizalama_istendi:
                self.log("ℹ️ Kelime hizalaması kapalı: segment zamanları kullanılacak.")

            if self.is_cancelled:
                raise KullaniciIptali()

            # --- 6) ALTYAZI BLOKLARINI KUR ---
            self.ilerleme(100, "Altyazı blokları kuruluyor...")
            srt_blocks = []
            for segment in segmentler:
                kelimeler = self._kelimeleri_topla(segment) if hizalandi else self._sozde_kelimeler(segment)
                if not kelimeler:
                    continue
                srt_blocks.extend(self._bloklari_kur(kelimeler, word_level))

            # --- HALÜSİNASYON DÖNGÜSÜ KORUMASI ---
            # Model tekrar döngüsüne girmişse aynı metin arka arkaya yüzlerce kez
            # gelebiliyor. Bunlar hem altyazıyı kullanılamaz hale getiriyor hem de
            # boşuna çeviriliyor; yazmadan önce ayıklıyoruz.
            srt_blocks, atilan_tekrar, en_uzun_tekrar = self._tekrar_filtrele(srt_blocks)
            if atilan_tekrar:
                self.log("=" * 45)
                self.log(f"⚠️ TEKRAR DÖNGÜSÜ: Aynı metin arka arkaya {en_uzun_tekrar} kez üretilmiş.")
                self.log(f"   {atilan_tekrar} tekrar bloğu ayıklandı.")
                self.log("   💡 Bu genellikle dilin yanlış algılanmasından olur.")
                self.log("      'Videonun Dili' kutusundan dili elle seçip tekrar deneyin.")

            for blok in srt_blocks:
                self.log(f"[{self.format_timestamp(blok['start'])}] {blok['text'][:45]}")

            # --- 7) ÇIKTI DOSYA İSİMLERİNİ BELİRLE ---
            # Türkçe çevirisi yapılacaksa: Türkçe dosya "video.srt" (varsayılan), orijinal
            # "video_EN.srt" olur. Böylece oynatıcı Türkçe altyazıyı otomatik yükler.
            base_path = os.path.splitext(video_file)[0]

            # "İngilizceye Çevir (Translate)" seçilmişse Whisper metni ZATEN İngilizceye
            # çevirmiş oluyor. Google'a kaynak dil olarak videonun orijinal dilini vermek
            # (ör. metin İngilizceyken source="fr") çeviriyi bozuyor ya da tamamen
            # boş bırakıyordu. Çevirinin gerçek kaynak dili aşağıdaki değişken.
            ceviri_kaynak_dili = "en" if task == "translate" else detected_lang

            will_translate = (
                self.auto_translate_tr.get()
                and ceviri_kaynak_dili != "tr"
                and not self.is_cancelled
                and len(srt_blocks) > 0
            )

            if len(srt_blocks) == 0:
                self.log("⚠️ UYARI: Hiç konuşma çıkarılamadı. Sessizlik Filtresi'ni 'Hassas' yapıp tekrar deneyin.")

            if will_translate:
                output_file_original = f"{base_path}{isim_eki}_{ceviri_kaynak_dili.upper()}.srt"
                output_file_tr = f"{base_path}{isim_eki}.srt"
            else:
                output_file_original = f"{base_path}{isim_eki}.srt"
                output_file_tr = None

            self._srt_yaz(output_file_original, srt_blocks)
            self.log(f"💾 Orijinal Altyazı Kaydedildi:\n{os.path.basename(output_file_original)}")

            # --- 8) TÜRKÇE ÇEVİRİ ---
            if will_translate:
                self.log("=" * 45)
                self.log("🌐 Orijinal altyazı Türkçe'ye çevriliyor (Google Translate)...")

                # Whisper, Google Çeviri'nin tanımadığı bir dil kodu döndürebiliyor
                # (bazı lehçeler). Eskiden bu, çevirici nesnesi kurulurken hata fırlatıp
                # tüm işlemi "KRİTİK HATA" ile bitiriyordu; artık otomatik algılamaya düşüyoruz.
                try:
                    if not GoogleTranslator(source="auto", target="tr").is_language_supported(ceviri_kaynak_dili):
                        self.log(f"⚠️ '{ceviri_kaynak_dili}' Google Çeviri'de desteklenmiyor, otomatik algılamaya geçiliyor.")
                        ceviri_kaynak_dili = "auto"
                except Exception:
                    ceviri_kaynak_dili = "auto"

                texts_to_translate = [b['text'] for b in srt_blocks]
                gruplar = list(self._satir_gruplari(texts_to_translate))
                translated_texts = {}
                tamamlanan = 0
                t_ceviri = time.time()

                self.log(f"   {len(texts_to_translate)} satır, {len(gruplar)} pakette gönderilecek.")

                def grup_isi(grup):
                    return grup, self._grup_cevir([texts_to_translate[i] for i in grup], ceviri_kaynak_dili)

                with ThreadPoolExecutor(max_workers=CEVIRI_ISCI_SAYISI) as executor:
                    futures = [executor.submit(grup_isi, g) for g in gruplar]
                    for future in as_completed(futures):
                        if self.is_cancelled:
                            for bekleyen in futures:
                                bekleyen.cancel()
                            break

                        grup, sonuclar = future.result()
                        for idx, ceviri in zip(grup, sonuclar):
                            # ceviri None ise o satır kalıcı olarak çevrilemedi:
                            # sözlüğe yazmıyoruz, aşağıda orijinal metin kullanılacak.
                            if ceviri is not None:
                                translated_texts[idx] = ceviri

                        tamamlanan += 1
                        yuzde = tamamlanan * 100 / len(gruplar)
                        self.ilerleme(yuzde, f"Çeviri: %{yuzde:.0f} ({tamamlanan}/{len(gruplar)} paket)")
                        if tamamlanan % 5 == 0 or tamamlanan == len(gruplar):
                            self.log(f"   🌐 Çeviri: %{yuzde:.0f}  ({tamamlanan}/{len(gruplar)} paket, "
                                     f"{self._sure_metni(time.time() - t_ceviri)})")

                if self.is_cancelled:
                    self.log("🛑 Çeviri yarıda kesildi; o ana kadar çevrilenler kaydediliyor.")

                # İptal edilse bile eldeki çeviriler yazılıyor (eskiden hepsi çöpe gidiyordu).
                self._srt_yaz(output_file_tr, srt_blocks, translated_texts)
                self.log(f"🇹🇷 Türkçe Altyazı Kaydedildi:\n{os.path.basename(output_file_tr)}")

                cevrilemeyen = len(srt_blocks) - len(translated_texts)
                if cevrilemeyen > 0 and not self.is_cancelled:
                    self.log(f"⚠️ {cevrilemeyen}/{len(srt_blocks)} satır çevrilemedi; bu satırlarda orijinal metin bırakıldı.")
                    self.log("   (Sebep genellikle Google'ın geçici istek limitidir. İşlemi tekrar çalıştırmak bu satırları düzeltir.)")
            elif self.auto_translate_tr.get() and ceviri_kaynak_dili == "tr":
                self.log("ℹ️ Video zaten Türkçe; ayrıca çeviri dosyası üretilmedi.")

            if not self.is_cancelled:
                gecen_sure = time.time() - baslangic_zamani
                self.log("=" * 45)
                self.log(f"🎉 TÜM İŞLEMLER KUSURSUZ TAMAMLANDI! (Süre: {self._sure_metni(gecen_sure)})")
                self.ilerleme(100, f"Tamamlandı — {self._sure_metni(gecen_sure)}")
                islem_basarili = True

        except KullaniciIptali:
            self.log("🛑 İşlem kullanıcı tarafından durduruldu.")
            self.ilerleme(0, "İptal edildi.")
        except Exception as e:
            self.log(f"❌ KRİTİK HATA:\n{str(e)}")
            with open(os.path.join(_APP_DIR, "HATA_RAPORU.txt"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.log("   Ayrıntılar HATA_RAPORU.txt dosyasına yazıldı.")
            self.ilerleme(0, "Hata oluştu.")

        finally:
            def bitis_islemleri():
                self.btn_start.config(state="normal", text="🚀 İŞLEMİ BAŞLAT", bg="#0078D4", fg="white")
                self.btn_cancel.config(state="disabled", text="🛑 İPTAL ET", bg="#D13438")

                if islem_basarili:
                    messagebox.showinfo("Başarılı", "🎉 Tüm altyazı ve çeviri işlemleri kusursuz tamamlandı!")
                elif self.is_cancelled:
                    messagebox.showwarning("İptal Edildi", "🛑 İşlem başarıyla durduruldu.")

            self.root.after(0, bitis_islemleri)


if __name__ == "__main__":
    root = tk.Tk()
    app = WhisperApp(root)
    root.mainloop()

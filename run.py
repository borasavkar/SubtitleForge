import os
import sys
import traceback
import threading
import time
import json
import gc
import queue
import random
import re
import shutil
import subprocess
import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from deep_translator import GoogleTranslator

from surum import SURUM

# Derleme bilgisi exe üretilirken run.spec tarafından yazılıyor. Kaynaktan
# çalıştırıldığında bu modül yok; o zaman "kaynaktan" olduğunu söylüyoruz.
# Amaç: elindeki exe'nin güncel olup olmadığını sürüm numarasına ek olarak
# derleme tarihinden ve commit'ten de anlayabilmek.
try:
    from _derleme_bilgisi import DERLEME_TARIHI, DERLEME_COMMIT
except Exception:
    DERLEME_TARIHI, DERLEME_COMMIT = "", ""

# Windows konsolu varsayılan olarak cp1254 kullanıyor ve log satırlarındaki emojiler
# UnicodeEncodeError ile çöküyordu (depodaki eski HATA_RAPORU.txt tam olarak buydu).
# Hata yakalayıcının kendisi de print() kullandığı için çökme raporu bile basılamıyordu.
for _akis in (sys.stdout, sys.stderr):
    try:
        _akis.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def surum_metni(kisa=False):
    """Kullanıcıya gösterilecek sürüm satırı.

    kisa=True  -> "v1.1.0"                                  (pencere başlığı)
    kisa=False -> "v1.1.0 · 2026-08-27 derlemesi · a2d324a"  (log satırı)
    """
    if kisa or not DERLEME_TARIHI:
        return f"v{SURUM}" if kisa else f"v{SURUM} · kaynaktan çalışıyor"
    parcalar = [f"v{SURUM}", f"{DERLEME_TARIHI} derlemesi"]
    if DERLEME_COMMIT:
        parcalar.append(DERLEME_COMMIT)
    return " · ".join(parcalar)

# Pencereli (pythonw / console=False) çalışırken ffmpeg'in siyah konsol
# penceresi açmasını engelleyen bayrak.
_KONSOLSUZ = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --- SİHİRLİ HATA YAKALAYICI ---
def hata_yakalayici(exctype, value, tb):
    hata_mesaji = "".join(traceback.format_exception(exctype, value, tb))
    with open(os.path.join(_APP_DIR, "HATA_RAPORU.txt"), "w", encoding="utf-8") as f:
        # Sürüm satırı raporun başında: paylaşılan bir rapordan hangi yapıya ait
        # olduğu anlaşılmıyordu.
        f.write(f"SubtitleForge {surum_metni()}\n\n{hata_mesaji}")
    # Pencereli (console=False) çalışırken stdout/stdin bulunmayabiliyor;
    # rapor zaten dosyaya yazıldı, ekrana basmak başarısız olursa sessiz geçiyoruz.
    try:
        print("\n" + "=" * 55)
        print("❌ KRİTİK BİR ÇÖKME YAŞANDI! ❌")
        print(hata_mesaji)
        input("\nPencereyi kapatmak için ENTER tuşuna bas...")
    except Exception:
        pass


sys.excepthook = hata_yakalayici


# --- ÇEVİRİ AYARLARI ---
# Google'ın ücretsiz uç noktası istek başına ~5000 karaktere izin veriyor.
# Satırları tek pakette birleştirip gönderiyoruz: ~1200 satırlık bir filmde
# ~1200 istek yerine ~35 istek yapılıyor. Hem çok daha hızlı, hem de
# "429 Too Many Requests" yüzünden satır kaybetme riski neredeyse sıfırlanıyor.
#
# ÖNEMLİ (eski sürümdeki "bazı cümleler çevrilmiyor" hatasının kaynağı):
# deep_translator'ın GoogleTranslator'ı /m (mobil HTML) uç noktasını kullanıyor ve
# dönen HTML'i `element.get_text(strip=True)` ile okuyor. Bu çağrı metindeki TÜM
# satır sonlarını siliyor; yani çok satırlı paket her zaman TEK satır olarak geri
# geliyordu. Satır sayısı hiçbir zaman tutmadığı için her paket satır-satır çeviriye
# düşüyor, Google da bu istek yağmuruna 429 ile cevap verip satırları çevrilmemiş
# bırakıyordu. Artık satır sonlarını koruyan translate_a/single uç noktasını
# kullanan kendi istemcimiz (GoogleCevirici) devrede; deep_translator yalnızca
# tek satırlık son çare yedeği olarak duruyor.
CEVIRI_MAX_KARAKTER = 1600   # tek istekte gönderilecek en fazla karakter
CEVIRI_MAX_SATIR = 40        # tek istekte gönderilecek en fazla satır
CEVIRI_ISCI_SAYISI = 3       # eşzamanlı istek sayısı (429 görülünce hepsi birlikte frenliyor)
CEVIRI_ZAMAN_ASIMI = 25      # tek istek için saniye
CEVIRI_ONARIM_TURU = 3       # ana geçişten sonra kaç kez "eksik satır" turu atılacak

# --- BİRİNCİL UÇ NOKTA DEVRE KESİCİSİ ---
# translate_a/single bazen bu IP'ye topluca 429 vermeye başlıyor ve saatlerce
# açılmıyor (ölçüldü: üç uç noktanın üçü de 429, aynı anda deep_translator'ın /m
# yolu sorunsuz çeviriyordu). Eskiden kod bunu fark etmiyordu: HER satır için
# 3 tur x 3 uç nokta = 9 beyhude istek atıyor, her 429'da 6-11 sn küresel fren
# uyguluyor, ancak ondan sonra çalışan yedek yola geçiyordu. 450 satırlık bir
# filmde bu saatlere çıkıyor, uygulama "takıldı" gibi görünüyor ve sonunda hiçbir
# satır çevrilmemiş oluyordu.
# Artık arka arkaya bu kadar limit yanıtı gelince birincil yol kapatılıyor ve
# istekler anında düşüyor. Araya BİR başarılı istek girerse sayaç sıfırlanıyor,
# yani geçici dalgalanma devreyi açmıyor.
CEVIRI_LIMIT_ESIGI = 18      # arka arkaya bu kadar 429/503 -> birincil yol kapanır
# Hızlı karar: bir tam cevir() turu (3 uç nokta x 3 deneme) HİÇ başarılı istek
# olmadan tükenmişse üç ayrı Google sunucusu da bizi reddediyor demektir; yol
# gerçekten kapalı. 18 ardışık limiti beklemek her 429'da 6-11 sn fren yediği
# için ~2,5 dakika boşa gidiyordu. Tek bir istek bile başarılı olmuşsa bu kural
# devreye girmiyor, tam eşik (CEVIRI_LIMIT_ESIGI) aranıyor.
CEVIRI_LIMIT_HIZLI = 9       # hiç başarı yokken bu kadar istek -> yol kapalı sayılır
CEVIRI_YEDEK_ISCI = 3        # yedek yolda eşzamanlı istek (satır satır gidiyor)
# Yedek yol da geçici olarak reddedebiliyor (ölçüldü: 10 satırın 2'si bir turda
# düştü, sonraki turda sorunsuz geldi). Tek denemede bırakılırsa o satır kalıcı
# olarak çevrilmemiş kalıyor ve kullanıcı "bazı cümleleri atlıyor" diyor.
CEVIRI_YEDEK_TURU = 4        # yedek yolda eksik kalan satırlar kaç tur tekrar denenir
# Turlar arası bekleme: 2-4 sn çok kısaydı, yorgun uç nokta toparlanamadan tekrar
# yükleniyorduk (ölçüldü: 3 turda 4 satır hâlâ düşüyordu). 6/12/18 sn ile uç nokta
# nefes alıyor; toplam ek maliyet en kötü ihtimalle ~36 sn.
CEVIRI_YEDEK_BEKLEME = 6.0   # tur başına saniye (tur 2 -> 6sn, tur 3 -> 12sn, ...)

# --- ÜÇÜNCÜ BASAMAK: YEREL ÇEVİRİ ---
# Google'ın iki uç noktası da istek limiti verdiğinde çalışan tek yol: makinede
# koşan Opus-MT modeli. Kota yok, internet yok (model bir kez indikten sonra),
# sansür yok -- yıldızlama yapamaz, çünkü ağa hiç çıkmıyor.
# Ölçüldü: CPU'da 2,3 cümle/sn (450 cümlelik film ~3 dk); 10 cümlelik
# karşılaştırmada kalite Google ile denk çıktı (4 birebir aynı, 2 Opus lehine,
# 1 Google lehine). Deyimlerde ikisi de zayıf.
# Türkçeye DOĞRUDAN model yalnızca İngilizceden var; diğer diller İngilizce
# üzerinden aktarılıyor (iki model, bir miktar kalite kaybı).
# Model HF önbelleğine iniyor; _model_klasoru_ayarla HF_HOME'u uygulamanın
# yanındaki "models" klasörüne çevirdiği için modelleri_kopyala.ps1 onu da taşıyor.
YEREL_MODEL_TR = "Helsinki-NLP/opus-mt-tc-big-en-tr"
YEREL_KOPRU_KALIBI = "Helsinki-NLP/opus-mt-{dil}-en"
YEREL_YIGIN = 16             # tek seferde modele verilecek cümle sayısı

# Aynı istek birden fazla ana üzerinden denenebiliyor: biri 429 verse bile
# diğerlerinden satır kurtarılabiliyor. Üçü de aynı JSON biçimini döndürüyor.
CEVIRI_UC_NOKTALARI = (
    "https://translate.googleapis.com/translate_a/single",
    "https://translate.google.com/translate_a/single",
    "https://clients5.google.com/translate_a/single",
)
CEVIRI_BASLIKLARI = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "tr,en;q=0.9",
}

# --- SANSÜR KARŞITI AYARLAR ---
# 1) Çeviri tarafı: /m uç noktası küfürleri bazen yıldızlayarak ("s**tir") ya da
#    yumuşatarak döndürüyor. translate_a/single bunu yapmıyor; alttaki desen yine de
#    yedek yoldan gelmiş yıldızlı satırları yakalayıp tekrar denemek için kullanılıyor.
# 2) Transkripsiyon tarafı: Whisper'a metnin birebir yazılacağını söyleyen kısa bir
#    initial_prompt veriliyor. İstem, MODELİN YAZDIĞI DİLDE olmak zorunda; yabancı
#    dilde istem modeli o dile kaydırıp altyazıyı tamamen bozuyor. Bu yüzden yalnızca
#    aşağıda karşılığı bulunan diller için uygulanıyor.

# Yıldızlı maskeleme deseni. Rakamlar bilerek dışarıda: "2*3" ya da "5 * 3 = 15"
# gibi çarpma ifadeleri sansür sanılıp boşuna yeniden çevriliyordu.
# [^\W\d_] = "harf" (Türkçe/Kiril harfleri dahil, rakam ve alt çizgi hariç).
SANSUR_DESENI = re.compile(r"[^\W\d_]\*(?:\*+|[^\W\d_])|\*{2,}[^\W\d_]|\*{3,}", re.UNICODE)

SANSURSUZ_ISTEMLER = {
    "tr": "Bu birebir bir deşifre metnidir. Küfür, argo ve müstehcen ifadeler "
          "yumuşatılmadan, kısaltılmadan ve sansürlenmeden aynen yazılır. "
          "Noktalama işaretleri eksiksiz kullanılır.",
    "en": "This is a verbatim transcript. Profanity, slang and explicit language are "
          "written out in full, exactly as spoken, without censoring or softening. "
          "Punctuation is used throughout.",
    "de": "Dies ist eine wortgetreue Abschrift. Schimpfwörter, Slang und explizite "
          "Ausdrücke werden unzensiert und ungeschönt genau so geschrieben, wie sie "
          "gesprochen werden. Die Zeichensetzung ist vollständig.",
    "fr": "Ceci est une transcription mot à mot. Les grossièretés, l'argot et le langage "
          "explicite sont écrits tels quels, sans censure ni adoucissement. "
          "La ponctuation est complète.",
    "it": "Questa è una trascrizione fedele. Volgarità, slang ed espressioni esplicite "
          "sono riportate esattamente come vengono pronunciate, senza censura né "
          "attenuazioni. La punteggiatura è completa.",
    "es": "Esta es una transcripción literal. Las palabrotas, la jerga y el lenguaje "
          "explícito se escriben tal y como se pronuncian, sin censura ni suavizado. "
          "Se usa la puntuación completa.",
    "ru": "Это дословная расшифровка. Ругательства, сленг и нецензурные выражения "
          "записываются точно так, как они произнесены, без цензуры и смягчения. "
          "Пунктуация используется полностью.",
}

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

# --- CÜMLE BÜTÜNLÜĞÜ ---
# Altyazı bölücüsü cümleleri karakter sınırında ve kelime boşluklarında kesiyor. Bu
# parçalar tek başına çevrilince anlam bozuluyor:
#   KAYNAK  : I never understood why she chose to leave the city without telling anyone.
#   PARÇALI : asla | neden şehri terk etmeyi seçtiğini anladı | kimseye söylemeden.
#   BÜTÜN   : Neden kimseye söylemeden şehri terk etmeyi seçtiğini hiç anlamadım.
# "never" ayrı bloğa düştüğü için olumsuzluk kayboluyor ve anlam TERSİNE dönüyor.
# Çözüm: çeviriyi cümle düzeyinde yapıp sonucu bloklara karakter oranına göre geri
# dağıtmak. Aşağıdaki sınırlar, noktalama hiç gelmediğinde grubun sonsuza kadar
# büyümesini engelliyor.
CUMLE_MAX_BLOK = 8           # bir cümle grubuna girebilecek en fazla blok
CUMLE_MAX_KARAKTER = 400     # bir cümle grubunun en fazla karakteri
CUMLE_MAX_BOSLUK = 2.0       # iki blok arası bu kadar saniyeden uzun sessizlik varsa ayır


class KullaniciIptali(Exception):
    """İptal butonuna basıldığında whisperx'in içinden çıkmak için kullanılıyor.
    whisperx.transcribe/align tek parça bloklayan çağrılar; iptali ancak
    ilerleme geri çağrısından istisna fırlatarak yakalayabiliyoruz."""
    pass


class CeviriHatasi(Exception):
    """Geçici çeviri hatası: ağ sorunu, HTTP hatası ya da bozuk yanıt."""
    pass


class GoogleCevirici:
    """Satır hizasını koruyan Google Çeviri istemcisi.

    Neden deep_translator yerine bu var: deep_translator'ın GoogleTranslator'ı
    çeviriyi mobil HTML sayfasından `get_text(strip=True)` ile söküyor ve bu çağrı
    satır sonlarını yok ediyor. translate_a/single uç noktası ise metni JSON olarak
    döndürüyor ve gönderdiğimiz "\\n" ayraçlarını olduğu gibi koruyor -- toplu
    çeviride satırların doğru altyazıya oturmasının tek güvenilir yolu bu.

    Bir de "fren" var: uç nokta 429/503 verdiğinde tek bir işçi değil, TÜM işçiler
    birlikte bekliyor. Eskiden her işçi kendi başına yeniden deneyip Google'ı daha
    da kızdırıyor, sonunda satırlar çevrilmemiş kalıyordu.
    """

    def __init__(self, iptal_kontrolu=None, log=None):
        self._iptal = iptal_kontrolu or (lambda: False)
        self._log = log or (lambda mesaj: None)
        # requests.Session thread-safe değil: her işçi kendi oturumunu kullanıyor.
        self._yerel = threading.local()
        self._kilit = threading.Lock()
        self._fren_bitisi = 0.0
        self.istek_sayisi = 0
        self.basarili_istek = 0
        # Devre kesici: arka arkaya gelen limit yanıtlarını sayıyor, bir başarıda
        # sıfırlanıyor. Eşiği aşınca birincil uç nokta kapanıyor (bkz. CEVIRI_LIMIT_ESIGI).
        self._ardisik_limit = 0
        self.limit_asildi = False

    # -- altyapı ----------------------------------------------------------

    @property
    def _oturum(self):
        oturum = getattr(self._yerel, "oturum", None)
        if oturum is None:
            oturum = requests.Session()
            oturum.headers.update(CEVIRI_BASLIKLARI)
            self._yerel.oturum = oturum
        return oturum

    def bekle(self, saniye):
        """İptale duyarlı bekleme: time.sleep(12) sırasında iptal edilen işlem
        eskiden 12 saniye boyunca yanıt vermiyordu."""
        bitis = time.monotonic() + saniye
        while True:
            kalan = bitis - time.monotonic()
            if kalan <= 0:
                return
            if self._iptal():
                raise KullaniciIptali()
            time.sleep(min(0.25, kalan))

    def _fren_bekle(self):
        while True:
            with self._kilit:
                kalan = self._fren_bitisi - time.monotonic()
            if kalan <= 0:
                return
            if self._iptal():
                raise KullaniciIptali()
            time.sleep(min(0.25, kalan))

    def _fren_uygula(self, saniye):
        with self._kilit:
            self._fren_bitisi = max(self._fren_bitisi, time.monotonic() + saniye)

    @staticmethod
    def _yaniti_coz(veri):
        """translate_a/single iki biçimde yanıt verebiliyor:
        dj=1 ile {"sentences": [{"trans": ...}, ...]}, dj yok sayılırsa [[["...",...]]].
        İkisini de aynı düz metne indiriyoruz."""
        parcalar = []
        if isinstance(veri, dict):
            for cumle in veri.get("sentences") or []:
                if isinstance(cumle, dict) and isinstance(cumle.get("trans"), str):
                    parcalar.append(cumle["trans"])
        elif isinstance(veri, list) and veri and isinstance(veri[0], list):
            for cumle in veri[0]:
                if isinstance(cumle, list) and cumle and isinstance(cumle[0], str):
                    parcalar.append(cumle[0])
        return "".join(parcalar)

    def _tek_istek(self, uc_nokta, metin, kaynak, hedef):
        parametreler = {
            "client": "gtx",
            "dj": "1",
            "dt": "t",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "sl": kaynak or "auto",
            "tl": hedef,
        }
        # Uzun paketler GET'in URL sınırını aşıyor (UTF-8 yüzde kodlaması metni
        # 3-6 katına çıkarıyor); bu yüzden metin gövdede POST ediliyor.
        try:
            cevap = self._oturum.post(uc_nokta, params=parametreler, data={"q": metin},
                                      timeout=CEVIRI_ZAMAN_ASIMI)
            if cevap.status_code in (400, 404, 405, 411, 413, 414, 501):
                cevap = self._oturum.get(uc_nokta, params=dict(parametreler, q=metin),
                                         timeout=CEVIRI_ZAMAN_ASIMI)
        except Exception as e:
            raise CeviriHatasi(f"{e.__class__.__name__}: {e}")

        with self._kilit:
            self.istek_sayisi += 1

        if cevap.status_code in (429, 503):
            # Google hepimize kızdı: tek tek değil, topluca geri çekiliyoruz.
            self._fren_uygula(random.uniform(6.0, 11.0))
            with self._kilit:
                self._ardisik_limit += 1
                sayi = self._ardisik_limit
                # İki koşuldan biri yeterli (bkz. CEVIRI_LIMIT_HIZLI):
                hicbir_basari = self.basarili_istek == 0 and self.istek_sayisi >= CEVIRI_LIMIT_HIZLI
                kapat = sayi >= CEVIRI_LIMIT_ESIGI or hicbir_basari
                yeni_kapandi = not self.limit_asildi and kapat
                if yeni_kapandi:
                    self.limit_asildi = True
                    toplam = self.istek_sayisi
            if yeni_kapandi:
                self._log(f"   🚧 Birincil çeviri uç noktası istek limiti veriyor "
                          f"({toplam} istek, {sayi} ardışık, hiç yanıt yok); "
                          f"bu yol kapatılıyor, yedek yola geçilecek.")
            raise CeviriHatasi(f"HTTP {cevap.status_code} (istek limiti)")
        if cevap.status_code != 200:
            raise CeviriHatasi(f"HTTP {cevap.status_code}")

        # Uç nokta yanıt verdi: devre kesici sayacı sıfırlanıyor. (200 dönüp boş
        # çeviri gelmesi bir limit sorunu değil, o yüzden burada sıfırlıyoruz.)
        with self._kilit:
            self.basarili_istek += 1
            self._ardisik_limit = 0

        try:
            veri = cevap.json()
        except Exception:
            raise CeviriHatasi("yanıt JSON olarak çözülemedi")

        cevrilmis = self._yaniti_coz(veri)
        if not cevrilmis.strip():
            raise CeviriHatasi("boş çeviri döndü")
        return cevrilmis.replace("\r\n", "\n").replace("\r", "\n")

    # -- genel arayüz -----------------------------------------------------

    def cevir(self, metin, kaynak, hedef="tr", tur_sayisi=3):
        """Metni çevirip düz metin döner. Satır sonları korunur.
        Kalıcı başarısızlıkta CeviriHatasi fırlatır."""
        if self.limit_asildi:
            # Devre kapalı: 9 beyhude istek atıp aralarında 6-11 sn fren beklemek
            # yerine anında düşüyoruz. Çağıran taraf yedek yola geçiyor.
            raise CeviriHatasi("birincil uç nokta istek limitinde (devre kapalı)")
        son_hata = None
        for tur in range(tur_sayisi):
            for uc_nokta in CEVIRI_UC_NOKTALARI:
                if self._iptal():
                    raise KullaniciIptali()
                self._fren_bekle()
                try:
                    return self._tek_istek(uc_nokta, metin, kaynak, hedef)
                except CeviriHatasi as e:
                    son_hata = e
            if tur < tur_sayisi - 1:
                # Üstel geri çekilme + jitter: işçilerin aynı anda tekrar
                # denemesi (thundering herd) yeni bir 429 dalgası yaratıyordu.
                self.bekle(min(2.0 * (2 ** tur), 15.0) + random.uniform(0, 1.0))
        raise son_hata or CeviriHatasi("çeviri uç noktalarının hiçbiri yanıt vermedi")

    def cevir_yedek(self, metin, kaynak, hedef="tr"):
        """Son çare: deep_translator'ın /m uç noktası. Yalnızca TEK satır için
        güvenli, çünkü o yol satır sonlarını siliyor. Başarısızsa None döner."""
        try:
            sonuc = GoogleTranslator(source=kaynak or "auto", target=hedef).translate(metin)
        except Exception:
            return None
        if sonuc is None:
            return None
        sonuc = str(sonuc).strip()
        return sonuc or None


class YerelCevirici:
    """Üçüncü basamak: tamamen yerel Opus-MT çevirisi.

    Google'ın hem translate_a/single hem /m yolu istek limiti verdiğinde devreye
    giriyor. Ağa hiç çıkmadığı için kota, 429 ve sansür diye bir derdi yok.

    Kullanılamıyorsa (kütüphane eksik, indirme başarısız, dil için model yok)
    hazirla() False döner ve çağıran taraf sessizce atlar: yerel çeviri bir
    iyileştirme, zorunluluk değil. Uygulamanın geri kalanı onsuz da çalışıyor."""

    def __init__(self, log=None, iptal_kontrolu=None):
        self._log = log or (lambda mesaj: None)
        self._iptal = iptal_kontrolu or (lambda: False)
        self._tr = None          # (tokenizer, model)  en -> tr
        self._kopru = None       # (tokenizer, model)  XX -> en  (gerekiyorsa)
        self._hazir = None       # None: denenmedi
        self._torch = None

    def hazirla(self, kaynak_dil):
        """Modelleri yükler; sonucu saklayıp bir daha denemez."""
        if self._hazir is not None:
            return self._hazir
        self._hazir = False

        try:
            import torch
            from transformers import MarianMTModel, MarianTokenizer
        except Exception as e:
            self._log(f"   ℹ️ Yerel çeviri kullanılamıyor (kütüphane eksik: {e}).")
            return False
        self._torch = torch

        dil = (kaynak_dil or "").lower().split("-")[0].split("_")[0]

        def yukle(ad):
            return MarianTokenizer.from_pretrained(ad), MarianMTModel.from_pretrained(ad)

        try:
            if dil in ("", "auto", "en"):
                if dil != "en":
                    self._log("   ℹ️ Kaynak dil kesin değil; yerel çeviri İngilizce varsayıyor.")
                self._log("   ⬇️ Yerel çeviri modeli hazırlanıyor (en→tr). "
                          "İlk seferde indirme birkaç dakika sürebilir...")
            else:
                self._log(f"   ⬇️ Yerel çeviri modeli hazırlanıyor ({dil}→en→tr). "
                          f"İlk seferde indirme birkaç dakika sürebilir...")
                self._kopru = yukle(YEREL_KOPRU_KALIBI.format(dil=dil))
            self._tr = yukle(YEREL_MODEL_TR)
        except Exception as e:
            self._log(f"   ℹ️ Yerel çeviri kurulamadı ({e}); bu basamak atlanıyor.")
            self._tr = self._kopru = None
            return False

        for ikili in (self._tr, self._kopru):
            if ikili:
                ikili[1].eval()
        self._hazir = True
        return True

    def _gecir(self, ikili, metinler, ilerleme=None, taban=0.0, pay=1.0):
        """Bir modeli yığın yığın çalıştırır."""
        tokenlayici, model = ikili
        cikti = []
        for i in range(0, len(metinler), YEREL_YIGIN):
            if self._iptal():
                raise KullaniciIptali()
            yigin = tokenlayici(metinler[i:i + YEREL_YIGIN], return_tensors="pt",
                                padding=True, truncation=True, max_length=512)
            with self._torch.no_grad():
                uretilen = model.generate(**yigin, num_beams=4, max_new_tokens=256)
            cikti += [tokenlayici.decode(c, skip_special_tokens=True) for c in uretilen]
            if ilerleme:
                ilerleme(taban + pay * min(1.0, (i + YEREL_YIGIN) / max(1, len(metinler))))
        return cikti

    def cevir_liste(self, metinler, ilerleme=None):
        """Metin listesini Türkçeye çevirir; aynı sırada liste döner."""
        if self._kopru:
            ara = self._gecir(self._kopru, metinler, ilerleme, 0.0, 0.5)
            return self._gecir(self._tr, ara, ilerleme, 0.5, 0.5)
        return self._gecir(self._tr, metinler, ilerleme, 0.0, 1.0)


class WhisperApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"SubtitleForge {surum_metni(kisa=True)}")
        # Pencere yeniden boyutlandırılabilir: alt sınır, ayar kutularının hepsinin
        # sığdığı yükseklik (bunun altında terminal alanı ezilir).
        self.root.minsize(700, 760)   # terminal paneli expand ettiği için taban düşük tutuluyor

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
        self.kuyruk = []                # sırayla işlenecek video yolları
        self.pipeline = None            # whisperx FasterWhisperPipeline
        self.yuklu_model_anahtari = ""  # (model, hassasiyet, çekirdek) üçlüsü
        self.align_model = None
        self.align_meta = None
        self.yuklu_align_dili = ""
        self._cevirici = None           # GoogleCevirici, her çalıştırmada yeniden kurulur
        self._ceviri_son_hata = None    # son çeviri hatası (kullanıcıya rapor için)

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

        # Pencere boyutu artık hatırlanıyor. Varsayılan yükseklik ekrana göre
        # hesaplanıyor: sabit 1055 px, kuyruk listesi eklendikten sonra terminal
        # panelini eziyordu; küçük ekranlarda ise pencere alta taşıyordu.
        x = ayarlar.get("x", 100)
        y = ayarlar.get("y", 100)
        try:
            ekran_h = self.root.winfo_screenheight()
        except Exception:
            ekran_h = 1080
        varsayilan_h = max(890, min(1180, ekran_h - 80))
        g = int(ayarlar.get("g", 760))
        h = int(ayarlar.get("h", varsayilan_h))
        # Kayıtlı boyut bu ekrana sığmıyorsa (başka monitörde kaydedilmiş olabilir)
        # ekrana göre kırpıyoruz, yoksa pencerenin altı görünmez oluyor.
        h = max(600, min(h, ekran_h - 40))
        self.root.geometry(f"{g}x{h}+{x}+{y}")

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
        self.uncensored = tk.BooleanVar(value=ayarlar.get("sansursuz", True))

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

        satir_dosya = ttk.Frame(frame_file)
        satir_dosya.pack(fill="x")
        self.entry_path = ttk.Entry(satir_dosya, textvariable=self.video_path, width=52)
        self.entry_path.pack(side="left", padx=(0, 10))
        btn_gozat = ttk.Button(satir_dosya, text="Gözat 📂", command=self.select_file)
        btn_gozat.pack(side="left")

        # --- KUYRUK ---
        # Kuyruk BOŞSA yukarıdaki tek dosya işlenir (eski davranış birebir korunur).
        # Kuyrukta dosya varsa sırayla hepsi işlenir; model bir kez yüklenip
        # tekrar kullanıldığı için ikinci videodan itibaren başlangıç maliyeti yok.
        satir_kuyruk = ttk.Frame(frame_file)
        satir_kuyruk.pack(fill="x", pady=(10, 0))
        self.btn_kuyruk_ekle = ttk.Button(satir_kuyruk, text="Kuyruğa Ekle ➕",
                                          command=self.kuyruga_ekle)
        self.btn_kuyruk_ekle.pack(side="left")
        self.btn_kuyruk_cikar = ttk.Button(satir_kuyruk, text="Seçileni Çıkar ➖",
                                           command=self.kuyruktan_cikar)
        self.btn_kuyruk_cikar.pack(side="left", padx=6)
        self.btn_kuyruk_temizle = ttk.Button(satir_kuyruk, text="Kuyruğu Temizle 🗑",
                                             command=self.kuyrugu_temizle)
        self.btn_kuyruk_temizle.pack(side="left")
        self.lbl_kuyruk = ttk.Label(satir_kuyruk, text="Kuyruk boş")
        self.lbl_kuyruk.pack(side="right")

        kutu = ttk.Frame(frame_file)
        kutu.pack(fill="x", pady=(6, 0))
        kaydirma = ttk.Scrollbar(kutu, orient="vertical")
        self.liste_kuyruk = tk.Listbox(kutu, height=4, activestyle="none",
                                       bg=input_bg, fg=fg_color, selectbackground="#0078D4",
                                       highlightthickness=0, relief="flat",
                                       yscrollcommand=kaydirma.set)
        kaydirma.config(command=self.liste_kuyruk.yview)
        self.liste_kuyruk.pack(side="left", fill="x", expand=True)
        kaydirma.pack(side="right", fill="y")

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
        btn_ff_gozat = ttk.Button(frame_ffmpeg, text="Gözat 📂", width=9,
                                  command=self.select_ffmpeg)
        btn_ff_gozat.pack(side="left", padx=(0, 4))
        btn_ff_oto = ttk.Button(frame_ffmpeg, text="Otomatik", width=9,
                                command=self.ffmpeg_otomatik)
        btn_ff_oto.pack(side="left")

        self.lbl_ffmpeg = tk.Label(frame_settings, text="FFmpeg aranıyor...", justify="left",
                                   bg=bg_color, fg="#aaaaaa", font=("Segoe UI", 9), anchor="w")
        self.lbl_ffmpeg.grid(row=9, column=0, columnspan=2, sticky="w", padx=5)

        chk_align = ttk.Checkbutton(frame_settings, text=" 🎯 Kelime Zamanlaması (WhisperX Hizalama - Önerilen)",
                                    variable=self.do_align)
        chk_align.grid(row=10, column=0, columnspan=2, sticky="w", padx=5, pady=(12, 2))

        chk_tr = ttk.Checkbutton(frame_settings, text=" 🇹🇷 İşlem Bitince Ekstra Türkçe (.srt) Dosyası Üret",
                                 variable=self.auto_translate_tr)
        chk_tr.grid(row=11, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 2))

        chk_sansur = ttk.Checkbutton(frame_settings, text=" 🔞 Sansürsüz Mod (küfür/argo yumuşatılmadan yazılsın)",
                                     variable=self.uncensored)
        chk_sansur.grid(row=12, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 10))

        # İşlem sürerken kilitlenecek kontroller. Kuyruk ortasında ayar
        # değiştirmek kalan videoları sessizce farklı işliyordu; kuyruğa ekleme
        # de çalışan tura yansımıyordu (liste işlem başında kopyalanıyor).
        self._kilitlenecek = [
            self.entry_path, btn_gozat,
            self.btn_kuyruk_ekle, self.btn_kuyruk_cikar, self.btn_kuyruk_temizle,
            combo_model, combo_lang, combo_task, combo_compute,
            combo_batch, combo_threads, combo_vad, combo_style,
            self.entry_ffmpeg, btn_ff_gozat, btn_ff_oto,
            chk_align, chk_tr, chk_sansur,
        ]

        tk.Label(frame_settings,
                 text="💡 İPUCU: Ekran kartı kullanılmıyor, tüm iş işlemcide dönüyor. Filmler için en dengeli ayar\n"
                      "     large-v3-turbo + int8'dir. Hizalama kapatılırsa hız artar, zamanlama kabalaşır.",
                 justify="left", bg=bg_color, fg="#aaaaaa", font=("Segoe UI", 9, "italic")
                 ).grid(row=13, column=0, columnspan=2, sticky="w", padx=5)

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

        self.txt_log = scrolledtext.ScrolledText(frame_log, height=16, state='disabled', font=("Consolas", 10),
                                                 bg="#000000", fg="#E0E0E0", relief="flat", padx=10, pady=10)
        self.txt_log.pack(fill="both", expand=True)

        self.root.after(100, self._flush_logs)

        # FFmpeg durumunu mainloop basladiktan SONRA kontrol et.
        # __init__ icinde thread baslatmak yaris yaratiyor: thread root.after'i
        # mainloop henuz calismadan cagirirsa "main thread is not in main loop"
        # ile sessizce oluyor ve etiket "araniyor..." kaliyordu.
        self.root.after(200, self._ffmpeg_durumu_guncelle)

        # Elindeki sürümün ne olduğu, log'un ilk satırında görünsün: HATA_RAPORU
        # paylaşıldığında hangi yapıya ait olduğu da böyle anlaşılıyor.
        self.log(f"🎬 SubtitleForge {surum_metni()}")

        if _MODEL_KLASORU:
            self.log(f"📦 Modeller program klasöründen okunuyor: {os.path.basename(_MODEL_KLASORU)}")

    # ------------------------------------------------------------------
    # ARAYÜZ YARDIMCILARI
    # ------------------------------------------------------------------

    def tk_hata_yakalayici(self, exc, val, tb):
        hata_mesaji = "".join(traceback.format_exception(exc, val, tb))
        with open(os.path.join(_APP_DIR, "HATA_RAPORU_UI.txt"), "w", encoding="utf-8") as f:
            f.write(f"SubtitleForge {surum_metni()}\n\n{hata_mesaji}")
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
                    "g": self.root.winfo_width(),
                    "h": self.root.winfo_height(),
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
                    "sansursuz": self.uncensored.get(),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        self.root.destroy()
        os._exit(0)

    MEDYA_TURLERI = [("Medya Dosyaları",
                      "*.mp4;*.mkv;*.avi;*.mov;*.mp3;*.wav;*.flac;*.ts;*.m4a;*.webm")]

    # ------------------------------------------------------------------
    # KUYRUK
    # ------------------------------------------------------------------

    def _kuyruk_yenile(self):
        """Listeyi ve başlat düğmesinin yazısını kuyruğa göre günceller.
        Yalnızca ana thread'den çağrılmalı (Tk kuralı)."""
        self.liste_kuyruk.delete(0, tk.END)
        for sira, yol in enumerate(self.kuyruk, 1):
            self.liste_kuyruk.insert(tk.END, f"{sira}. {os.path.basename(yol)}")

        adet = len(self.kuyruk)
        self.lbl_kuyruk.config(text="Kuyruk boş" if not adet else f"Kuyrukta {adet} video")

        # İşlem sürerken düğmenin yazısına dokunmuyoruz ("⏳ İşleniyor..." kalsın).
        if self.btn_start["state"] != "disabled":
            self.btn_start.config(
                text="🚀 İŞLEMİ BAŞLAT" if not adet else f"🚀 KUYRUĞU BAŞLAT ({adet} video)")

    def kuyruga_ekle(self):
        """Bir veya birden fazla dosyayı kuyruğa ekler. Aynı dosya iki kez girmez."""
        try:
            yollar = filedialog.askopenfilenames(filetypes=self.MEDYA_TURLERI)
        except Exception as e:
            self.log(f"❌ Dosya Seçme Hatası: {e}")
            return

        eklenen = 0
        for yol in yollar or ():
            if yol and yol not in self.kuyruk:
                self.kuyruk.append(yol)
                eklenen += 1
        if eklenen:
            self.log(f"➕ Kuyruğa {eklenen} video eklendi (toplam {len(self.kuyruk)}).")
        elif yollar:
            self.log("ℹ️ Seçilen dosyalar zaten kuyrukta.")
        self._kuyruk_yenile()

    def kuyruktan_cikar(self):
        secili = list(self.liste_kuyruk.curselection())
        if not secili:
            self.log("ℹ️ Önce kuyruktan çıkarılacak satırı seçin.")
            return
        for i in sorted(secili, reverse=True):     # sondan başa: indeksler kaymasın
            if 0 <= i < len(self.kuyruk):
                self.log(f"➖ Kuyruktan çıkarıldı: {os.path.basename(self.kuyruk[i])}")
                del self.kuyruk[i]
        self._kuyruk_yenile()

    def kuyrugu_temizle(self):
        if not self.kuyruk:
            return
        self.log(f"🗑 Kuyruk temizlendi ({len(self.kuyruk)} video çıkarıldı).")
        self.kuyruk.clear()
        self._kuyruk_yenile()

    def select_file(self):
        try:
            file_path = filedialog.askopenfilename(
                filetypes=self.MEDYA_TURLERI)
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

    def _arayuzu_kilitle(self, kilitli):
        """İşlem sürerken ayarları ve kuyruk düğmelerini kilitler.

        Kuyruk ortasında model/dil değiştirilirse kalan videolar sessizce farklı
        ayarla işleniyordu (ayarlar her videonun başında yeniden okunuyor).
        Kuyruğa ekleme/çıkarma ise çalışan tura hiç yansımıyordu, çünkü liste
        işlem başında kopyalanıyor -- kullanıcı eklediği videoların işleneceğini
        sanıyordu. İkisi de sessiz ve yanıltıcıydı."""
        for w in getattr(self, "_kilitlenecek", ()):
            try:
                if kilitli:
                    w.config(state="disabled")
                else:
                    # Combobox "normal" olursa elle yazılabilir hale gelir.
                    w.config(state="readonly" if isinstance(w, ttk.Combobox) else "normal")
            except Exception:
                pass

    def _on_kontrol(self):
        """İşlem/kuyruk başlamadan ÖNCE bir kez yapılan kontroller.

        Eskiden bunlar her video için ayrı yapılıyordu: FFmpeg yoksa 20 videoluk
        kuyrukta 20 modal hata kutusu üst üste açılıyor, kullanıcı hepsini tek tek
        kapatmak zorunda kalıyordu. Artık en baştan tek kutu."""
        if not self._ffmpeg_coz():
            elle = (self.ffmpeg_path.get() or "").strip()
            self.log("❌ HATA: FFmpeg bulunamadı. Ses FFmpeg ile çözülüyor, onsuz devam edilemez.")
            if elle:
                self.log(f"   Ayarlardaki yol geçersiz: {elle}")
            else:
                self.log("   Kurulum:  winget install Gyan.FFmpeg")
            messagebox.showerror(
                "FFmpeg Bulunamadı",
                "Bu program sesi çözmek için FFmpeg kullanıyor ve sisteminizde bulunamadı.\n\n"
                "Çözüm 1: FFmpeg kurun\n"
                "    winget install Gyan.FFmpeg\n\n"
                "Çözüm 2: Zaten kuruluysa 'FFmpeg Yolu' satırındaki 'Gözat' düğmesiyle\n"
                "ffmpeg.exe dosyasını elle seçin.")
            return False

        if ("distil" in self.model_size.get().lower()
                and self.lang_map.get(self.source_lang.get(), "auto") not in ("en", "auto")):
            self.log("⚠️ KRİTİK UYARI: 'distil' modelleri sadece İNGİLİZCE için eğitilmiştir!")
            messagebox.showerror("Model Uygunsuz",
                                 "'distil-large-v3' modeli sadece İngilizce destekler.")
            return False
        return True

    def start_thread(self):
        # Kuyruk BOŞSA eski davranış: yukarıdaki tek dosya işlenir.
        kuyruk = list(self.kuyruk)
        if not kuyruk and not self.video_path.get():
            self.log("⚠️ HATA: Lütfen bir video dosyası seçin ya da kuyruğa ekleyin!")
            return

        # Ön kontroller tek seferde; geçemezse hiç başlamıyoruz.
        if not self._on_kontrol():
            return

        self.is_cancelled = False
        self._arayuzu_kilitle(True)
        self.btn_start.config(state="disabled", text="⏳ İşleniyor...", bg="#555555", fg="white")
        self.btn_cancel.config(state="normal", text="🛑 İPTAL ET", bg="#D13438")

        if kuyruk:
            # Liste kopyalanıyor: kuyruk düğmeleri işlem boyunca kilitli olsa da
            # (bkz. _arayuzu_kilitle) çalışan tur kendi anlık görüntüsüyle ilerlesin.
            threading.Thread(target=self._kuyrugu_isle, args=(kuyruk,), daemon=True).start()
        else:
            threading.Thread(target=self.run_process, daemon=True).start()

    def _kuyrugu_isle(self, kuyruk):
        """Kuyruktaki videoları sırayla işler (worker thread).

        Model önbelleğe alındığı için (bkz. _modeli_hazirla) ikinci videodan
        itibaren model yükleme maliyeti yok; ayarlar değişmediği sürece tek
        yükleme bütün kuyruğa yetiyor."""
        toplam = len(kuyruk)
        basarili, basarisiz = 0, []
        t_kuyruk = time.time()

        self.log("=" * 45)
        self.log(f"📋 KUYRUK BAŞLADI — {toplam} video sırayla işlenecek.")

        for sira, video in enumerate(kuyruk, 1):
            if self.is_cancelled:
                break
            self.log("")
            self.log("=" * 45)
            self.log(f"📼 [{sira}/{toplam}] {os.path.basename(video)}")
            self.log("=" * 45)
            try:
                if self.run_process(video_file=video, kuyruk_modu=True):
                    basarili += 1
                elif not self.is_cancelled:
                    basarisiz.append(os.path.basename(video))
            except Exception as e:
                # Tek bir videonun patlaması kuyruğun geri kalanını düşürmemeli.
                self._ceviri_son_hata = str(e)
                self.log(f"❌ [{sira}/{toplam}] Bu video işlenemedi: {e}")
                basarisiz.append(os.path.basename(video))

        atlanan = toplam - basarili - len(basarisiz)
        self.log("")
        self.log("=" * 45)
        if self.is_cancelled:
            self.log(f"🛑 KUYRUK DURDURULDU — {basarili}/{toplam} video tamamlandı "
                     f"({self._sure_metni(time.time() - t_kuyruk)}).")
        else:
            self.log(f"🏁 KUYRUK BİTTİ — {basarili}/{toplam} video başarılı "
                     f"({self._sure_metni(time.time() - t_kuyruk)}).")
        if basarisiz:
            self.log(f"   ❌ Başarısız: {', '.join(basarisiz)}")
        if atlanan > 0 and self.is_cancelled:
            self.log(f"   ⏭️ İşlenmeyen: {atlanan} video")

        def bitis():
            self._arayuzu_kilitle(False)
            self.btn_start.config(state="normal", bg="#0078D4", fg="white")
            self.btn_cancel.config(state="disabled", text="🛑 İPTAL ET", bg="#D13438")
            self._kuyruk_yenile()          # düğme yazısını geri koyar
            if self.is_cancelled:
                messagebox.showwarning("Kuyruk Durduruldu",
                                       f"🛑 {basarili}/{toplam} video tamamlandı.")
            elif basarisiz:
                messagebox.showwarning("Kuyruk Bitti",
                                       f"{basarili}/{toplam} video başarılı.\n\n"
                                       f"Başarısız: {', '.join(basarisiz)}")
            else:
                messagebox.showinfo("Kuyruk Bitti",
                                    f"🎉 {toplam} videonun tamamı işlendi!")
        self.root.after(0, bitis)

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
        """Uzun bir altyazı satırını dengeli biçimde en fazla 3 satıra böler.
        Bölme yalnızca dosyaya yazarken yapılıyor: metin bellekte tek satır kalırsa
        Google'a paket halinde gönderilen çeviri hizalaması bozulmuyor.

        Neden 2 değil 3: Türkçe çeviri kaynak metinden %20-30 uzayabiliyor. 75
        karakterlik bir İngilizce blok 100+ karaktere çıkınca iki satıra sıkıştırmak
        ekranda 50'şer karakterlik, okunması zor satırlar üretiyordu."""
        metin = " ".join(metin.split())
        if len(metin) <= SATIR_MAX_KARAKTER:
            return metin

        kelimeler = metin.split(" ")
        if len(kelimeler) < 2:
            return metin

        satir_sayisi = min(3, max(2, -(-len(metin) // SATIR_MAX_KARAKTER)))
        satir_sayisi = min(satir_sayisi, len(kelimeler))
        hedef = len(metin) / satir_sayisi

        # Her satırı hedef uzunluğa en yakın yerde kapatıyoruz: kelime eklemek
        # satırı hedeften UZAKLAŞTIRIYORSA orada kesiyoruz.
        satirlar, mevcut = [], []
        for kelime in kelimeler:
            if not mevcut:
                mevcut.append(kelime)
                continue
            simdiki = " ".join(mevcut)
            aday = f"{simdiki} {kelime}"
            son_satir = len(satirlar) == satir_sayisi - 1
            if not son_satir and abs(len(aday) - hedef) > abs(len(simdiki) - hedef):
                satirlar.append(simdiki)
                mevcut = [kelime]
            else:
                mevcut.append(kelime)
        if mevcut:
            satirlar.append(" ".join(mevcut))

        return "\n".join(satirlar)

    def _zamanlari_duzelt(self, bloklar):
        """Sıfır/negatif süreli ve üst üste binen blokları düzeltir.

        whisperx komşu segmentleri bazen çakışan zamanlarla üretiyor; hizalama da
        aynı damgayı iki kelimeye verebiliyor. Oynatıcılar böyle blokları ya hiç
        göstermiyor ya da bir öncekini anında eziyor -- ekranda 'atlanmış' gibi
        görünen altyazıların bir kısmı buradan geliyordu."""
        MIN_SURE = 0.05
        onceki_son = None
        for blok in bloklar:
            bas = self._sayi(blok.get("start")) or 0.0
            son = self._sayi(blok.get("end"))
            if onceki_son is not None and bas < onceki_son:
                bas = onceki_son
            if son is None or son < bas + MIN_SURE:
                son = bas + MIN_SURE
            blok["start"], blok["end"] = bas, son
            onceki_son = son
        return bloklar

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
        """SRT dosyasını ATOMİK yazar; yazılan satır varsa True döner.

        Eskiden hedef dosya doğrudan "w" ile açılıyordu: açıldığı anda içerik
        siliniyordu. Videodan hiç konuşma çıkmadığında (ya da yazma yarıda
        kaldığında) kullanıcının aynı isimli ESKİ altyazısı sıfır bayta iniyordu.
        Artık önce geçici dosyaya yazılıyor, ancak dolu bir içerik hazır olduğunda
        os.replace ile yerine taşınıyor. Tek satır bile çıkmazsa hedefe hiç
        dokunulmuyor ve False dönüyor."""
        gecici = yol + ".yeni"
        sira = 0
        try:
            with open(gecici, "w", encoding="utf-8") as f:
                for i, blok in enumerate(bloklar):
                    metin = metinler.get(i, blok["text"]) if metinler is not None else blok["text"]
                    metin = self._satir_kir(metin or "")
                    if not metin.strip():
                        continue      # boş blok SRT'yi bozuyor, numarayı da harcamamalı
                    sira += 1
                    bas = self.format_timestamp(blok["start"])
                    son = self.format_timestamp(blok["end"])
                    f.write(f"{sira}\n{bas} --> {son}\n{metin}\n\n")
        except Exception:
            self._gecici_sil(gecici)
            raise

        if sira == 0:
            self._gecici_sil(gecici)
            return False

        os.replace(gecici, yol)      # aynı diskte atomik: yarım dosya kalmaz
        return True

    @staticmethod
    def _gecici_sil(yol):
        try:
            os.remove(yol)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ÇEVİRİ YARDIMCILARI
    # ------------------------------------------------------------------

    def _satir_gruplari(self, indeksler, metinler):
        """Verilen satır indekslerini, karakter/satır sınırını aşmayan paketlere böler."""
        grup, uzunluk = [], 0
        for i in indeksler:
            satir_uzunlugu = len(metinler[i]) + 1
            if grup and (uzunluk + satir_uzunlugu > CEVIRI_MAX_KARAKTER or len(grup) >= CEVIRI_MAX_SATIR):
                yield grup
                grup, uzunluk = [], 0
            grup.append(i)
            uzunluk += satir_uzunlugu
        if grup:
            yield grup

    def _cumle_gruplari(self, bloklar):
        """Ardışık blokları cümle bütünlüğüne göre gruplar; blok indeks listeleri üretir.

        Blok kurucu (`_bloklari_kur`) cümleyi 75 karakterde ve kelimeler arası 1 sn
        sessizlikte kesiyor. Ortaya çıkan parça tek başına çevrilince anlam bozuluyor
        (bkz. CUMLE_MAX_BLOK yanındaki not), bu yüzden çeviri birimi blok değil cümle.
        Zamanlamalara ve blok yapısına dokunulmuyor; yalnızca hangi blokların birlikte
        çevrileceği belirleniyor."""
        grup = []
        uzunluk = 0
        onceki_bit = None

        for i, blok in enumerate(bloklar):
            metin = " ".join((blok.get("text") or "").split())

            # "♪", "...", "- -" gibi satırlar zaten çeviriye gitmiyor
            # (bkz. _cevrilmeye_deger). Cümleye karıştırılırlarsa geri dağıtım
            # sırasında konuşmanın içine sızıyorlar; kendi gruplarında bırakılıyorlar.
            if not self._cevrilmeye_deger(metin):
                if grup:
                    yield grup
                    grup, uzunluk = [], 0
                yield [i]
                onceki_bit = blok["end"]
                continue

            # Uzun sessizlik varsa yeni bir replik başlamıştır; öncekiyle birleştirme.
            if (grup and onceki_bit is not None
                    and (blok["start"] - onceki_bit) > CUMLE_MAX_BOSLUK):
                yield grup
                grup, uzunluk = [], 0

            grup.append(i)
            uzunluk += len(metin) + 1
            onceki_bit = blok["end"]

            # Cümle bitti mi? Kapanış tırnak/parantezlerini kırpıp bakıyoruz.
            # ("…" kırpılmıyor: kendisi bir cümle sonu işareti.)
            son = metin.rstrip("\"'»)]}").rstrip()
            cumle_bitti = son.endswith((".", "?", "!", "…", ":", "؟"))

            if cumle_bitti or len(grup) >= CUMLE_MAX_BLOK or uzunluk >= CUMLE_MAX_KARAKTER:
                yield grup
                grup, uzunluk = [], 0

        if grup:
            yield grup

    @staticmethod
    def _ceviriyi_dagit(ceviri, orijinal_metinler):
        """Bir cümlenin çevirisini, geldiği bloklara orijinal karakter oranına göre
        kelime sınırlarından geri dağıtır. Zamanlamalar korunur.

        Kelime sayısı blok sayısından azsa None döner -- o grup için çağıran taraf blok
        blok çeviriye düşer, çünkü boş altyazı satırı bırakmak kabul edilemez."""
        kelimeler = (ceviri or "").split()
        n = len(orijinal_metinler)

        if n == 1:
            return [ceviri]
        if len(kelimeler) < n:
            return None

        toplam = sum(len(m) for m in orijinal_metinler) or 1
        sonuc = []
        kalan = kelimeler

        for i, metin in enumerate(orijinal_metinler):
            if i == n - 1:
                sonuc.append(" ".join(kalan))
                break
            adet = max(1, round(len(kelimeler) * len(metin) / toplam))
            adet = min(adet, len(kalan) - (n - 1 - i))   # kalan bloklara en az birer kelime
            sonuc.append(" ".join(kalan[:adet]))
            kalan = kalan[adet:]

        return sonuc

    @staticmethod
    def _cevrilmeye_deger(metin):
        """Sadece noktalama/nota işareti içeren satırlar ("...", "- -", "♪") çeviriye
        gönderilmiyor: Google bunları yutup paketten düşürüyor ve satır hizası kayıyordu."""
        return bool(metin.strip()) and any(ch.isalnum() for ch in metin)

    def _tek_satir_cevir(self, metin, kaynak_dil, yedek_kullan=True):
        """Tek satırı çevirir. Kalıcı olarak başarısız olursa None döner
        (çağıran taraf o satırda orijinal metni korur)."""
        temiz = metin.strip()
        if not self._cevrilmeye_deger(temiz):
            return metin

        try:
            sonuc = self._cevirici.cevir(temiz, kaynak_dil)
            if sonuc.strip():
                return sonuc.strip()
        except KullaniciIptali:
            raise
        except CeviriHatasi:
            pass

        if not yedek_kullan or self.is_cancelled:
            return None
        return self._cevirici.cevir_yedek(temiz, kaynak_dil)

    def _grup_cevir(self, grup, satirlar, kaynak_dil, sonuclar):
        """Bir paketi tek istekte çevirip `sonuclar` sözlüğüne {indeks: çeviri} yazar.

        Google bazen satırları birleştirip/bölerek farklı sayıda satır döndürüyor.
        Sayı tutmazsa hizalama kayar (o paketten sonraki TÜM altyazılar yanlış metinle
        eşleşir), bu yüzden yalnızca birebir eşleşme kabul ediliyor. Eşleşmezse paket
        İKİYE BÖLÜNÜP tekrar deneniyor: 40 satırlık bir pakette tek bir sorunlu satır
        için 40 ayrı istek atmak yerine ~10 istekle sorunlu satır izole ediliyor.

        Ağ/limit hatasında bölmek işe yaramaz (istek sayısını katlar, 429'u besler);
        o paket olduğu gibi bırakılıp onarım turuna devrediliyor."""
        if not grup:
            return

        metinler = [satirlar[i] for i in grup]
        hizasiz = False
        try:
            cevrilmis = self._cevirici.cevir("\n".join(metinler), kaynak_dil)
            parcalar = cevrilmis.split("\n")
            if len(parcalar) == len(grup):
                for i, parca in zip(grup, parcalar):
                    parca = parca.strip()
                    sonuclar[i] = parca if parca else satirlar[i]
                return
            hizasiz = True
        except KullaniciIptali:
            raise
        except CeviriHatasi as e:
            self._ceviri_son_hata = str(e)

        if not hizasiz:
            return                      # ağ/limit sorunu → onarım turu tekrar dener

        if len(grup) == 1:
            ceviri = self._tek_satir_cevir(satirlar[grup[0]], kaynak_dil)
            if ceviri is not None:
                sonuclar[grup[0]] = ceviri
            return

        orta = len(grup) // 2
        self._grup_cevir(grup[:orta], satirlar, kaynak_dil, sonuclar)
        self._grup_cevir(grup[orta:], satirlar, kaynak_dil, sonuclar)

    def _bloklari_cevir(self, bloklar, kaynak_dil):
        """Altyazı bloklarını Türkçeye çevirir.

        {blok indeksi: çeviri} sözlüğü döner. Sözlükte yer almayan indeksler kalıcı
        olarak çevrilememiş demektir; çağıran taraf o satırlarda orijinal metni bırakıyor.

        Çeviri birimi blok DEĞİL cümledir: bölücü cümleyi karakter sınırında kestiği
        için parça parça çeviri anlamı bozuyordu (bkz. CUMLE_MAX_BLOK yanındaki not).
        Bloklar cümlelere gruplanıp öyle çevriliyor, sonuç bloklara geri dağıtılıyor.

        Akış: cümle gruplama → paketleme → eşzamanlı çeviri → eksik kalanlar için onarım
        turları → sansür denetimi → bloklara geri dağıtma. Onarım turları olmadan,
        geçici bir ağ/limit hatasına denk gelen paketteki bütün satırlar sessizce
        çevrilmemiş kalıyordu."""
        self._cevirici = GoogleCevirici(iptal_kontrolu=lambda: self.is_cancelled, log=self.log)
        self._ceviri_son_hata = None
        t_ceviri = time.time()

        # Satır içi satır sonu paket hizasını bozar; blok kurucu üretmiyor ama
        # çeviriye giren metni yine de tek satıra indirgiyoruz.
        blok_metinleri = [" ".join((b.get("text") or "").split()) for b in bloklar]

        # Blokları cümle bütünlüğüne göre grupla; bundan sonrası CÜMLE indeksleriyle
        # çalışıyor, blok indekslerine ancak en sonda geri dönülüyor.
        cumle_gruplari = list(self._cumle_gruplari(bloklar))
        satirlar = [" ".join(blok_metinleri[i] for i in grup) for grup in cumle_gruplari]

        # Yalnızca noktalama içeren satırları ("...", "♪", "- -") baştan ayırıyoruz:
        # Google bunları yutup paketten düşürüyor, satır hizası da kayıyordu.
        ceviriler = {}
        cevrilecek = []
        for i, satir in enumerate(satirlar):
            if self._cevrilmeye_deger(satir):
                cevrilecek.append(i)
            else:
                ceviriler[i] = satir

        gruplar = list(self._satir_gruplari(cevrilecek, satirlar))
        if not gruplar:
            return self._bloklara_dagit(cumle_gruplari, ceviriler, blok_metinleri, kaynak_dil)

        self.log(f"   {len(bloklar)} satır → {len(satirlar)} cümle; {len(cevrilecek)} cümle "
                 f"{len(gruplar)} pakette gönderilecek ({CEVIRI_ISCI_SAYISI} eşzamanlı istek).")

        def grup_isi(grup):
            yerel = {}
            self._grup_cevir(grup, satirlar, kaynak_dil, yerel)
            return yerel

        tamamlanan = 0
        futures = []
        executor = ThreadPoolExecutor(max_workers=CEVIRI_ISCI_SAYISI)
        try:
            futures = [executor.submit(grup_isi, g) for g in gruplar]
            for future in as_completed(futures):
                # Sonucu İPTAL KONTROLÜNDEN ÖNCE topluyoruz: as_completed bu paketi
                # zaten bitmiş olduğu için verdi, önce iptale bakıp break edersek
                # tamamlanmış bir paketin çevirisi çöpe gidiyordu.
                # Ayrıca tek bir paketin patlaması eskiden run_process'i "KRİTİK
                # HATA"ya düşürüp o ana kadarki BÜTÜN çevirileri kaybettiriyordu.
                try:
                    ceviriler.update(future.result())
                except KullaniciIptali:
                    pass
                except Exception as e:
                    self._ceviri_son_hata = str(e)
                    self.log(f"   ⚠️ Bir çeviri paketi hata verdi ({e}); onarım turunda tekrar denenecek.")

                tamamlanan += 1
                yuzde = tamamlanan * 100 / len(gruplar)
                # Paket SAYISI denemeyi ölçüyor, başarıyı değil: uç nokta her pakete
                # 429 verdiğinde ekranda "%100 (12/12 paket)" yazıp tek satır bile
                # çevrilmemiş olabiliyordu. Gerçek çeviri sayısını da gösteriyoruz.
                cevrilen = sum(1 for i in cevrilecek if i in ceviriler)
                self.ilerleme(yuzde, f"Çeviri: %{yuzde:.0f} ({cevrilen}/{len(cevrilecek)} cümle)")
                if tamamlanan % 5 == 0 or tamamlanan == len(gruplar):
                    self.log(f"   🌐 Çeviri: %{yuzde:.0f}  ({tamamlanan}/{len(gruplar)} paket, "
                             f"{cevrilen}/{len(cevrilecek)} cümle çevrildi, "
                             f"{self._sure_metni(time.time() - t_ceviri)})")

                if self.is_cancelled:
                    for bekleyen in futures:
                        bekleyen.cancel()
                    break
        finally:
            executor.shutdown(wait=True)

        # İptalde kuyruk boşaltılırken çalışmakta olan paketler yine de bitiyor;
        # okunmadan bırakılan sonuçları burada topluyoruz.
        for bekleyen in futures:
            if bekleyen.done() and not bekleyen.cancelled():
                try:
                    ceviriler.update(bekleyen.result())
                except Exception:
                    pass

        # --- ONARIM TURLARI ---
        # Ana geçişte ağ/istek limiti yüzünden düşen satırlar burada tekrar deneniyor.
        # Eskiden bu satırlar sessizce orijinal diliyle dosyaya yazılıyordu;
        # "bazı cümleler çevrilmemiş" şikâyetinin ikinci sebebi buydu.
        for tur in range(1, CEVIRI_ONARIM_TURU + 1):
            if self.is_cancelled:
                break
            eksik = [i for i in cevrilecek if i not in ceviriler]
            if not eksik:
                break

            # ERKEN ÇIKIŞ: birincil uç nokta topluca limit veriyorsa onarım turu
            # aynı duvara tekrar tekrar toslar. Ölçülen bir çalıştırmada 3 tur x 45
            # grup sırayla dönüp saatler harcamış, tek satır bile kurtaramamıştı.
            # Doğrudan yedek yola geçiyoruz.
            if self._cevirici.limit_asildi:
                self.log("   ⏭️ Birincil uç nokta istek limitinde; onarım turları atlanıyor, "
                         "kalan satırlar yedek yoldan çevrilecek.")
                break

            self.log(f"   🔁 Onarım turu {tur}/{CEVIRI_ONARIM_TURU}: {len(eksik)} satır tekrar deneniyor...")
            try:
                # Limit yediysek bir soluklanma; her turda biraz daha uzun.
                self._cevirici.bekle(min(3.0 * tur, 12.0))
            except KullaniciIptali:
                break

            # Onarım turu eskiden SIRAYLA dönüyordu; ana geçiş 3 işçi kullanırken
            # burada tek iş parçacığı vardı. Birkaç satır düştüğünde fark etmiyordu
            # ama her şey düştüğünde (429 fırtınası) dakikalar yerine saatler sürüyordu.
            kucuk_gruplar = [eksik[j:j + 10] for j in range(0, len(eksik), 10)]

            def onarim_isi(kucuk):
                # Ana geçişteki gibi YEREL sözlüğe yazıp sonra birleştiriyoruz:
                # paylaşılan sözlüğe birden fazla iş parçacığından yazmıyoruz.
                yerel = {}
                self._grup_cevir(kucuk, satirlar, kaynak_dil, yerel)
                return yerel

            tamam = 0
            onarim_havuzu = ThreadPoolExecutor(max_workers=CEVIRI_ISCI_SAYISI)
            onarim_isleri = []
            try:
                onarim_isleri = [onarim_havuzu.submit(onarim_isi, k) for k in kucuk_gruplar]
                for onarim in as_completed(onarim_isleri):
                    try:
                        ceviriler.update(onarim.result())
                    except KullaniciIptali:
                        pass
                    except Exception as e:
                        self._ceviri_son_hata = str(e)
                    tamam += 1
                    yuzde = tamam * 100 / len(kucuk_gruplar)
                    self.ilerleme(yuzde, f"Çeviri onarımı {tur}: %{yuzde:.0f} "
                                         f"({tamam}/{len(kucuk_gruplar)} grup)")
                    if self.is_cancelled:
                        for bekleyen in onarim_isleri:
                            bekleyen.cancel()
                        break
            finally:
                onarim_havuzu.shutdown(wait=True)

            # İptalde/erken çıkışta okunmadan kalan sonuçlar da toplanıyor.
            for bekleyen in onarim_isleri:
                if bekleyen.done() and not bekleyen.cancelled():
                    try:
                        ceviriler.update(bekleyen.result())
                    except Exception:
                        pass

        # --- YEDEK YOL ---
        # Birincil uç nokta kapandıysa (ya da onarım turları yetmediyse) kalan
        # satırlar deep_translator'ın /m uç noktasından tek tek çevriliyor. O yol
        # satır sonlarını sildiği için toplu gönderilemiyor, ama eşzamanlı
        # çalıştırınca 450 satır birkaç dakikada bitiyor -- saatler yerine.
        kalan = [i for i in cevrilecek if i not in ceviriler]
        if kalan and not self.is_cancelled:
            self._yedek_yoldan_cevir(kalan, satirlar, kaynak_dil, ceviriler)

        # --- ÜÇÜNCÜ BASAMAK: YEREL ÇEVİRİ ---
        # Google'ın iki yolu da veremediyse makinede çeviriyoruz. Ağa çıkmadığı
        # için kota/429 diye bir sorunu yok; tek bedeli süre.
        kalan = [i for i in cevrilecek if i not in ceviriler]
        if kalan and not self.is_cancelled:
            self._yerelden_cevir(kalan, satirlar, kaynak_dil, ceviriler)

        # --- SANSÜR DENETİMİ ---
        # Birincil uç nokta kapalıysa atlanıyor: sansürü kaldıran tek yol o uç nokta
        # (yedek /m yolu küfürleri yıldızlıyor), kapalıyken denemek boşuna.
        if self._cevirici.limit_asildi:
            pass
        elif self.uncensored.get() and not self.is_cancelled:
            try:
                self._sansuru_onar(ceviriler, satirlar, kaynak_dil)
            except KullaniciIptali:
                pass

        basarili = sum(1 for i in cevrilecek if i in ceviriler)
        if self._cevirici.limit_asildi:
            self.log("   🚫 Google'ın birincil çeviri uç noktası bu IP'ye istek limiti uyguladı "
                     "(HTTP 429). Genellikle 30-60 dakika içinde açılıyor.")
        self.log(f"   📊 {basarili}/{len(cevrilecek)} cümle çevrildi "
                 f"({self._cevirici.istek_sayisi} istek, {self._sure_metni(time.time() - t_ceviri)}).")

        # --- ÇEVİRİYİ BLOKLARA GERİ DAĞIT ---
        return self._bloklara_dagit(cumle_gruplari, ceviriler, blok_metinleri, kaynak_dil)

    def _yedek_yoldan_cevir(self, eksik, satirlar, kaynak_dil, ceviriler):
        """Kalan satırları deep_translator'ın /m uç noktasından TEK TEK çevirir.

        Birincil uç nokta (translate_a/single) bu IP'ye topluca 429 vermeye
        başladığında çalışan tek yol bu -- ölçüldü: üç birincil uç nokta da 429
        verirken /m sorunsuz çeviriyordu. O yol satır sonlarını sildiği için toplu
        gönderilemiyor; onun yerine eşzamanlı çalıştırıyoruz, 450 satır birkaç
        dakikada bitiyor.

        Arka arkaya CEVIRI_LIMIT_ESIGI kadar başarısızlık gelirse bu yol da
        kapanmış demektir; boşuna sürdürmek yerine bırakılıyor."""
        self.log(f"   🛟 Yedek yol: {len(eksik)} satır tek tek çevriliyor "
                 f"({CEVIRI_YEDEK_ISCI} eşzamanlı istek)...")
        t_yedek = time.time()
        kilit = threading.Lock()
        durum = {"ardisik_hata": 0, "pes": False}
        verimsiz = 0          # arka arkaya tek satır bile kurtaramayan tur sayısı

        for yedek_tur in range(1, CEVIRI_YEDEK_TURU + 1):
            bu_tur = [i for i in eksik if i not in ceviriler]
            if not bu_tur or durum["pes"] or self.is_cancelled:
                break
            if yedek_tur > 1:
                self.log(f"   🔁 Yedek yol turu {yedek_tur}/{CEVIRI_YEDEK_TURU}: "
                         f"{len(bu_tur)} satır tekrar deneniyor...")
                try:
                    self._cevirici.bekle(CEVIRI_YEDEK_BEKLEME * (yedek_tur - 1))
                except KullaniciIptali:
                    break
                # Yeni tur temiz sayfa: önceki turun hataları pes ettirmesin.
                with kilit:
                    durum["ardisik_hata"] = 0
            onceki = sum(1 for i in eksik if i in ceviriler)
            self._yedek_tek_gecis(bu_tur, satirlar, kaynak_dil, ceviriler,
                                  kilit, durum, t_yedek)
            # Bir tur HİÇBİR satır kurtaramadıysa bu yol kapalı demektir; tekrar
            # denemek sadece zaman yakıyor (ölçüldü: 4 tur x ~15 sn = 1 dakika,
            # sıfır kazanç). Kısmi başarı varsa devam ediyoruz, çünkü /m tek tük
            # satırı geçici reddedip sonraki turda veriyor.
            if sum(1 for i in eksik if i in ceviriler) == onceki:
                # Tek verimsiz tur yetmiyor: /m bazen bir turun TAMAMINI reddedip
                # sonrakinde sorunsuz veriyor. Ama arka arkaya iki tur hiçbir şey
                # getirmiyorsa yol kapalı demektir; kalan turları harcamıyoruz.
                verimsiz += 1
                if verimsiz >= 2:
                    self.log("   ⏭️ Yedek yol arka arkaya iki turda hiçbir satır "
                             "çeviremedi; kalan turlar atlanıyor.")
                    break
            else:
                verimsiz = 0

        if durum["pes"]:
            self.log(f"   ⚠️ Yedek yol da arka arkaya {CEVIRI_LIMIT_ESIGI} kez başarısız oldu; "
                     f"çeviri burada bırakıldı. Kalan satırlarda orijinal metin kalacak.")
        kalan_sayi = sum(1 for i in eksik if i not in ceviriler)
        if kalan_sayi:
            self.log(f"   ⚠️ {kalan_sayi} satır yedek yoldan da çevrilemedi; "
                     f"o satırlarda orijinal metin kalacak.")

    def _yedek_tek_gecis(self, eksik, satirlar, kaynak_dil, ceviriler, kilit, durum, t_yedek):
        """Yedek yolun tek turu: verilen satırları eşzamanlı olarak /m'den çevirir."""

        def bir_satir(i):
            if self.is_cancelled or durum["pes"]:
                return i, None
            try:
                ceviri = self._cevirici.cevir_yedek(satirlar[i], kaynak_dil)
            except Exception:
                ceviri = None
            with kilit:
                if ceviri:
                    durum["ardisik_hata"] = 0
                else:
                    durum["ardisik_hata"] += 1
                    if durum["ardisik_hata"] >= CEVIRI_LIMIT_ESIGI:
                        durum["pes"] = True
            return i, ceviri

        tamam = 0
        havuz = ThreadPoolExecutor(max_workers=CEVIRI_YEDEK_ISCI)
        isler = []
        try:
            isler = [havuz.submit(bir_satir, i) for i in eksik]
            for is_sonucu in as_completed(isler):
                try:
                    indeks, ceviri = is_sonucu.result()
                    if ceviri:
                        ceviriler[indeks] = ceviri
                except Exception as e:
                    self._ceviri_son_hata = str(e)
                tamam += 1
                yuzde = tamam * 100 / len(eksik)
                self.ilerleme(yuzde, f"Yedek çeviri: %{yuzde:.0f} ({tamam}/{len(eksik)} satır)")
                if tamam % 50 == 0 or tamam == len(eksik):
                    self.log(f"   🛟 Yedek yol: {tamam}/{len(eksik)} satır "
                             f"({self._sure_metni(time.time() - t_yedek)})")
                if self.is_cancelled or durum["pes"]:
                    for bekleyen in isler:
                        bekleyen.cancel()
                    break
        finally:
            havuz.shutdown(wait=True)

        # Kuyruk boşaltılırken biten ama okunmayan sonuçlar çöpe gitmesin.
        for bekleyen in isler:
            if bekleyen.done() and not bekleyen.cancelled():
                try:
                    indeks, ceviri = bekleyen.result()
                    if ceviri:
                        ceviriler[indeks] = ceviri
                except Exception:
                    pass


    def _yerelden_cevir(self, eksik, satirlar, kaynak_dil, ceviriler):
        """Kalan satırları makinedeki Opus-MT modeliyle çevirir (üçüncü basamak).

        Google'ın iki uç noktası da istek limiti verdiğinde çalışan tek yol bu.
        Model yoksa/yüklenemezse sessizce dönülüyor: yerel çeviri bir iyileştirme,
        zorunluluk değil."""
        if getattr(self, "_yerel", None) is None:
            self._yerel = YerelCevirici(log=self.log,
                                        iptal_kontrolu=lambda: self.is_cancelled)
        if not self._yerel.hazirla(kaynak_dil):
            return

        self.log(f"   💻 Yerel çeviri: {len(eksik)} satır makinede çevriliyor "
                 f"(internet gerekmiyor)...")
        t_yerel = time.time()
        try:
            sonuclar = self._yerel.cevir_liste(
                [satirlar[i] for i in eksik],
                ilerleme=lambda o: self.ilerleme(o * 100, f"Yerel çeviri: %{o * 100:.0f}"))
        except KullaniciIptali:
            return
        except Exception as e:
            self._ceviri_son_hata = str(e)
            self.log(f"   ⚠️ Yerel çeviri hata verdi ({e}); atlanıyor.")
            return

        eklenen = 0
        for i, ceviri in zip(eksik, sonuclar):
            if ceviri and ceviri.strip():
                ceviriler[i] = ceviri.strip()
                eklenen += 1
        self.log(f"   💻 Yerel çeviri: {eklenen}/{len(eksik)} satır çevrildi "
                 f"({self._sure_metni(time.time() - t_yerel)}).")

    def _bloklara_dagit(self, cumle_gruplari, cumle_ceviriler, blok_metinleri, kaynak_dil):
        """Cümle çevirilerini geldikleri bloklara geri dağıtır; {blok: metin} döner.

        Çevirisi blok sayısından az kelime içeren gruplar dağıtılamıyor; onlar için eski
        yönteme, blok blok çeviriye düşülüyor. Boş altyazı satırı bırakmak kabul edilemez."""
        blok_ceviriler = {}
        yedek_bloklar = []

        for cumle_idx, grup in enumerate(cumle_gruplari):
            ceviri = cumle_ceviriler.get(cumle_idx)
            if ceviri is None:
                continue                      # çevrilemedi → orijinal metin kalır

            dagitim = self._ceviriyi_dagit(ceviri, [blok_metinleri[i] for i in grup])
            if dagitim is None:
                yedek_bloklar.extend(grup)
                continue

            for blok_idx, metin in zip(grup, dagitim):
                blok_ceviriler[blok_idx] = metin

        if yedek_bloklar and not self.is_cancelled:
            self.log(f"   ↩️ {len(yedek_bloklar)} satır tek tek çevriliyor "
                     f"(cümle çevirisi bloklara bölünemedi).")
            for paket in self._satir_gruplari(yedek_bloklar, blok_metinleri):
                if self.is_cancelled:
                    break
                try:
                    self._grup_cevir(paket, blok_metinleri, kaynak_dil, blok_ceviriler)
                except KullaniciIptali:
                    break
                except Exception as e:
                    self._ceviri_son_hata = str(e)

            # _grup_cevir YALNIZCA birincil uç noktayı kullanıyor. O yol kapalıyken
            # (429 duvarı) anında düşüp hiçbir şey üretmiyordu ve bu bloklar sessizce
            # orijinal diliyle kalıyordu -- "bazı cümleleri atlıyor" şikâyetinin
            # sebebi buydu. Üstelik tam da düzeltmeye çalıştığımız parçalanmış
            # cümlelere denk geliyor: Türkçe eklemeli bir dil olduğu için çeviri
            # sık sık blok sayısından az kelime içeriyor ("I do not know why" ->
            # "Bilmiyorum") ve dağıtım başarısız oluyor.
            # Kalanları çalışan yedek yoldan (deep_translator /m) tek tek çeviriyoruz.
            kalan = [i for i in yedek_bloklar if i not in blok_ceviriler]
            if kalan and not self.is_cancelled:
                self._yedek_yoldan_cevir(kalan, blok_metinleri, kaynak_dil, blok_ceviriler)

            # Yedek yol da veremediyse üçüncü basamak: yerel model.
            kalan = [i for i in yedek_bloklar if i not in blok_ceviriler]
            if kalan and not self.is_cancelled:
                self._yerelden_cevir(kalan, blok_metinleri, kaynak_dil, blok_ceviriler)

        return blok_ceviriler

    def _sansuru_onar(self, ceviriler, satirlar, kaynak_dil):
        """Yedek yoldan (deep_translator /m) gelen satırlarda küfürler bazen
        yıldızlanmış oluyor ("s**tir"). Kaynakta yıldız yokken çeviride varsa satır,
        sansür uygulamayan translate_a/single uç noktasından tekrar isteniyor."""
        supheli = [i for i, ceviri in ceviriler.items()
                   if SANSUR_DESENI.search(ceviri) and not SANSUR_DESENI.search(satirlar[i])]
        if not supheli:
            return

        self.log(f"   🔞 {len(supheli)} satırda yıldızlanmış (sansürlü) çeviri bulundu, tekrar isteniyor...")
        duzelen = 0
        for i in supheli:
            if self.is_cancelled:
                break
            # yedek_kullan=False: sansürü zaten yedek yol getirdi, oraya geri dönmüyoruz.
            yeni = self._tek_satir_cevir(satirlar[i], kaynak_dil, yedek_kullan=False)
            if yeni and not SANSUR_DESENI.search(yeni):
                ceviriler[i] = yeni
                duzelen += 1
        self.log(f"   🔞 {duzelen}/{len(supheli)} satırdaki sansür kaldırıldı.")

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

    def _istem_ayarla(self, istem):
        """Yüklü modelin initial_prompt'unu modeli YENİDEN YÜKLEMEDEN değiştirir.
        whisperx'in kendisi de suppress_tokens'ı aynı yöntemle (dataclasses.replace)
        değiştiriyor, yani desteklenen bir müdahale. Model büyük ve CPU'da yeniden
        yüklemek dakikalar sürebildiği için bu yol tercih ediliyor."""
        secenekler = getattr(self.pipeline, "options", None)
        if secenekler is None:
            return False
        try:
            if hasattr(secenekler, "_replace"):                     # NamedTuple sürümleri
                self.pipeline.options = secenekler._replace(initial_prompt=istem)
            else:
                from dataclasses import replace as _degistir
                self.pipeline.options = _degistir(secenekler, initial_prompt=istem)
            return True
        except Exception:
            try:
                secenekler.initial_prompt = istem
                return True
            except Exception:
                return False

    def _sansursuz_istem(self, dil, sansursuz):
        """Whisper'a metnin birebir yazılacağını söyleyen kısa istemi seçer.

        İstem MODELİN YAZACAĞI DİLDE olmalı: yabancı dilde bir istem modeli o dile
        kaydırıp altyazıyı tamamen bozuyor. Karşılığı olmayan dillerde istem
        verilmiyor (None), böylece hiç değilse doğru dilde çıktı garanti."""
        if not sansursuz:
            return None
        return SANSURSUZ_ISTEMLER.get((dil or "").lower())

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

    def run_process(self, video_file=None, kuyruk_modu=False):
        """Tek bir videoyu işler. İşlem başarılıysa True döner.

        video_file verilmezse arayüzdeki tek dosya alanı kullanılır (eski davranış).
        kuyruk_modu=True iken düğmeleri ve bitiş kutusunu bu metot yönetmez;
        onları kuyruk döngüsü (_kuyrugu_isle) hepsi bittiğinde bir kez yapıyor."""
        islem_basarili = False
        baslangic_zamani = time.time()
        try:
            video_file = video_file or self.video_path.get()
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
                return False

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

                # Kutu yalnızca tek dosya akışında: kuyrukta her video için bir
                # kutu açmak N tane modal pencere yığıyordu. Ön kontrol (_on_kontrol)
                # zaten baştan yakalıyor; buradaki yol savunma amaçlı duruyor.
                if not kuyruk_modu:
                    def _ffmpeg_uyarisi():
                        messagebox.showerror(
                            "FFmpeg Bulunamadı",
                            "Bu program sesi çözmek için FFmpeg kullanıyor ve sisteminizde bulunamadı.\n\n"
                            "Çözüm 1: FFmpeg kurun\n"
                            "    winget install Gyan.FFmpeg\n\n"
                            "Çözüm 2: Zaten kuruluysa 'FFmpeg Yolu' satırındaki 'Gözat' düğmesiyle\n"
                            "ffmpeg.exe dosyasını elle seçin.")
                    self.root.after(0, _ffmpeg_uyarisi)
                return False

            if "distil" in model_name.lower() and lang_code not in ("en", "auto"):
                self.log("⚠️ KRİTİK UYARI: 'distil' modelleri sadece İNGİLİZCE için eğitilmiştir!")

                if not kuyruk_modu:
                    def _distil_uyarisi():
                        messagebox.showerror("Model Uygunsuz",
                                             "'distil-large-v3' modeli sadece İngilizce destekler.")
                    self.root.after(0, _distil_uyarisi)
                return False

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

            # --- 3b) SANSÜRSÜZ MOD ---
            # Modeli yeniden yüklemeden initial_prompt'u ayarlıyoruz. Dil ancak bu
            # noktada kesinleştiği için (oylama 3. adımda bitiyor) burada yapılıyor.
            sansursuz = self.uncensored.get()
            istem_dili = "en" if task == "translate" else (secilen_dil or "")
            istem = self._sansursuz_istem(istem_dili, sansursuz)
            if self._istem_ayarla(istem):
                if istem:
                    self.log("🔞 Sansürsüz mod açık: metin birebir, küfür/argo yumuşatılmadan yazılacak.")
                elif sansursuz and istem_dili:
                    self.log(f"ℹ️ Sansürsüz mod için '{istem_dili.upper()}' dilinde hazır istem yok; "
                             "Whisper varsayılan davranışıyla devam ediyor.")
                    self.log("   (Yabancı dilde istem vermek modeli o dile kaydırıp altyazıyı bozuyor.)")

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

            # Çakışan/sıfır süreli bloklar oynatıcıda görünmüyor: yazmadan önce düzelt.
            srt_blocks = self._zamanlari_duzelt(srt_blocks)

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
                self.log("   ⛔ Altyazı dosyası YAZILMADI; aynı isimli eski altyazınız varsa olduğu gibi duruyor.")
                self.ilerleme(0, "Konuşma bulunamadı — dosya yazılmadı.")
                return False

            if will_translate:
                output_file_original = f"{base_path}{isim_eki}_{ceviri_kaynak_dili.upper()}.srt"
                output_file_tr = f"{base_path}{isim_eki}.srt"
            else:
                output_file_original = f"{base_path}{isim_eki}.srt"
                output_file_tr = None

            if not self._srt_yaz(output_file_original, srt_blocks):
                self.log("⛔ Yazılabilir altyazı satırı çıkmadı; dosya oluşturulmadı "
                         "(varsa eski altyazınıza dokunulmadı).")
                return False
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

                translated_texts = self._bloklari_cevir(srt_blocks, ceviri_kaynak_dili)

                if self.is_cancelled:
                    self.log("🛑 Çeviri yarıda kesildi; o ana kadar çevrilenler kaydediliyor.")

                # İptal edilse bile eldeki çeviriler yazılıyor (eskiden hepsi çöpe gidiyordu).
                self._srt_yaz(output_file_tr, srt_blocks, translated_texts)
                self.log(f"🇹🇷 Türkçe Altyazı Kaydedildi:\n{os.path.basename(output_file_tr)}")

                cevrilemeyen = len(srt_blocks) - len(translated_texts)
                if cevrilemeyen > 0 and not self.is_cancelled:
                    self.log(f"⚠️ {cevrilemeyen}/{len(srt_blocks)} satır çevrilemedi; bu satırlarda orijinal metin bırakıldı.")
                    if self._ceviri_son_hata:
                        self.log(f"   Son hata: {self._ceviri_son_hata}")
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
                f.write(f"SubtitleForge {surum_metni()}\n\n{traceback.format_exc()}")
            self.log("   Ayrıntılar HATA_RAPORU.txt dosyasına yazıldı.")
            self.ilerleme(0, "Hata oluştu.")

        finally:
            if not kuyruk_modu:
                def bitis_islemleri():
                    self._arayuzu_kilitle(False)
                    self.btn_start.config(state="normal", bg="#0078D4", fg="white")
                    self.btn_cancel.config(state="disabled", text="🛑 İPTAL ET", bg="#D13438")
                    self._kuyruk_yenile()      # düğme yazısını kuyruğa göre geri koyar

                    if islem_basarili:
                        messagebox.showinfo("Başarılı", "🎉 Tüm altyazı ve çeviri işlemleri kusursuz tamamlandı!")
                    elif self.is_cancelled:
                        messagebox.showwarning("İptal Edildi", "🛑 İşlem başarıyla durduruldu.")

                self.root.after(0, bitis_islemleri)

        return islem_basarili


if __name__ == "__main__":
    root = tk.Tk()
    app = WhisperApp(root)
    root.mainloop()

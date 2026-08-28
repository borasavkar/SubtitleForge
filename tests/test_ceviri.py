"""SubtitleForge çeviri katmanı için çevrimdışı regresyon testleri.

Çalıştırmak için:  python tests/test_ceviri.py

Google'a hiç istek atılmıyor: requests.Session sahte bir taşıyıcıyla değiştirilip
gerçek uç noktanın davranışları (satır sonlarını koruma, 429, satır birleştirme,
ağ hatası) taklit ediliyor.
"""
import os
import sys
import types
import tempfile

# --- tkinter yok: run.py'yi içe aktarabilmek için asgari sahte modüller ---
for ad in ("tkinter", "tkinter.filedialog", "tkinter.ttk",
           "tkinter.scrolledtext", "tkinter.messagebox"):
    mod = types.ModuleType(ad)
    sys.modules.setdefault(ad, mod)
tk = sys.modules["tkinter"]
tk.filedialog = sys.modules["tkinter.filedialog"]
tk.ttk = sys.modules["tkinter.ttk"]
tk.scrolledtext = sys.modules["tkinter.scrolledtext"]
tk.messagebox = sys.modules["tkinter.messagebox"]
for isim in ("StringVar", "BooleanVar", "Frame", "Label", "Button", "Tk"):
    setattr(tk, isim, type(isim, (), {}))
tk.END = "end"

# run.py depo kökünde duruyor.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run  # noqa: E402
sys.excepthook = sys.__excepthook__

BASARILI, BASARISIZ = [], []


def kontrol(ad, kosul, ayrinti=""):
    (BASARILI if kosul else BASARISIZ).append(ad)
    print(f"  {'✔' if kosul else '✘'} {ad}{('  → ' + ayrinti) if ayrinti and not kosul else ''}")


# ----------------------------------------------------------------------
# Sahte HTTP taşıyıcısı
# ----------------------------------------------------------------------
class SahteYanit:
    def __init__(self, status_code, govde=None):
        self.status_code = status_code
        self._govde = govde

    def json(self):
        if self._govde is None:
            raise ValueError("gövde yok")
        return self._govde


class SahteOturum:
    """davranis(metin, uc_nokta, istek_no) -> SahteYanit"""
    def __init__(self, davranis):
        self.headers = {}
        self.davranis = davranis
        self.istekler = []

    def post(self, url, params=None, data=None, timeout=None):
        metin = (data or {}).get("q", "")
        self.istekler.append((url, metin))
        return self.davranis(metin, url, len(self.istekler))

    def get(self, url, params=None, timeout=None):
        metin = (params or {}).get("q", "")
        self.istekler.append((url, metin))
        return self.davranis(metin, url, len(self.istekler))


def cumleler_yaniti(metin, donustur=lambda s: "TR:" + s):
    """Gerçek translate_a/single gibi: satır sonlarını KORUYAN dj=1 yanıtı."""
    satirlar = metin.split("\n")
    cumleler = []
    for i, s in enumerate(satirlar):
        trans = donustur(s) + ("\n" if i < len(satirlar) - 1 else "")
        cumleler.append({"trans": trans, "orig": s})
    return SahteYanit(200, {"sentences": cumleler, "src": "en"})


def cevirici_kur(davranis):
    cev = run.GoogleCevirici()
    oturum = SahteOturum(davranis)
    # threading.local yerine düz nesne: tüm işçi thread'leri aynı sahte oturumu görsün.
    cev._yerel = types.SimpleNamespace(oturum=oturum)
    return cev, oturum


# ----------------------------------------------------------------------
def test_satir_hizasi_korunuyor():
    print("\n[1] Paket çevirisinde satır hizası")
    cev, oturum = cevirici_kur(lambda m, u, n: cumleler_yaniti(m))
    kaynak = ["Hello there.", "How are you?", "Fine, thanks."]
    sonuc = cev.cevir("\n".join(kaynak), "en")
    parcalar = sonuc.split("\n")
    kontrol("3 satır gönderildi, 3 satır geldi", len(parcalar) == 3, f"{len(parcalar)}")
    kontrol("satırlar sırasıyla eşleşiyor",
            parcalar == ["TR:Hello there.", "TR:How are you?", "TR:Fine, thanks."], str(parcalar))
    kontrol("tek istek yeterli oldu", len(oturum.istekler) == 1, str(len(oturum.istekler)))


def test_eski_hata_yakalanirdi():
    print("\n[2] Eski hatanın taklidi: satır sonlarını SİLEN uç nokta")
    # deep_translator'ın /m yolu tam olarak böyle davranıyordu.
    def davranis(m, u, n):
        return cumleler_yaniti(m.replace("\n", " "), donustur=lambda s: "TR:" + s)
    cev, oturum = cevirici_kur(davranis)
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.is_cancelled = False
    uygulama._cevirici = cev
    uygulama._ceviri_son_hata = None
    uygulama.log = lambda m: None

    satirlar = [f"Line number {i}." for i in range(8)]
    sonuclar = {}
    uygulama._grup_cevir(list(range(8)), satirlar, "en", sonuclar)
    kontrol("hizasızlık ikiye bölerek çözüldü, 8 satırın 8'i çevrildi",
            len(sonuclar) == 8, f"{len(sonuclar)} satır")
    kontrol("hiçbir satır yanlış metinle eşleşmedi",
            all(sonuclar[i].endswith(f"Line number {i}.") for i in sonuclar),
            str(sonuclar))


def test_bir_bozuk_satir_izole_ediliyor():
    print("\n[3] 16 satırlık pakette tek bozuk satır")
    # 7 numaralı satır Google tarafından İKİYE bölünüyor (gerçek bir davranış).
    def davranis(m, u, n):
        satirlar = m.split("\n")
        cikti = []
        for s in satirlar:
            cikti.append("TR:" + s.replace("BOZUK", "BO\nZUK"))
        return cumleler_yaniti("\n".join(cikti), donustur=lambda s: s)

    cev, oturum = cevirici_kur(davranis)
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.is_cancelled = False
    uygulama._cevirici = cev
    uygulama._ceviri_son_hata = None
    uygulama.log = lambda m: None

    satirlar = [f"line {i}" for i in range(16)]
    satirlar[7] = "BOZUK satir"
    sonuclar = {}
    uygulama._grup_cevir(list(range(16)), satirlar, "en", sonuclar)

    saglam = [i for i in range(16) if i != 7]
    kontrol("bozuk satır dışındaki 15 satır doğru eşleşti",
            all(sonuclar.get(i, "").endswith(f"line {i}") for i in saglam),
            str({i: sonuclar.get(i) for i in saglam if not sonuclar.get(i, '').endswith(f'line {i}')}))
    kontrol("satır satır çeviriden (16 istek) çok daha az istek atıldı",
            len(oturum.istekler) < 16, f"{len(oturum.istekler)} istek")


def test_429_freni():
    print("\n[4] HTTP 429 → topluca fren")
    durum = {"n": 0}

    def davranis(m, u, n):
        durum["n"] += 1
        if durum["n"] == 1:
            return SahteYanit(429)
        return cumleler_yaniti(m)

    cev, oturum = cevirici_kur(davranis)
    import time
    t0 = time.monotonic()
    sonuc = cev.cevir("Hello", "en")
    gecen = time.monotonic() - t0
    kontrol("429 sonrası ikinci uç noktadan çeviri alındı", sonuc == "TR:Hello", sonuc)
    kontrol("429 global fren uyguladı (>=5 sn bekledi)", gecen >= 5.0, f"{gecen:.1f} sn")


def test_ag_hatasinda_bolunmuyor():
    print("\n[5] Ağ hatasında paket bölünmüyor (429 beslenmesin)")
    def davranis(m, u, n):
        raise OSError("bağlantı koptu")

    cev, oturum = cevirici_kur(davranis)
    cev.bekle = lambda s: None          # testte beklemeyelim
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.is_cancelled = False
    uygulama._cevirici = cev
    uygulama._ceviri_son_hata = None
    uygulama.log = lambda m: None

    satirlar = [f"line {i}" for i in range(32)]
    sonuclar = {}
    uygulama._grup_cevir(list(range(32)), satirlar, "en", sonuclar)
    kontrol("hiçbir satır çevrilmedi (onarım turuna kaldı)", sonuclar == {}, str(sonuclar))
    # 3 tur x 3 uç nokta = 9 istek; bölünseydi 60+ olurdu.
    kontrol("istek sayısı patlamadı", len(oturum.istekler) <= 12, f"{len(oturum.istekler)} istek")
    kontrol("hata mesajı saklandı", bool(uygulama._ceviri_son_hata), "")


def test_noktalama_satirlari():
    print("\n[6] Sadece noktalama içeren satırlar")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    kontrol("'...' çeviriye gönderilmiyor", not uygulama._cevrilmeye_deger("..."))
    kontrol("'♪' çeviriye gönderilmiyor", not uygulama._cevrilmeye_deger("♪"))
    kontrol("'- -' çeviriye gönderilmiyor", not uygulama._cevrilmeye_deger("- -"))
    kontrol("normal cümle gönderiliyor", uygulama._cevrilmeye_deger("Merhaba dünya."))
    kontrol("'5' gönderiliyor", uygulama._cevrilmeye_deger("5"))


def test_gruplama():
    print("\n[7] Paketleme sınırları")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    metinler = ["x" * 100 for _ in range(100)]
    gruplar = list(uygulama._satir_gruplari(list(range(100)), metinler))
    kontrol("hiçbir paket satır sınırını aşmıyor",
            all(len(g) <= run.CEVIRI_MAX_SATIR for g in gruplar))
    kontrol("hiçbir paket karakter sınırını aşmıyor",
            all(sum(len(metinler[i]) + 1 for i in g) <= run.CEVIRI_MAX_KARAKTER
                or len(g) == 1 for g in gruplar))
    kontrol("tüm satırlar tam bir kez paketlendi",
            sorted(i for g in gruplar for i in g) == list(range(100)))


def test_satir_kirma():
    print("\n[8] Satır kırma")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    kisa = "Kısa bir satır."
    kontrol("kısa metin bölünmüyor", uygulama._satir_kir(kisa) == kisa)

    uzun = ("Bu cümle Türkçeye çevrildikten sonra epeyce uzadı ve tek satıra "
            "kesinlikle sığmayacak kadar fazla kelime içeriyor artık.")
    cikti = uygulama._satir_kir(uzun)
    satirlar = cikti.split("\n")
    kontrol("uzun metin 2-3 satıra bölündü", 2 <= len(satirlar) <= 3, str(len(satirlar)))
    kontrol("kelime kaybı yok", " ".join(satirlar) == " ".join(uzun.split()))
    kontrol("satırlar dengeli (en uzun satır <= 55)",
            max(len(s) for s in satirlar) <= 55, str([len(s) for s in satirlar]))

    tek_kelime = "A" * 90
    kontrol("tek kelimelik dev metin bölünmüyor", uygulama._satir_kir(tek_kelime) == tek_kelime)


def test_zaman_duzeltme():
    print("\n[9] Zaman düzeltme")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    bloklar = [
        {"start": 1.0, "end": 3.0, "text": "a"},
        {"start": 2.0, "end": 2.5, "text": "b"},     # öncekiyle çakışıyor
        {"start": 5.0, "end": 5.0, "text": "c"},     # sıfır süre
        {"start": 9.0, "end": 8.0, "text": "d"},     # ters
    ]
    uygulama._zamanlari_duzelt(bloklar)
    kontrol("çakışma kalmadı",
            all(bloklar[i]["start"] >= bloklar[i - 1]["end"] for i in range(1, len(bloklar))),
            str(bloklar))
    kontrol("hiçbir blok sıfır/negatif süreli değil",
            all(b["end"] > b["start"] for b in bloklar), str(bloklar))


def test_srt_yazma():
    print("\n[10] SRT yazımı")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    bloklar = [
        {"start": 0.0, "end": 1.0, "text": "Bir"},
        {"start": 1.0, "end": 2.0, "text": "İki"},
        {"start": 2.0, "end": 3.0, "text": "Üç"},
    ]
    ceviriler = {0: "One", 2: "Three"}      # 1 numara çevrilemedi → orijinal kalmalı
    with tempfile.TemporaryDirectory() as d:
        yol = os.path.join(d, "t.srt")
        uygulama._srt_yaz(yol, bloklar, ceviriler)
        icerik = open(yol, encoding="utf-8").read()
    kontrol("çeviriler yazıldı", "One" in icerik and "Three" in icerik)
    kontrol("çevrilemeyen satırda orijinal korundu", "İki" in icerik)
    kontrol("'None' yazılmadı", "None" not in icerik)
    kontrol("numaralandırma 1,2,3", icerik.startswith("1\n") and "\n2\n" in icerik and "\n3\n" in icerik)

    # boş metinli blok numarayı harcamamalı
    bloklar2 = [{"start": 0.0, "end": 1.0, "text": "A"},
                {"start": 1.0, "end": 2.0, "text": "   "},
                {"start": 2.0, "end": 3.0, "text": "B"}]
    with tempfile.TemporaryDirectory() as d:
        yol = os.path.join(d, "t2.srt")
        uygulama._srt_yaz(yol, bloklar2)
        icerik2 = open(yol, encoding="utf-8").read()
    numaralar = [s.splitlines()[0] for s in icerik2.strip().split("\n\n")]
    kontrol("boş blok atlandı, numaralar boşluksuz", numaralar == ["1", "2"], str(numaralar))


def test_sansur_deseni():
    print("\n[11] Sansür (yıldızlama) tespiti")
    D = run.SANSUR_DESENI
    for ornek in ["s**tir", "f***", "b*ch", "Seni a***k", "***"]:
        kontrol(f"yakalandı: {ornek}", bool(D.search(ornek)))
    for ornek in ["Merhaba dünya", "5 * 3 = 15", "2*3", "yıldız işareti", "3*4=12", "madde*"]:
        kontrol(f"yanlış alarm yok: {ornek}", not D.search(ornek))


def test_sansur_onarimi():
    print("\n[12] Sansürlü satırın tekrar istenmesi")
    def davranis(m, u, n):
        return cumleler_yaniti(m, donustur=lambda s: s.replace("fuck", "siktir"))

    cev, _ = cevirici_kur(davranis)
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.is_cancelled = False
    uygulama._cevirici = cev
    uygulama.log = lambda m: None

    kaynak = ["Oh fuck.", "Normal cümle."]
    ceviriler = {0: "Ah s**tir.", 1: "Normal cümle."}      # 0 yedek yoldan sansürlü geldi
    uygulama._sansuru_onar(ceviriler, kaynak, "en")
    kontrol("sansürlü satır yeniden çevrildi", ceviriler[0] == "Oh siktir.", ceviriler[0])
    kontrol("temiz satıra dokunulmadı", ceviriler[1] == "Normal cümle.")


def test_istem_secimi():
    print("\n[13] Sansürsüz mod istemi")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    kontrol("kapalıyken istem yok", uygulama._sansursuz_istem("en", False) is None)
    kontrol("İngilizce istem var", bool(uygulama._sansursuz_istem("en", True)))
    kontrol("Türkçe istem var", bool(uygulama._sansursuz_istem("tr", True)))
    kontrol("desteklenmeyen dilde istem yok (dil kayması riski)",
            uygulama._sansursuz_istem("ja", True) is None)
    kontrol("boş dil kodunda istem yok", uygulama._sansursuz_istem("", True) is None)
    kontrol("İngilizce istem sansürsüzlüğü söylüyor",
            "censor" in uygulama._sansursuz_istem("en", True).lower())
    kontrol("Türkçe istem sansürsüzlüğü söylüyor",
            "sansür" in uygulama._sansursuz_istem("tr", True).lower())
    kontrol("uygulamanın tüm dilleri için istem var",
            all(k in run.SANSURSUZ_ISTEMLER for k in ("tr", "en", "fr", "de", "it", "ru", "es")))


def test_istem_ayarlama():
    print("\n[14] initial_prompt'un yüklü modele uygulanması")
    from dataclasses import dataclass, field

    @dataclass
    class SahteSecenekler:
        initial_prompt: str = None
        beam_size: int = 5

    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.pipeline = types.SimpleNamespace(options=SahteSecenekler())
    kontrol("dataclass seçenekleri güncellendi",
            uygulama._istem_ayarla("TEST") and uygulama.pipeline.options.initial_prompt == "TEST")
    kontrol("None ile sıfırlanabiliyor",
            uygulama._istem_ayarla(None) and uygulama.pipeline.options.initial_prompt is None)

    # NamedTuple sürümü
    from collections import namedtuple
    NT = namedtuple("NT", "initial_prompt beam_size")
    uygulama.pipeline = types.SimpleNamespace(options=NT(None, 5))
    kontrol("NamedTuple seçenekleri güncellendi",
            uygulama._istem_ayarla("X") and uygulama.pipeline.options.initial_prompt == "X")

    uygulama.pipeline = types.SimpleNamespace(options=None)
    kontrol("seçenek yoksa çökmüyor", uygulama._istem_ayarla("X") is False)


def test_liste_yaniti():
    print("\n[15] dj=1 yok sayılırsa (liste biçimli yanıt)")
    def davranis(m, u, n):
        satirlar = m.split("\n")
        parcalar = []
        for i, s in enumerate(satirlar):
            parcalar.append(["TR:" + s + ("\n" if i < len(satirlar) - 1 else ""), s, None, None, 10])
        return SahteYanit(200, [parcalar, None, "en"])

    cev, _ = cevirici_kur(davranis)
    sonuc = cev.cevir("a\nb\nc", "en")
    kontrol("liste biçimi de çözülüyor", sonuc == "TR:a\nTR:b\nTR:c", repr(sonuc))


def test_tekrar_filtresi():
    print("\n[16] Halüsinasyon tekrar filtresi (regresyon)")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    bloklar = [{"start": i, "end": i + 1, "text": "Ah!"} for i in range(90)]
    bloklar.append({"start": 100, "end": 101, "text": "Gerçek cümle."})
    temiz, atilan, en_uzun = uygulama._tekrar_filtrele(bloklar)
    kontrol("döngü ayıklandı", atilan == 90 - run.TEKRAR_LIMITI, str(atilan))
    kontrol("gerçek cümle korundu", temiz[-1]["text"] == "Gerçek cümle.")
    kontrol("en uzun tekrar raporlandı", en_uzun == 90, str(en_uzun))


def uctan_uca_uygulama(davranis, sansursuz=True):
    cev, oturum = cevirici_kur(davranis)
    cev.bekle = lambda s: None

    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    uygulama.is_cancelled = False
    uygulama.log = lambda m: None
    uygulama.ilerleme = lambda y, e: None
    uygulama._ceviri_son_hata = None
    uygulama.uncensored = types.SimpleNamespace(get=lambda: sansursuz)
    uygulama._sure_metni = run.WhisperApp._sure_metni.__get__(uygulama)
    # GoogleCevirici'yi sabitliyoruz ki _bloklari_cevir yenisini kurmasın.
    gercek = run.GoogleCevirici
    run.GoogleCevirici = lambda **kw: cev
    return uygulama, cev, oturum, gercek


def test_uctan_uca_onarim():
    print("\n[17] Uçtan uca: ilk turda düşen satırlar onarım turunda kurtarılıyor")
    durum = {"basarisiz_kalan": 3}

    def davranis(m, u, n):
        # İlk 3 istek ağ hatası veriyor, sonrası düzeliyor (geçici kesinti taklidi).
        if durum["basarisiz_kalan"] > 0:
            durum["basarisiz_kalan"] -= 1
            raise OSError("geçici kesinti")
        return cumleler_yaniti(m)

    uygulama, cev, oturum, gercek = uctan_uca_uygulama(davranis)
    try:
        bloklar = [{"start": i, "end": i + 1, "text": f"Sentence number {i}."} for i in range(120)]
        bloklar[5]["text"] = "..."          # çevrilmemeli
        bloklar[9]["text"] = "♪"
        ceviriler = uygulama._bloklari_cevir(bloklar, "en")
    finally:
        run.GoogleCevirici = gercek

    kontrol("120 satırın tamamı sonuçlandı", len(ceviriler) == 120, f"{len(ceviriler)}")
    eksik = [i for i in range(120) if i not in ceviriler]
    kontrol("çevrilemeyen satır kalmadı", not eksik, str(eksik))
    kontrol("noktalama satırları olduğu gibi bırakıldı",
            ceviriler[5] == "..." and ceviriler[9] == "♪", f"{ceviriler[5]!r} {ceviriler[9]!r}")
    yanlis = [i for i in range(120) if i not in (5, 9)
              and ceviriler[i] != f"TR:Sentence number {i}."]
    kontrol("hiçbir satır kaymadı (metin↔indeks eşleşmesi doğru)", not yanlis, str(yanlis[:5]))


def test_uctan_uca_kalici_hata():
    print("\n[18] Uçtan uca: kalıcı kesintide orijinal metin korunuyor, çökme yok")
    def davranis(m, u, n):
        raise OSError("ağ tamamen kapalı")

    uygulama, cev, oturum, gercek = uctan_uca_uygulama(davranis)
    # deep_translator yedeği de çalışmasın (ağ yok).
    cev.cevir_yedek = lambda metin, kaynak, hedef="tr": None
    try:
        bloklar = [{"start": i, "end": i + 1, "text": f"Line {i}."} for i in range(20)]
        ceviriler = uygulama._bloklari_cevir(bloklar, "en")
    finally:
        run.GoogleCevirici = gercek

    kontrol("çeviri yok ama istisna fırlamadı", ceviriler == {}, str(ceviriler))

    with tempfile.TemporaryDirectory() as d:
        yol = os.path.join(d, "t.srt")
        uygulama._satir_kir = run.WhisperApp._satir_kir.__get__(uygulama)
        uygulama.format_timestamp = run.WhisperApp.format_timestamp.__get__(uygulama)
        uygulama._srt_yaz(yol, bloklar, ceviriler)
        icerik = open(yol, encoding="utf-8").read()
    kontrol("SRT yine de yazıldı, orijinal metinlerle", "Line 19." in icerik)
    kontrol("'None' sızmadı", "None" not in icerik)


def test_uctan_uca_iptal():
    print("\n[19] Uçtan uca: iptal edildiğinde o ana kadarki çeviriler korunuyor")
    durum = {"n": 0}

    def davranis(m, u, n):
        durum["n"] += 1
        if durum["n"] > 2:
            uygulama.is_cancelled = True
        return cumleler_yaniti(m)

    uygulama, cev, oturum, gercek = uctan_uca_uygulama(davranis)
    try:
        bloklar = [{"start": i, "end": i + 1, "text": f"Line {i}."} for i in range(300)]
        ceviriler = uygulama._bloklari_cevir(bloklar, "en")
    finally:
        run.GoogleCevirici = gercek

    kontrol("iptal sonrası eldeki çeviriler duruyor", len(ceviriler) > 0, str(len(ceviriler)))
    kontrol("iptal hepsini çevirmeden bitti", len(ceviriler) < 300, str(len(ceviriler)))
    yanlis = [i for i, c in ceviriler.items() if c != f"TR:Line {i}."]
    kontrol("kısmi sonuçta da hiçbir satır kaymadı", not yanlis, str(yanlis[:5]))


def test_cumle_gruplama():
    print("\n[20] Cümle grupları (çeviri birimi blok değil cümle)")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)

    def grupla(bloklar):
        return list(uygulama._cumle_gruplari(bloklar))

    # Bölücünün 75 karakterde parçaladığı TEK bir cümle.
    parcali = [
        {"start": 0.0, "end": 1.0, "text": "I never"},
        {"start": 1.0, "end": 3.0, "text": "understood why she chose to leave the city"},
        {"start": 3.0, "end": 4.5, "text": "without telling anyone."},
    ]
    kontrol("parçalanmış cümle tek grupta birleşti",
            grupla(parcali) == [[0, 1, 2]], str(grupla(parcali)))

    # Uzun sessizlik yeni repliktir; cümle bitmese bile ayrılmalı.
    sessizlikli = [
        {"start": 0.0, "end": 1.0, "text": "I never"},
        {"start": 30.0, "end": 31.0, "text": "understood why."},
    ]
    kontrol("uzun sessizlik replikleri ayırdı",
            grupla(sessizlikli) == [[0], [1]], str(grupla(sessizlikli)))

    # Nota/noktalama satırları konuşmaya karışmamalı.
    notali = [
        {"start": 0.0, "end": 1.0, "text": "♪"},
        {"start": 1.0, "end": 2.0, "text": "I never"},
        {"start": 2.0, "end": 3.0, "text": "understood."},
    ]
    kontrol("'♪' kendi grubunda kaldı", grupla(notali) == [[0], [1, 2]], str(grupla(notali)))

    # Noktalama hiç gelmezse grup sonsuza kadar büyümemeli.
    noktasiz = [{"start": float(i), "end": i + 1.0, "text": "kelime"} for i in range(30)]
    gruplar = grupla(noktasiz)
    kontrol("noktalama yoksa blok sınırı devrede",
            all(len(g) <= run.CUMLE_MAX_BLOK for g in gruplar), str([len(g) for g in gruplar]))
    kontrol("karakter sınırı devrede",
            all(sum(len(noktasiz[i]["text"]) + 1 for i in g) <= run.CUMLE_MAX_KARAKTER
                or len(g) == 1 for g in gruplar))

    # Hiçbir blok kaybolmamalı, sıra bozulmamalı (SRT satırı kaybı kabul edilemez).
    karisik = parcali + notali + noktasiz
    for i, b in enumerate(karisik):
        b["start"], b["end"] = float(i), i + 1.0
    duz = [i for g in grupla(karisik) for i in g]
    kontrol("hiçbir blok kaybolmadı / tekrarlamadı",
            duz == list(range(len(karisik))), str(duz))

    tirnakli = [{"start": 0.0, "end": 1.0, "text": '"Bitti."'},
                {"start": 1.0, "end": 2.0, "text": "Yeni cümle."}]
    kontrol("kapanış tırnağı cümle sonunu gizlemiyor",
            grupla(tirnakli) == [[0], [1]], str(grupla(tirnakli)))

    uc_nokta = [{"start": 0.0, "end": 1.0, "text": "Bekle…"},
                {"start": 1.0, "end": 2.0, "text": "Sonra gitti."}]
    kontrol("'…' cümle sonu sayılıyor", grupla(uc_nokta) == [[0], [1]], str(grupla(uc_nokta)))


def test_ceviri_dagitimi():
    print("\n[21] Çevirinin bloklara geri dağıtılması")
    uygulama = run.WhisperApp.__new__(run.WhisperApp)
    D = uygulama._ceviriyi_dagit

    kontrol("tek blok tam çeviriyi alır",
            D("Merhaba dünya.", ["Hello world."]) == ["Merhaba dünya."])

    d = D("bir iki uc dort bes alti", ["ab", "abcde", "ab"])
    kontrol("3 blok / 6 kelime dağıtıldı", d is not None and len(d) == 3, str(d))
    kontrol("boş satır üretilmedi", bool(d) and all(p.strip() for p in d), str(d))
    kontrol("kelime kaybı/tekrarı yok",
            bool(d) and " ".join(d) == "bir iki uc dort bes alti", str(d))

    d3 = D("bir iki uc", ["ab", "abcde", "ab"])
    kontrol("3 blok / 3 kelime → birer kelime", d3 == ["bir", "iki", "uc"], str(d3))

    kontrol("3 blok / 2 kelime → None (blok blok çeviriye düşer)",
            D("bir iki", ["ab", "abcde", "ab"]) is None)

    d5 = D("a b c d e", ["x", "x", "x", "x", "x"])
    kontrol("5 blok / 5 kelime → boş satır yok", d5 == ["a", "b", "c", "d", "e"], str(d5))

    d_oran = D(" ".join(str(i) for i in range(10)), ["a", "a" * 50])
    kontrol("uzun bloğa daha çok kelime düştü",
            bool(d_oran) and len(d_oran[1].split()) > len(d_oran[0].split()), str(d_oran))

    kontrol("boş çeviri None döner", D("", ["ab", "cd"]) is None)


def test_uctan_uca_cumle_butunlugu():
    print("\n[22] Uçtan uca: cümle bütün halde çevrilip bloklara dağıtılıyor")
    uygulama, cev, oturum, gercek = uctan_uca_uygulama(lambda m, u, n: cumleler_yaniti(m))
    try:
        bloklar = [
            {"start": 0.0, "end": 1.0, "text": "I never"},
            {"start": 1.0, "end": 3.0, "text": "understood why she chose to leave the city"},
            {"start": 3.0, "end": 4.5, "text": "without telling anyone."},
            {"start": 40.0, "end": 41.0, "text": "♪"},
            {"start": 41.0, "end": 43.0, "text": "She came back."},
        ]
        ceviriler = uygulama._bloklari_cevir(bloklar, "en")
    finally:
        run.GoogleCevirici = gercek

    kontrol("her blok bir çeviri aldı", len(ceviriler) == 5, str(sorted(ceviriler)))
    kontrol("boş altyazı satırı üretilmedi",
            all(str(ceviriler[i]).strip() for i in ceviriler), str(ceviriler))
    kontrol("'♪' olduğu gibi kaldı", ceviriler.get(3) == "♪", repr(ceviriler.get(3)))

    gonderilen = " || ".join(m for _, m in oturum.istekler)
    kontrol("cümle Google'a BÜTÜN gönderildi (parça parça değil)",
            "I never understood why she chose to leave the city without telling anyone."
            in gonderilen, gonderilen)

    birlesik = " ".join(ceviriler[i] for i in (0, 1, 2))
    kontrol("cümlenin çevirisi 3 bloğa eksiksiz dağıtıldı",
            birlesik == ("TR:I never understood why she chose to leave the city "
                         "without telling anyone."), birlesik)
    kontrol("ayrı replik ayrı çevrildi", ceviriler.get(4) == "TR:She came back.",
            repr(ceviriler.get(4)))


for t in (test_satir_hizasi_korunuyor, test_eski_hata_yakalanirdi,
          test_bir_bozuk_satir_izole_ediliyor, test_429_freni,
          test_ag_hatasinda_bolunmuyor, test_noktalama_satirlari, test_gruplama,
          test_satir_kirma, test_zaman_duzeltme, test_srt_yazma, test_sansur_deseni,
          test_sansur_onarimi, test_istem_secimi, test_istem_ayarlama,
          test_liste_yaniti, test_tekrar_filtresi,
          test_uctan_uca_onarim, test_uctan_uca_kalici_hata, test_uctan_uca_iptal,
          test_cumle_gruplama, test_ceviri_dagitimi, test_uctan_uca_cumle_butunlugu):
    t()

print("\n" + "=" * 60)
print(f"GEÇEN: {len(BASARILI)}   KALAN: {len(BASARISIZ)}")
if BASARISIZ:
    for ad in BASARISIZ:
        print("  ✘", ad)
sys.exit(1 if BASARISIZ else 0)

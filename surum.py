"""Sürüm numarasının TEK kaynağı.

Hem run.py (pencere başlığı + log) hem de run.spec (exe'nin Windows sürüm
kaynağı) buradan okuyor. Ayrı bir dosya olmasının sebebi: run.spec'in bu değeri
okumak için run.py'yi import etmesi gerekseydi, derleme sırasında tkinter ve
deep_translator da yüklenirdi.

Sürüm yükseltirken yalnızca burayı değiştirin.
  MAJOR.MINOR.PATCH
  PATCH -> hata düzeltmesi, MINOR -> yeni özellik, MAJOR -> geriye uyumsuz değişiklik
"""

SURUM = "1.1.0"

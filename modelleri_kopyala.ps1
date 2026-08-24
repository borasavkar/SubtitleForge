# Modelleri exe'nin yanindaki "models" klasorune kopyalar.
#
# Bu klasor VARSA run.py modelleri oradan okur (HF_HOME / TORCH_HOME / NLTK_DATA
# oraya yonlendirilir). Boylece exe'yi baska bir bilgisayara kopyaladiginda
# internet olmadan da calisir. Klasor YOKSA hicbir sey degismez: modeller her
# zamanki gibi %USERPROFILE%\.cache altindan okunur/indirilir.
#
# Kullanim:
#   .\modelleri_kopyala.ps1                 -> varsayilan model (large-v3-turbo)
#   .\modelleri_kopyala.ps1 -Hepsi          -> onbellekteki tum Whisper modelleri
#   .\modelleri_kopyala.ps1 -Hedef "D:\tasinabilir\run"

param(
    [string]$Hedef = "$PSScriptRoot\dist\run",
    [switch]$Hepsi
)

$ErrorActionPreference = "Stop"

$hfKaynak    = "$env:USERPROFILE\.cache\huggingface\hub"
$torchKaynak = "$env:USERPROFILE\.cache\torch\hub"
$models      = Join-Path $Hedef "models"
$hfHedef     = Join-Path $models "hub"

if (-not (Test-Path $Hedef)) { throw "Hedef klasor yok: $Hedef  (once exe'yi derleyin)" }
New-Item -ItemType Directory -Force -Path $hfHedef | Out-Null

function Kopyala($kaynak, $hedef, $etiket) {
    if (-not (Test-Path $kaynak)) { Write-Host "  ATLANDI  $etiket (kaynak yok)"; return }
    $mb = [math]::Round(((Get-ChildItem $kaynak -Recurse -File -EA SilentlyContinue |
                          Measure-Object Length -Sum).Sum) / 1MB)
    Write-Host ("  {0,6} MB  {1}" -f $mb, $etiket)
    Copy-Item $kaynak $hedef -Recurse -Force
}

Write-Host "Hedef: $models`n"

# --- Whisper modelleri (CTranslate2 formati) ---
# Varsayilan yalnizca turbo: arayuzdeki varsayilan model bu ve tek basina 1.5 GB.
$desen = if ($Hepsi) { "models--*" } else { "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" }
Get-ChildItem $hfKaynak -Directory -Filter $desen -EA SilentlyContinue | ForEach-Object {
    Kopyala $_.FullName $hfHedef $_.Name
}

# --- Hizalama modelleri ---
# torchaudio paketleri (en/fr/de/es/it) torch hub'da, digerleri HF'de durur.
Kopyala "$torchKaynak\checkpoints" $hfHedef "torch hub checkpoints (wav2vec2 hizalama)"

# --- NLTK cumle bolucu ---
# Exe'nin icinde zaten var; burada da bulunmasi zarar vermez, tasima kolayligi icin.
$nltk = "$env:APPDATA\nltk_data"
if (Test-Path $nltk) { Kopyala $nltk $models "nltk_data" }

$toplam = [math]::Round(((Get-ChildItem $models -Recurse -File -EA SilentlyContinue |
                          Measure-Object Length -Sum).Sum) / 1MB)
Write-Host "`nBITTI. models klasoru: $toplam MB"
Write-Host "Programi acinca log'da su satiri gormelisiniz:"
Write-Host "  Modeller program klasorunden okunuyor: models"

# TwinCAT ADS ルータ (AI 側 Windows) 導入チェック
#
# 目的: AI 側 Windows に TC1000 (TwinCAT 3 ADS Setup) のルータが
#       入っているかを確認し、無ければ導入手順を表示する。
#       このスクリプトは「確認と案内」だけを行い、勝手にインストールしない。
#
# 実行: powershell -ExecutionPolicy Bypass -File .\Setup-AdsRouter-Check.ps1

$ErrorActionPreference = 'SilentlyContinue'
Write-Host '=== TwinCAT ADS Router check (AI-side Windows) ===' -ForegroundColor Cyan

# 1) TcAdsDll.dll の存在 (pyads が Windows で必要とする DLL)
$dllCandidates = @(
    'C:\TwinCAT\3.1\System\TcAdsDll.dll',
    'C:\TwinCAT\Ads\TcAdsDll.dll',
    "$env:ProgramFiles\Beckhoff\TwinCAT\3.1\System\TcAdsDll.dll",
    "${env:ProgramFiles(x86)}\Beckhoff\TwinCAT\3.1\System\TcAdsDll.dll",
    "$env:SystemRoot\System32\TcAdsDll.dll"
)
$dll = $dllCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($dll) {
    Write-Host "[OK] TcAdsDll.dll: $dll" -ForegroundColor Green
} else {
    Write-Host '[NG] TcAdsDll.dll が見つからない (ADS ルータ未導入の可能性)' -ForegroundColor Yellow
}

# 2) TwinCAT 関連サービス
$svc = Get-Service | Where-Object { $_.Name -match 'TcSysSrv|TwinCAT|TcAms' }
if ($svc) {
    $svc | ForEach-Object { Write-Host ("[OK] Service: {0} ({1})" -f $_.Name, $_.Status) -ForegroundColor Green }
} else {
    Write-Host '[NG] TwinCAT 関連サービスが見つからない' -ForegroundColor Yellow
}

# 3) AMS ルータの待ち受け (TCP 48898)
$listen = Get-NetTCPConnection -LocalPort 48898 -State Listen
if ($listen) {
    Write-Host '[OK] TCP 48898 が Listen 中 (AMS ルータ応答あり)' -ForegroundColor Green
} else {
    Write-Host '[--] TCP 48898 の Listen は検出できず (ルータ停止中 or 未導入)' -ForegroundColor Yellow
}

# 4) Python / uv
$py = Get-Command python -ErrorAction SilentlyContinue
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($py) { Write-Host "[OK] python: $($py.Source)" -ForegroundColor Green } else { Write-Host '[NG] python が無い (3.11+ が必要)' -ForegroundColor Yellow }
if ($uv) { Write-Host "[OK] uv: $($uv.Source)" -ForegroundColor Green } else { Write-Host '[NG] uv が無い (https://docs.astral.sh/uv/ から導入)' -ForegroundColor Yellow }

Write-Host ''
if (-not $dll) {
    Write-Host '--- ルータ導入手順 (TC1000) ---'
    Write-Host '1. Beckhoff アカウントを登録する (ダウンロードに必要)'
    Write-Host '   https://www.beckhoff.com/'
    Write-Host '2. Download finder > Software and tools > TwinCAT 3 > TC1xxx'
    Write-Host '   > Runtime > TC1000 > "TC3.1 ADS" の Setup をダウンロード'
    Write-Host '3. インストーラを Full install で実行し、再起動する'
    Write-Host '4. 本スクリプトを再実行して [OK] になることを確認する'
    Write-Host ''
}
Write-Host '--- ルート設定 (導入後) ---'
Write-Host '1. トレイの TwinCAT アイコンを右クリック > Router > Edit Routes'
Write-Host '2. Add で対象 PLC を登録: Name / AMS Net Id (PLC) / Address (PLC の IP)'
Write-Host '   / Transport は TCP/IP'
Write-Host '3. PLC 側にも AI 側への戻りルートが必要 (XAE の SYSTEM > Routes、'
Write-Host '   もしくは smoke_test 前に add_route_to_plc を使う方法が仕様書にあり)'
Write-Host '4. 疎通確認: tools\ads-mcp で  uv run python smoke_test.py <既知の変数名>'

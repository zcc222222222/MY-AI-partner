#requires -Version 5.1
<#
  Compress the full-page background video for the v1 frontend.

  Why: the source ships at 3840x2160 / 60fps / 23s / 133 MB. That trips GitHub's
  100 MB per-file limit (push is rejected outright) and costs a 4K60 decode just
  to sit behind the chat UI.

  What it does:
    - scales to 1080p (keeps aspect ratio, width rounded to an even number)
    - drops to 30 fps (imperceptible behind a 40%-visible scrim, halves bitrate)
    - re-encodes H.264 yuv420p (widest browser support) at CRF 23
    - keeps the audio track as AAC, so the speaker button in the UI has something
      to play. The video element stays muted by default - browsers only allow
      MUTED autoplay, which is why the audio is inaudible until you click.
    - writes +faststart, i.e. moves the moov atom to the front of the file. The
      original has moov at the END, which forces the browser to buffer the whole
      thing before the first frame.

  Looping needs nothing here: the <video> element has the `loop` attribute.

  Result on the original footage: 133.50 MB -> 12.06 MB (91% smaller), verified
  as 1920x1080 / 30fps / 695 frames / AAC kept / faststart true.

  Usage (this machine has no pwsh / PowerShell 7, only Windows PowerShell 5.1):
    powershell -ExecutionPolicy Bypass -File .\scripts\compress-background-video.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File .\scripts\compress-background-video.ps1

  NOTE: keep this file ASCII-only - Windows PowerShell 5.1 mis-decodes UTF-8
  without BOM, which would corrupt any non-ASCII literal in here.
#>
[CmdletBinding()]
param(
    # Source video. Defaults to the largest *.mp4 sitting in the project root.
    [string]$Input,
    # Destination. Defaults to the file the frontend actually loads.
    [string]$Output,
    [int]$Height = 1080,
    [int]$Fps = 30,
    [int]$Crf = 23,
    [int]$AudioBitrate = 128,
    # Strip audio entirely (smaller file, and the speaker button goes quiet).
    [switch]$NoAudio,
    # Print the ffmpeg command without running it.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Info([string]$t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Ok([string]$t) { Write-Host "  [ok]   $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "  [warn] $t" -ForegroundColor Yellow }

function Find-Ffmpeg {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # Build the candidate list defensively: some environments leave ProgramFiles
    # or LOCALAPPDATA unset, and Join-Path *throws* on a null base rather than
    # returning null.
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($base in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:USERPROFILE)) {
        if (-not $base) { continue }
        $candidates.Add((Join-Path $base 'Microsoft\WinGet\Links\ffmpeg.exe'))
        $candidates.Add((Join-Path $base 'ffmpeg\bin\ffmpeg.exe'))
        $candidates.Add((Join-Path $base 'scoop\shims\ffmpeg.exe'))
    }
    $candidates.Add('C:\ffmpeg\bin\ffmpeg.exe')
    $candidates.Add('C:\ProgramData\chocolatey\bin\ffmpeg.exe')

    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $Output) {
    $Output = Join-Path $ProjectRoot 'v1\frontend\public\assets\app-background.mp4'
}
if (-not $Input) {
    $found = Get-ChildItem -LiteralPath $ProjectRoot -Filter '*.mp4' -File |
        Where-Object { $_.FullName -ne $Output } |
        Sort-Object -Property Length -Descending |
        Select-Object -First 1
    if (-not $found) {
        throw "No source .mp4 found in $ProjectRoot. Pass -Input explicitly."
    }
    $Input = $found.FullName
}

Write-Host "`n=== compress background video ===" -ForegroundColor Cyan
Info ("input  : " + $Input)
Info ("output : " + $Output)

if (-not (Test-Path -LiteralPath $Input)) { throw "input not found: $Input" }
$beforeMb = (Get-Item -LiteralPath $Input).Length / 1MB
Info ("source : {0:N2} MB" -f $beforeMb)

$ffmpeg = Find-Ffmpeg
if (-not $ffmpeg) {
    Write-Host "`n  [fail] ffmpeg not found." -ForegroundColor Red
    Info 'install it with:   winget install Gyan.FFmpeg'
    Info 'or grab a static build: https://www.gyan.dev/ffmpeg/builds/'
    Info 'then re-run this script.'
    exit 1
}
Ok ("ffmpeg : " + $ffmpeg)
$prevEapVer = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$ffVersion = (& $ffmpeg -version | Select-Object -First 1)
$ErrorActionPreference = $prevEapVer
if ($ffVersion) { Info ("version: " + $ffVersion) }

# -2 keeps the aspect ratio and forces an even width (required by yuv420p).
$scaleFilter = "scale=-2:${Height}:flags=lanczos"

$ffArgs = @(
    '-y',
    '-hide_banner',
    '-i', $Input,
    '-map', '0:v:0'
)
if (-not $NoAudio) { $ffArgs += @('-map', '0:a:0?') }
$ffArgs += @(
    '-vf', $scaleFilter,
    '-r', "$Fps",
    '-c:v', 'libx264',
    '-preset', 'slow',
    '-crf', "$Crf",
    '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart'
)
if ($NoAudio) {
    $ffArgs += '-an'
} else {
    $ffArgs += @('-c:a', 'aac', '-b:a', "${AudioBitrate}k")
}
$ffArgs += $Output

if ($DryRun) {
    Write-Host "`n  command:" -ForegroundColor Cyan
    Info ("& `"" + $ffmpeg + "`" " + ($ffArgs -join ' '))
    Write-Host "`n  (dry run - nothing written)`n"
    exit 0
}

Write-Host "`n  encoding (this takes a minute or two)..." -ForegroundColor Cyan
# PowerShell 5.1 turns native stderr into a terminating NativeCommandError when
# ErrorActionPreference is Stop, and ffmpeg writes its progress to stderr.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $ffmpeg @ffArgs
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($code -ne 0) { throw "ffmpeg exited with code $code" }

if (-not (Test-Path -LiteralPath $Output)) { throw "ffmpeg reported success but $Output is missing" }

$afterMb = (Get-Item -LiteralPath $Output).Length / 1MB
Write-Host ""
Ok ("{0:N2} MB -> {1:N2} MB  ({2:N0}% smaller)" -f $beforeMb, $afterMb, (100 - ($afterMb / $beforeMb * 100)))
if ($afterMb -gt 100) {
    Warn "still over GitHub's 100 MB per-file limit - raise -Crf or lower -Height and re-run."
} else {
    Ok "under 100 MB: you can now delete the '*.mp4' line in v1\frontend\public\assets\.gitignore to track it."
}

Write-Host "`n  verifying the output container..." -ForegroundColor Cyan
$probe = Join-Path $PSScriptRoot 'probe-mp4.js'
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if ($node -and (Test-Path -LiteralPath $probe)) {
    & $node $probe $Output
} else {
    Warn 'node or probe-mp4.js unavailable - skipped container check'
}
Write-Host ""

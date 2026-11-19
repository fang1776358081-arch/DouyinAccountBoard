param([switch]$Once, [switch]$WaitForResume)

$ErrorActionPreference = 'Stop'
if ($env:DOUYIN_BOARD_COLLECTION_ENABLED -ne '1') { throw '作品采集未启用。' }
if (-not $env:DOUYIN_BOARD_F2_PYTHON -or -not (Test-Path -LiteralPath $env:DOUYIN_BOARD_F2_PYTHON -PathType Leaf)) {
    throw '请配置 DOUYIN_BOARD_F2_PYTHON 指向采集 Python 运行环境。'
}
$workerArgs = if ($Once) { @('--once') } else { @() }
if ($WaitForResume) { $workerArgs += '--wait-for-resume' }
& $env:DOUYIN_BOARD_F2_PYTHON -X utf8 (Join-Path $PSScriptRoot 'f2_worker.py') @workerArgs
exit $LASTEXITCODE

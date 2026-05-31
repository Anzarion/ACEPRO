<#
.SYNOPSIS
    Correlate ACE tangle-telemetry SILENCE phases with klippy.log activity.

.DESCRIPTION
    Reads SILENCE_START / SILENCE_END markers emitted by runout_monitor
    (see _log_tangle_telemetry) from klippy.log. For each silence window,
    prints:
      - Header (when/where/duration/missing pulses)
      - Klipper Stats lines within the window (print_stall, buffer_time,
        mcu_awake, srtt, retransmit)
      - All non-Stats klippy.log lines within the window (macros,
        TMC events, gcode responses, exceptions) plus/minus a configurable
        context margin

    The intent is one-glance diagnosis: which macro / event / state
    change coincides with each encoder silence.

.PARAMETER KlippyLog
    Path to klippy.log.

.PARAMETER ContextSeconds
    Seconds before SILENCE_START and after SILENCE_END to include from
    klippy.log. Default 10.

.PARAMETER MinDurationS
    Skip silences shorter than this (default 0 = show all).

.PARAMETER OutputFile
    Where to mirror all console output. Default: silence-report.txt next
    to the input KlippyLog. Pass "" or "-" to disable file output.

.EXAMPLE
    .\silence-correlate.ps1 -KlippyLog .\klippy.log

.EXAMPLE
    .\silence-correlate.ps1 -KlippyLog .\klippy.log -ContextSeconds 20 -MinDurationS 5

.EXAMPLE
    .\silence-correlate.ps1 -KlippyLog .\klippy.log -OutputFile C:\tmp\report.txt
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)] [string] $KlippyLog,
    [double] $ContextSeconds = 10.0,
    [double] $MinDurationS = 0.0,
    [string] $OutputFile = $null
)

if (-not (Test-Path $KlippyLog)) {
    Write-Error "KlippyLog not found: $KlippyLog"
    exit 1
}

# Default output file: silence-report.txt alongside the input klippy.log.
if ($null -eq $OutputFile) {
    $OutputFile = Join-Path -Path (Split-Path -Parent (Resolve-Path $KlippyLog).Path) `
                            -ChildPath 'silence-report.txt'
}

# Start-Transcript captures EVERYTHING (Write-Host, formatted strings,
# everything visible on the console) without rewriting any output call.
# Use $script:TranscriptStarted to know whether to stop it cleanly at exit.
$script:TranscriptStarted = $false
if ($OutputFile -and $OutputFile -ne '-' -and $OutputFile.Length -gt 0) {
    try {
        Start-Transcript -Path $OutputFile -Force | Out-Null
        $script:TranscriptStarted = $true
        Write-Host "(transcript: $OutputFile)" -ForegroundColor DarkGray
    } catch {
        Write-Warning "Could not open transcript file '$OutputFile': $($_.Exception.Message)"
    }
}

try {

Write-Host "=== Scanning klippy.log for SILENCE markers ===" -ForegroundColor Cyan
$silences = @()
$curStart = $null

$silenceLines = Select-String -Path $KlippyLog -Pattern "tangle-tlm SILENCE_" -SimpleMatch
foreach ($m in $silenceLines) {
    $line = $m.Line
    if ($line -match 'SILENCE_START.*t=([\d.]+).*ext=([\d.-]+).*enc=(\d+).*ace_action=(\S+).*ext_pwm=([\d.]+).*layer=(\S+)') {
        $curStart = @{
            t_start       = [double]$Matches[1]
            ext_start     = [double]$Matches[2]
            enc_start     = [int]$Matches[3]
            ace_action    = $Matches[4]
            ext_pwm       = [double]$Matches[5]
            layer         = $Matches[6]
            line_nr_start = $m.LineNumber
        }
    } elseif ($line -match 'SILENCE_END.*t=([\d.]+).*ext=([\d.-]+).*enc=(\d+).*dur=([\d.]+).*ext_delta=([\d.-]+)') {
        if ($curStart) {
            $silences += [PSCustomObject]@{
                t_start          = $curStart.t_start
                t_end            = [double]$Matches[1]
                ext_start        = $curStart.ext_start
                ext_end          = [double]$Matches[2]
                enc_start        = $curStart.enc_start
                enc_end          = [int]$Matches[3]
                duration_s       = [double]$Matches[4]
                ext_delta        = [double]$Matches[5]
                ace_action_start = $curStart.ace_action
                ext_pwm_start    = $curStart.ext_pwm
                layer            = $curStart.layer
                line_nr_start    = $curStart.line_nr_start
                line_nr_end      = $m.LineNumber
            }
            $curStart = $null
        }
    }
}
Write-Host "Found $($silences.Count) complete silence windows"
if ($silences.Count -eq 0) {
    Write-Host "(no SILENCE_START/END markers in klippy.log)" -ForegroundColor Yellow
    exit 0
}

# Apply MinDurationS filter
$silences = @($silences | Where-Object { $_.duration_s -ge $MinDurationS })
Write-Host "After MinDurationS=$MinDurationS filter: $($silences.Count) windows"
if ($silences.Count -eq 0) { exit 0 }

# Index klippy.log: each line gets a LogicalT (= most recent Stats time above it)
Write-Host ""
Write-Host "=== Indexing klippy.log timestamps ===" -ForegroundColor Cyan
$klogIdx = @()
$ln = 0
$lastStatsT = $null
foreach ($l in (Get-Content $KlippyLog)) {
    $ln++
    $statsT = $null
    if ($l -match '^Stats (\d+\.\d+):') {
        $statsT = [double]$Matches[1]
        $lastStatsT = $statsT
    }
    $klogIdx += [PSCustomObject]@{
        LineNumber = $ln
        Line       = $l
        StatsT     = $statsT
        LogicalT   = $lastStatsT
    }
}
Write-Host "Indexed $($klogIdx.Count) lines"

Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  $($silences.Count) SILENCE WINDOW(S) FOUND" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host ""

$idx = 0
foreach ($s in ($silences | Sort-Object t_start)) {
    $idx++
    $wStart = $s.t_start - $ContextSeconds
    $wEnd   = $s.t_end   + $ContextSeconds
    $missing = 0
    if ($s.ext_delta -gt 0) {
        $missing = [int][math]::Round($s.ext_delta / 1.09)
    }

    Write-Host "----------------------------------------" -ForegroundColor Yellow
    Write-Host "SILENCE #$idx | layer=$($s.layer) | dur=$($s.duration_s)s | ext_delta=$($s.ext_delta)mm | ~$missing pulses missing" -ForegroundColor Yellow
    Write-Host "  t=$($s.t_start) to $($s.t_end)  ext=$($s.ext_start) to $($s.ext_end)  enc=$($s.enc_start) to $($s.enc_end)" -ForegroundColor Yellow
    Write-Host "  ace_action_at_start=$($s.ace_action_start)  ext_pwm_at_start=$($s.ext_pwm_start)" -ForegroundColor Yellow
    Write-Host "  klippy.log lines $($s.line_nr_start) to $($s.line_nr_end), context window $wStart to $wEnd s" -ForegroundColor Yellow
    Write-Host ""

    # Stats summary — extract each field individually because the Stats
    # line layout varies (mcu_awake/srtt appear in the per-MCU block at
    # the start, print_time/buffer_time/print_stall at the end).
    $statsInWindow = $klogIdx | Where-Object {
        $null -ne $_.StatsT -and $_.StatsT -ge $wStart -and $_.StatsT -le $wEnd
    }
    Write-Host "  Stats lines in window ($($statsInWindow.Count)):"
    # Show first/middle/last + anything with non-zero stall (anomalies)
    $statsArr = @($statsInWindow)
    $statsAnomalies = $statsArr | Where-Object {
        $_.Line -match 'print_stall=(\d+)' -and [int]$Matches[1] -gt 0
    }
    $statsToShow = @()
    if ($statsArr.Count -gt 0) { $statsToShow += $statsArr[0] }
    if ($statsArr.Count -gt 2) { $statsToShow += $statsArr[[int]($statsArr.Count / 2)] }
    if ($statsArr.Count -gt 1) { $statsToShow += $statsArr[-1] }
    $statsToShow += $statsAnomalies
    foreach ($st in ($statsToShow | Sort-Object StatsT -Unique)) {
        $line = $st.Line
        $pt    = if ($line -match 'print_time=(\S+)')    { $Matches[1] } else { '?' }
        $buf   = if ($line -match 'buffer_time=(\S+)')   { $Matches[1] } else { '?' }
        $stall = if ($line -match 'print_stall=(\d+)')   { $Matches[1] } else { '?' }
        $awk   = if ($line -match 'mcu: mcu_awake=(\S+)'){ $Matches[1] } else { '?' }
        $srtt  = if ($line -match 'mcu:[^|]*?srtt=(\S+)'){ $Matches[1] } else { '?' }
        $rtx   = if ($line -match 'mcu:[^|]*?bytes_retransmit=(\d+)') { $Matches[1] } else { '?' }
        $sl    = if ($line -match 'sysload=(\S+)')       { $Matches[1] } else { '?' }
        Write-Host ("    t={0,-9} pt={1,-9} buf={2,-6} stall={3} mcu_aw={4} srtt={5} retx={6} sysload={7}" -f `
                    $st.StatsT, $pt, $buf, $stall, $awk, $srtt, $rtx, $sl)
    }
    if ($statsAnomalies.Count -gt 0) {
        Write-Host ("    -> $($statsAnomalies.Count) Stats line(s) had print_stall > 0") -ForegroundColor Red
    }

    # Non-Stats lines in window
    Write-Host ""
    Write-Host "  Non-Stats klippy.log activity in window:"
    $nonStats = @($klogIdx | Where-Object {
        ($null -ne $_.LogicalT) `
        -and ($_.LogicalT -ge $wStart) `
        -and ($_.LogicalT -le $wEnd) `
        -and ($null -eq $_.StatsT) `
        -and ($_.Line.Trim().Length -gt 0) `
        -and ($_.Line -notmatch '^Receive:') `
        -and ($_.Line -notmatch '^Send:') `
        -and ($_.Line -notmatch '^Dump ') `
        -and ($_.Line -notmatch '^TMC ') `
        -and ($_.Line -notmatch '^>>>') `
        -and ($_.Line -notmatch '^---')
    })
    if ($nonStats.Count -eq 0) {
        Write-Host "    (no non-Stats activity in window)" -ForegroundColor DarkGray
    } else {
        Write-Host "    ($($nonStats.Count) lines):"
        foreach ($n in $nonStats) {
            $preview = $n.Line
            if ($preview.Length -gt 180) {
                $preview = $preview.Substring(0, 180) + " [...]"
            }
            Write-Host ("    L{0,-6} t~{1,-9} {2}" -f $n.LineNumber, $n.LogicalT, $preview)
        }
    }
    Write-Host ""
}

$totalDur = ($silences | Measure-Object duration_s -Sum).Sum
$totalExt = ($silences | Measure-Object ext_delta  -Sum).Sum
$totalMissing = [int][math]::Round($totalExt / 1.09)
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "Summary: $($silences.Count) silences, total $([math]::Round($totalDur, 1))s, total $([math]::Round($totalExt, 1))mm ext_delta, ~$totalMissing missing pulses"
Write-Host "========================================" -ForegroundColor Magenta

} finally {
    if ($script:TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}

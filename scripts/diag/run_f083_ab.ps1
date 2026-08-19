# F083 local full-instance A/B orchestrator (3 configs, decision-focused).
# Starts a local Nous instance per config (localhost:8000, local docker Postgres),
# runs the follow-up probe, stops the instance. Leaves background workers on
# (per user). Writes reports/f083_probe_<label>.json + a run log.
$ErrorActionPreference = "Continue"
$env:UV_LINK_MODE = "copy"
$logRoot = "reports/_f083_ab_run.log"
function Log($m) { $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m; Write-Output $line; Add-Content -Path $logRoot -Value $line }

# Config matrix: label -> extra F083 env (base = code defaults: A1/C1/C2 on, A2/B off)
$configs = @(
    @{ label = "baseline"; env = @{} },
    @{ label = "a2_on";    env = @{ "NOUS_FOLLOWUP_FIRST_TURN_EPISODE" = "true" } },
    @{ label = "b_on";     env = @{ "NOUS_FOLLOWUP_FIRST_TURN_EPISODE" = "true"; "NOUS_EPISODE_OPEN_THREADS" = "true" } }
)

function Stop-Instance($proc) {
    try { if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } } catch {}
    Start-Sleep -Seconds 2
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*nous.main*' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
    Start-Sleep -Seconds 2
}

Set-Content -Path $logRoot -Value ("F083 A/B run start " + (Get-Date))
foreach ($c in $configs) {
    $label = $c.label
    Log "=== CONFIG: $label ==="
    # base env every config needs
    $env:NOUS_PORT = "8000"; $env:DB_HOST = "localhost"
    # clear the toggleable F083 flags, then apply this config's overrides
    Remove-Item Env:NOUS_FOLLOWUP_FIRST_TURN_EPISODE -ErrorAction SilentlyContinue
    Remove-Item Env:NOUS_EPISODE_OPEN_THREADS -ErrorAction SilentlyContinue
    foreach ($k in $c.env.Keys) { Set-Item -Path "Env:$k" -Value $c.env[$k] }
    Log ("flags: FIRST_TURN_EPISODE={0} OPEN_THREADS={1}" -f $env:NOUS_FOLLOWUP_FIRST_TURN_EPISODE, $env:NOUS_EPISODE_OPEN_THREADS)

    $out = "reports/_f083_inst_$label.out.log"; $errl = "reports/_f083_inst_$label.err.log"
    $proc = Start-Process -FilePath "uv" -ArgumentList "run","--extra","runtime","--extra","agent","python","-m","nous.main" `
        -RedirectStandardOutput $out -RedirectStandardError $errl -PassThru -NoNewWindow
    Log "started PID=$($proc.Id), waiting for health..."
    $ok = $false
    for ($i = 0; $i -lt 70; $i++) {
        Start-Sleep -Seconds 3
        try { $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -TimeoutSec 5 -UseBasicParsing; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch {}
        if ($proc.HasExited) { Log "instance EXITED early; see $errl"; break }
    }
    if (-not $ok) { Log "config ${label}: instance NOT healthy, skipping probe"; Stop-Instance $proc; continue }
    Log "healthy after ~$($i*3)s; running probe..."
    & py scripts/diag/followup_probe.py --label $label 2>&1 | ForEach-Object { Log "probe> $_" }
    Log "probe done for $label; stopping instance"
    Stop-Instance $proc
}
Log "=== A/B run complete ==="

param(
  [int]$IntervalMs = 1500,
  [string]$Out = 'C:\mining\data\cpu.json'
)
# Headless CPU sensor bridge for the remote agent: LibreHardwareMonitor library
# (its kernel driver needs elevation) samples CPU temp/power/clock/load and drops
# a small JSON file. Works for both Intel (CPU Package / Core Max) and AMD
# (Tctl/Tdie identifiers) sensor layouts.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Add-Type -Path (Join-Path $here 'LibreHardwareMonitor\LibreHardwareMonitorLib.dll')

$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsCpuEnabled = $true
$computer.IsGpuEnabled = $false
$computer.Open()

$outDir = Split-Path -Parent $Out
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
$tmp = "$Out.tmp"

$fails = 0
while ($true) {
  $temp = $null; $package = $null; $coreMax = $null; $watts = $null
  $coresAvg = $null; $clocks = New-Object System.Collections.ArrayList; $load = $null
  try {
    foreach ($hw in $computer.Hardware) {
      $hw.Update()
      foreach ($s in $hw.Sensors) {
        if ($null -eq $s.Value) { continue }
        $id = [string]$s.Identifier
        if ($s.SensorType -eq 'Temperature') {
          if ($s.Name -match 'CPU Package') { $package = [double]$s.Value }
          elseif ($s.Name -match 'Core Max') { $coreMax = [double]$s.Value }
          elseif ($id -like '*/temperature/2') { $temp = [double]$s.Value }
          elseif ($null -eq $temp) { $temp = [double]$s.Value }
        } elseif ($s.SensorType -eq 'Power' -and $s.Name -match 'Package') {
          $watts = [double]$s.Value
        } elseif ($s.SensorType -eq 'Clock' -and $s.Name -eq 'Cores (Average)') {
          $coresAvg = [double]$s.Value
        } elseif ($s.SensorType -eq 'Clock') {
          [void]$clocks.Add([double]$s.Value)
        } elseif ($s.SensorType -eq 'Load' -and $s.Name -match 'CPU Total') {
          $load = [double]$s.Value
        }
      }
    }
    if ($null -ne $package) { $temp = $package }
    elseif ($null -ne $coreMax) { $temp = $coreMax }
    $mhz = $null
    if ($null -ne $coresAvg) { $mhz = $coresAvg }
    elseif ($clocks.Count -gt 0) { $mhz = ($clocks | Measure-Object -Average).Average }
    $fails = 0
  } catch {
    $fails++
    Start-Sleep -Milliseconds 5000
    if ($fails -gt 20) { $computer.Close(); throw }
    continue
  }
  $json = [ordered]@{
    ts       = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    temp_c   = if ($null -ne $temp) { [math]::Round($temp, 1) } else { $null }
    power_w  = if ($null -ne $watts) { [math]::Round($watts, 1) } else { $null }
    mhz      = if ($null -ne $mhz) { [math]::Round($mhz) } else { $null }
    load_pct = if ($null -ne $load) { [math]::Round($load) } else { $null }
  } | ConvertTo-Json -Compress
  Set-Content -Path $tmp -Value $json -Encoding ASCII -Force
  Move-Item -Path $tmp -Destination $Out -Force
  Start-Sleep -Milliseconds $IntervalMs
}

# Windows half of rigwatch: one long-lived PowerShell that prints a JSON line every
# couple of seconds. Started by the Python server over WSL interop (so no per-tick
# process spawn), and stopped when its stdout pipe closes.
#
# CPU numbers are percent of the whole machine (all logical processors).
# __WATCH__ is replaced with the Windows process names apps.toml asks for.
$ErrorActionPreference = 'SilentlyContinue'
$watch = @(__WATCH__)
$interval = 2.0
$gpuEvery = 5          # GPU counters cost ~1 s, so read them every 5th tick

Add-Type @"
using System; using System.Runtime.InteropServices;
public static class RW {
  [DllImport("kernel32.dll")] public static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
  [StructLayout(LayoutKind.Sequential)] public class MEM {
    public uint len = 64; public uint load; public ulong total; public ulong avail;
    public ulong pf; public ulong pfa; public ulong v; public ulong va; public ulong vx; }
  [DllImport("kernel32.dll")] public static extern bool GlobalMemoryStatusEx([In, Out] MEM m);
}
// GPU temperature and fan speed from the graphics kernel, the call Task Manager uses.
// Needs no admin rights and no vendor tool; works for any WDDM 2.4+ driver that reports it.
public static class RWT {
  [StructLayout(LayoutKind.Sequential)] struct ENUM2 { public uint n; public IntPtr p; }
  [StructLayout(LayoutKind.Sequential)] struct QAI { public uint h; public uint type; public IntPtr data; public uint size; }
  [DllImport("gdi32.dll")] static extern int D3DKMTEnumAdapters2(ref ENUM2 e);
  [DllImport("gdi32.dll")] static extern int D3DKMTQueryAdapterInfo(ref QAI q);
  static uint[] handles;          // opened once, kept for the life of the sampler
  static void Open() {
    var e = new ENUM2(); e.n = 16; e.p = Marshal.AllocHGlobal(16 * 20);       // D3DKMT_ADAPTERINFO is 20 bytes
    handles = new uint[0];
    if (D3DKMTEnumAdapters2(ref e) == 0) {
      handles = new uint[e.n];
      for (int i = 0; i < e.n; i++) handles[i] = (uint)Marshal.ReadInt32(e.p, i * 20);
    }
    Marshal.FreeHGlobal(e.p);
  }
  // {degrees C, fan rpm} of the hottest adapter that reports a temperature, or null
  public static double[] Read() {
    if (handles == null) Open();
    double[] best = null;
    IntPtr buf = Marshal.AllocHGlobal(64);                                    // D3DKMT_ADAPTER_PERFDATA
    foreach (uint h in handles) {
      for (int i = 0; i < 64; i += 4) Marshal.WriteInt32(buf, i, 0);
      var q = new QAI(); q.h = h; q.type = 62; q.data = buf; q.size = 64;     // KMTQAITYPE_ADAPTERPERFDATA
      if (D3DKMTQueryAdapterInfo(ref q) != 0) continue;
      double t = (uint)Marshal.ReadInt32(buf, 56) / 10.0;                     // tenths of a degree
      if (t > 0 && t < 150 && (best == null || t > best[0])) best = new double[] { t, (uint)Marshal.ReadInt32(buf, 48) };
    }
    Marshal.FreeHGlobal(buf);
    return best;
  }
}
"@

$ncpu = [Environment]::ProcessorCount
$cpuName = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name.Trim()
$gpu = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch 'Basic|Virtual|Parsec|Remote' } | Select-Object -First 1
$vram = 0
Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' |
  ForEach-Object { $v = $_.'HardwareInformation.qwMemorySize'; if ($v -and [uint64]$v -gt $vram) { $vram = [uint64]$v } }

[long]$pIdle = 0; [long]$pKern = 0; [long]$pUser = 0
[void][RW]::GetSystemTimes([ref]$pIdle, [ref]$pKern, [ref]$pUser)
$prev = @{}
$prevNet = @{}
$prevT = [DateTime]::UtcNow
$tick = 0
$gpuOut = $null

function NetCounters {
  $h = @{}
  foreach ($n in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
    if ($n.OperationalStatus -ne 'Up') { continue }
    if ($n.NetworkInterfaceType -eq 'Loopback' -or $n.NetworkInterfaceType -eq 'Tunnel') { continue }
    if ($n.Description -match 'Hyper-V|Virtual|Tailscale|WireGuard|Loopback|vEthernet') { continue }
    $s = $n.GetIPStatistics()
    $h[$n.Name] = @{ rx = [double]$s.BytesReceived; tx = [double]$s.BytesSent; desc = $n.Description }
  }
  $h
}
$prevNet = NetCounters

while ($true) {
  Start-Sleep -Milliseconds ([int]($interval * 1000))
  $tick++
  $now = [DateTime]::UtcNow
  $dt = ($now - $prevT).TotalSeconds
  $prevT = $now

  [long]$i = 0; [long]$k = 0; [long]$u = 0
  [void][RW]::GetSystemTimes([ref]$i, [ref]$k, [ref]$u)
  $di = $i - $pIdle; $dall = ($k - $pKern) + ($u - $pUser)      # kernel time includes idle
  $pIdle = $i; $pKern = $k; $pUser = $u
  $cpu = 0.0
  if ($dall -gt 0) { $cpu = 100.0 * ($dall - $di) / $dall }

  $m = New-Object RW+MEM
  [void][RW]::GlobalMemoryStatusEx($m)

  # per process CPU from TotalProcessorTime deltas, rolled up by process name
  $agg = @{}
  $cur = @{}
  $self = $null
  foreach ($p in [System.Diagnostics.Process]::GetProcesses()) {
    $name = $p.ProcessName
    $ticks = $null
    try { $ticks = $p.TotalProcessorTime.Ticks } catch { }
    $key = "$($p.Id)/$name"
    $c = 0.0
    if ($null -ne $ticks) {
      $cur[$key] = $ticks
      if ($prev.ContainsKey($key) -and $dt -gt 0) {
        $c = 100.0 * ($ticks - $prev[$key]) / ($dt * 1e7 * $ncpu)
        if ($c -lt 0) { $c = 0.0 }
      }
    }
    $ws = [double]$p.WorkingSet64
    if ($p.Id -eq $PID) { $self = @{ cpu = $c; ws = $ws } }
    if (-not $agg.ContainsKey($name)) { $agg[$name] = @{ name = $name; cpu = 0.0; ws = 0.0; n = 0; pids = @() } }
    $a = $agg[$name]; $a.cpu += $c; $a.ws += $ws; $a.n += 1
    if ($a.pids.Count -lt 64) { $a.pids += $p.Id }
  }
  $prev = $cur

  $list = @($agg.Values)
  $keep = @{}
  $list | Sort-Object { $_.cpu } -Descending | Select-Object -First 14 | ForEach-Object { $keep[$_.name] = $_ }
  $list | Sort-Object { $_.ws } -Descending | Select-Object -First 14 | ForEach-Object { $keep[$_.name] = $_ }
  foreach ($w in $watch + @('vmmemWSL', 'vmmem')) { if ($agg.ContainsKey($w)) { $keep[$w] = $agg[$w] } }
  $sumCpu = 0.0; $sumWs = 0.0
  foreach ($a in $list) { $sumCpu += $a.cpu; $sumWs += $a.ws }

  # GPU: Task Manager's number is the busiest engine; per process it is that process's busiest engine
  if ($tick % $gpuEvery -eq 1) {
    $engines = @{}; $byPid = @{}
    $g = Get-Counter -Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue
    if ($g) {
      foreach ($s in $g.CounterSamples) {
        if ($s.InstanceName -match '^pid_(\d+)_(.+)$') {
          $pidG = [int]$Matches[1]; $eng = $Matches[2]
          $type = ($eng -replace '^.*engtype_', '')
          $engines[$eng] = [double]$engines[$eng] + $s.CookedValue
          $prevP = $byPid[$pidG]
          if ($null -eq $prevP -or $s.CookedValue -gt $prevP.util) { $byPid[$pidG] = @{ util = $s.CookedValue; type = $type } }
        }
      }
    }
    $mem = Get-Counter -Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue
    $vused = 0.0
    if ($mem) { foreach ($s in $mem.CounterSamples) { if ($s.CookedValue -gt $vused) { $vused = $s.CookedValue } } }
    $util = 0.0
    foreach ($v in $engines.Values) { if ($v -gt $util) { $util = $v } }
    $pidName = @{}
    foreach ($a in $list) { foreach ($x in $a.pids) { $pidName[$x] = $a.name } }
    $gp = @{}
    foreach ($kv in $byPid.GetEnumerator()) {
      if ($kv.Value.util -lt 0.5) { continue }
      $n = $pidName[$kv.Key]; if (-not $n) { $n = "pid $($kv.Key)" }
      if (-not $gp.ContainsKey($n) -or $gp[$n].util -lt $kv.Value.util) { $gp[$n] = @{ name = $n; util = [math]::Min(100, $kv.Value.util); type = $kv.Value.type } }
    }
    $gpuOut = @{ util = [math]::Min(100, $util); vram_used = $vused; procs = @($gp.Values | Sort-Object { $_.util } -Descending | Select-Object -First 8) }
  }

  $temps = $null
  $gt = [RWT]::Read()
  if ($gt) { $temps = @{ gpu = $gt[0]; gpu_fan = $gt[1] } }

  $netNow = NetCounters
  $nets = @()
  foreach ($kv in $netNow.GetEnumerator()) {
    $p0 = $prevNet[$kv.Key]
    if ($p0 -and $dt -gt 0) {
      $nets += @{ name = $kv.Key; desc = $kv.Value.desc
                  rx = [math]::Max(0, ($kv.Value.rx - $p0.rx) / $dt); tx = [math]::Max(0, ($kv.Value.tx - $p0.tx) / $dt) }
    }
  }
  $prevNet = $netNow

  $out = @{
    t = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    ncpu = $ncpu; cpu_name = $cpuName; cpu = $cpu
    mem_total = [double]$m.total; mem_used = [double]($m.total - $m.avail)
    proc_cpu_sum = $sumCpu; proc_ws_sum = $sumWs; nproc = $list.Count
    procs = @($keep.Values | ForEach-Object { @{ name = $_.name; cpu = $_.cpu; ws = $_.ws; n = $_.n } })
    self = $self
    gpu_name = $gpu.Name; vram_total = [double]$vram; gpu = $gpuOut
    temps = $temps
    net = $nets
  }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress -Depth 5))
  [Console]::Out.Flush()
}

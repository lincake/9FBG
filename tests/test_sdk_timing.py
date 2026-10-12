"""Exercise the actual x86 helper wait code without entering its SDK Main."""
import json
import os
from pathlib import Path
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "assets/sdk/BaySpecSdkStream.cs"
HARNESS = r'''
using System;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;

internal static class WaitHarness {
    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetHandleInformation(IntPtr handle, out uint flags);

    private static int Main(string[] args) {
        // Loading the source assembly cannot execute Main or call the vendor DLL.
        Assembly assembly = Assembly.LoadFrom(args[0]);
        Type outer = assembly.GetType("BaySpecSdkStream", true);
        Type type = outer.GetNestedType("CadenceWaiter", BindingFlags.NonPublic);
        MethodInfo wait = type.GetMethod("WaitUntil");
        MethodInfo fields = type.GetMethod("JsonFields");
        MethodInfo fallback = type.GetMethod("UseFallback", BindingFlags.NonPublic | BindingFlags.Instance);
        FieldInfo handleField = type.GetField("timer", BindingFlags.NonPublic | BindingFlags.Instance);
        Console.Out.Flush();
        using (EventWaitHandle stop = new EventWaitHandle(false,
                args[1] == "stop_auto" ? EventResetMode.AutoReset : EventResetMode.ManualReset)) {
            object waiter = Activator.CreateInstance(type, new object[] {stop});
            IntPtr originalHandle = (IntPtr)handleField.GetValue(waiter);
            string initial = (string)fields.Invoke(waiter, null);
            Stopwatch clock = Stopwatch.StartNew();
            TimeSpan cpu = Process.GetCurrentProcess().TotalProcessorTime;
            if (args[1] == "cadence") {
                double[] intervals = new double[100];
                for (int i=0; i<intervals.Length; i++) {
                    double before = clock.Elapsed.TotalMilliseconds;
                    wait.Invoke(waiter, new object[] {clock, before+10.0});
                    intervals[i] = clock.Elapsed.TotalMilliseconds-before;
                }
                double elapsed = clock.Elapsed.TotalMilliseconds;
                double cpuMs = (Process.GetCurrentProcess().TotalProcessorTime-cpu).TotalMilliseconds;
                Array.Sort(intervals);
                Console.WriteLine("{\"hardware_started\":false,\"elapsed_ms\":"+elapsed.ToString(CultureInfo.InvariantCulture)+
                    ",\"cpu_ms\":"+cpuMs.ToString(CultureInfo.InvariantCulture)+
                    ",\"min_ms\":"+intervals[0].ToString(CultureInfo.InvariantCulture)+
                    ",\"median_ms\":"+intervals[50].ToString(CultureInfo.InvariantCulture)+
                    ",\"p95_ms\":"+intervals[95].ToString(CultureInfo.InvariantCulture)+initial+"}");
            } else if (args[1] == "fallback") {
                fallback.Invoke(waiter, new object[] {"测试注入运行时计时器失败"});
                wait.Invoke(waiter, new object[] {clock, 120.0});
                double elapsed = clock.Elapsed.TotalMilliseconds;
                double cpuMs = (Process.GetCurrentProcess().TotalProcessorTime-cpu).TotalMilliseconds;
                Thread signal = new Thread(delegate() {Thread.Sleep(25); stop.Set();});
                signal.Start();
                double start = clock.Elapsed.TotalMilliseconds;
                wait.Invoke(waiter, new object[] {clock, start+10000.0});
                double stopMs = clock.Elapsed.TotalMilliseconds-start;
                signal.Join();
                Console.WriteLine("{\"hardware_started\":false,\"elapsed_ms\":"+elapsed.ToString(CultureInfo.InvariantCulture)+
                    ",\"cpu_ms\":"+cpuMs.ToString(CultureInfo.InvariantCulture)+
                    ",\"stop_ms\":"+stopMs.ToString(CultureInfo.InvariantCulture)+(string)fields.Invoke(waiter,null)+"}");
            } else if (args[1] == "stop" || args[1] == "stop_auto") {
                Thread signal = new Thread(delegate() {Thread.Sleep(25); stop.Set();});
                signal.Start();
                wait.Invoke(waiter, new object[] {clock, 10000.0});
                signal.Join();
                bool stopped = (bool)outer.GetField("StopRequested", BindingFlags.NonPublic|BindingFlags.Static).GetValue(null);
                string reason = (string)outer.GetField("StopReason", BindingFlags.NonPublic|BindingFlags.Static).GetValue(null);
                Console.WriteLine("{\"hardware_started\":false,\"stop_ms\":"+clock.Elapsed.TotalMilliseconds.ToString(CultureInfo.InvariantCulture)+
                    ",\"stopped\":"+(stopped?"true":"false")+",\"reason\":\""+reason+"\""+initial+"}");
            } else if (args[1] == "dispose") {
                uint flags;
                bool existed = originalHandle != IntPtr.Zero && GetHandleInformation(originalHandle, out flags);
                ((IDisposable)waiter).Dispose();
                ((IDisposable)waiter).Dispose();
                bool exists = GetHandleInformation(originalHandle, out flags);
                bool rejected = false;
                try {wait.Invoke(waiter, new object[] {clock, 10.0});}
                catch (TargetInvocationException ex) {rejected = ex.InnerException is ObjectDisposedException;}
                Console.WriteLine("{\"hardware_started\":false,\"handle_existed\":"+(existed?"true":"false")+
                    ",\"handle_closed\":"+(!exists?"true":"false")+",\"disposed_wait_rejected\":"+(rejected?"true":"false")+"}");
            } else if (args[1] == "data_contract") {
                ushort sample = (ushort)outer.GetMethod("ReadBigEndianUInt16", BindingFlags.NonPublic|BindingFlags.Static)
                    .Invoke(null,new object[] {new byte[] {0x12,0x34},0});
                int points = (int)outer.GetField("SpectrumPoints",BindingFlags.NonPublic|BindingFlags.Static).GetRawConstantValue();
                Console.WriteLine("{\"hardware_started\":false,\"sample\":"+sample+",\"points\":"+points+"}");
            }
            ((IDisposable)waiter).Dispose();
        }
        return 0;
    }
}
'''


@pytest.fixture(scope="module")
def compiled_helper(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("真实 Windows 等待计时器回归仅在 Windows 执行")
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("需要现有 .NET Framework x86 编译器；测试不会安装")
    directory = tmp_path_factory.mktemp("sdk_waiter_x86")
    library, helper = directory / "Source.dll", directory / "Helper.exe"
    harness_source, harness = directory / "WaitHarness.cs", directory / "WaitHarness.exe"
    harness_source.write_text(HARNESS, encoding="utf-8")
    for target, sources, kind in ((library, [SOURCE], "library"), (helper, [SOURCE], "exe"),
                                  (harness, [harness_source], "exe")):
        completed = subprocess.run([str(compiler), "/nologo", f"/target:{kind}", "/platform:x86",
                                    "/optimize+", "/checked+", "/r:System.Management.dll",
                                    f"/out:{target}", *map(str, sources)], capture_output=True,
                                   encoding="utf-8", errors="replace", timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr
    return library, helper, harness


def run_waiter(compiled_helper, mode):
    library, _, harness = compiled_helper
    completed = subprocess.run([str(harness), str(library), mode], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    rows = [json.loads(line.lstrip("\ufeff")) for line in completed.stdout.splitlines() if line.lstrip("\ufeff").startswith("{")]
    assert rows[-1]["hardware_started"] is False
    return rows


def test_actual_x86_high_resolution_waiter_keeps_10ms_without_busy_spin(compiled_helper):
    result = run_waiter(compiled_helper, "cadence")[-1]
    assert result["high_resolution_waitable_timer_active"] is True
    assert result["cadence_wait_mechanism"] == "waitable_timer_high_resolution"
    assert result["cadence_wait_fallback_reason"] == ""
    assert result["min_ms"] >= 9.95
    assert result["median_ms"] < 14, "10ms采样等待不能重新退化为约15.6ms节拍"
    assert result["p95_ms"] < 25
    assert result["cpu_ms"] < result["elapsed_ms"] * .35, "等待不能改用持续忙循环"


@pytest.mark.parametrize("mode", ["stop", "stop_auto"])
def test_actual_waiter_stop_event_interrupts_long_deadline(compiled_helper, mode):
    result = run_waiter(compiled_helper, mode)[-1]
    assert result["stopped"] is True and result["reason"] == "explicit_stop_event"
    assert result["stop_ms"] < 300


def test_runtime_timer_failure_is_explicit_low_cpu_and_still_interruptible(compiled_helper):
    rows = run_waiter(compiled_helper, "fallback")
    assert rows[0]["type"] == "timing_warning"
    result = rows[-1]
    assert result["high_resolution_waitable_timer_active"] is False
    assert result["cadence_wait_mechanism"] == "stop_event_wait_fallback"
    assert "运行时计时器失败" in result["cadence_wait_fallback_reason"]
    assert result["elapsed_ms"] >= 119.9
    assert result["cpu_ms"] < result["elapsed_ms"] * .35
    assert result["stop_ms"] < 300


def test_helper_owned_timer_closes_once_and_rejects_reuse(compiled_helper):
    result = run_waiter(compiled_helper, "dispose")[-1]
    assert result["handle_existed"] and result["handle_closed"] and result["disposed_wait_rejected"]


def test_waiter_change_preserves_actual_512_point_byte_and_timing_contract(compiled_helper):
    assert run_waiter(compiled_helper, "data_contract")[-1] == {"hardware_started": False, "sample": 0x1234, "points": 512}
    _, helper, _ = compiled_helper
    completed = subprocess.run([str(helper), "--describe-timing", "--integration", "20", "--trigger-hz", "100", "--interval-ms", "10"],
                               capture_output=True, encoding="utf-8-sig", errors="replace", timeout=5)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["hardware_started"] is False and result["interval_ms"] == 10
    assert result["integration_register_value"] == 0 and result["lval_register_value"] == 12344
    assert result["effective_integration_us"] == 20 and result["trigger_frequency_hz"] == 100

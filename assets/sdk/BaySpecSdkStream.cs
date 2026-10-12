using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Management;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

internal static class BaySpecSdkStream
{
    private static volatile bool StopRequested = false;
    private static string StopReason = "running";
    private const string DllName = "_USB20BSDevOpDLL.dll";
    private const int ObjectBytes = 8192;
    private const int SpectrumPoints = 512;
    private const int SpectrumBytes = SpectrumPoints * 2;
    private const int VendorIdentityBufferBytes = 256;
    private const uint RequestedTimerResolutionMs = 1;
    private const string TimingProgrammingContract = "sense_2020_v1633_register_conversion_v1";

    [DllImport("winmm.dll", EntryPoint = "timeBeginPeriod")]
    private static extern uint TimeBeginPeriod(uint periodMs);

    [DllImport("winmm.dll", EntryPoint = "timeEndPeriod")]
    private static extern uint TimeEndPeriod(uint periodMs);

    private const uint CreateWaitableTimerHighResolution = 0x00000002;
    private const uint TimerModifyStateAndSynchronize = 0x00100002;
    private const uint WaitObject0 = 0;
    private const uint InfiniteWait = 0xFFFFFFFF;

    [DllImport("kernel32.dll", EntryPoint = "CreateWaitableTimerExW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWaitableTimerEx(
        IntPtr attributes, string name, uint flags, uint desiredAccess
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetWaitableTimerEx(
        IntPtr timer, ref long dueTime, int period, IntPtr completionRoutine,
        IntPtr completionArgument, IntPtr wakeContext, uint tolerableDelay
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForMultipleObjects(
        uint count, [In] IntPtr[] handles, [MarshalAs(UnmanagedType.Bool)] bool waitAll, uint milliseconds
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CancelWaitableTimer(IntPtr timer);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport(DllName, EntryPoint = "??0USB20BSDevOp@@QAE@XZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern IntPtr Ctor(IntPtr self);

    [DllImport(DllName, EntryPoint = "??1USB20BSDevOp@@QAE@XZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern void Dtor(IntPtr self);

    [DllImport(DllName, EntryPoint = "?USB20BS_InitialUSB20BSDevOp@USB20BSDevOp@@QAEXPAUHWND__@@@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern void Initial(IntPtr self, IntPtr hwnd);

    [DllImport(DllName, EntryPoint = "?USB20BS_GetCypressUsbDevCount@USB20BSDevOp@@QAEHXZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern int GetCypressUsbDevCount(IntPtr self);

    [DllImport(DllName, EntryPoint = "?USB20BS_OpenDev@USB20BSDevOp@@QAEHH@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern int OpenDev(IntPtr self, int index);

    [DllImport(DllName, EntryPoint = "?USB20BS_IsCurOpenedDevOK@USB20BSDevOp@@QAEHXZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern int IsCurOpenedDevOk(IntPtr self);

    [DllImport(DllName, EntryPoint = "?USB20BS_GetCurOpenedDevName@USB20BSDevOp@@QAEPADXZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern IntPtr GetCurOpenedDevName(IntPtr self);

    [DllImport(DllName, EntryPoint = "?USB20BS_GetCurOpenedDevVID@USB20BSDevOp@@QAEGXZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern ushort GetCurOpenedDevVid(IntPtr self);

    [DllImport(DllName, EntryPoint = "?USB20BS_GetCurOpenedDevPID@USB20BSDevOp@@QAEGXZ", CallingConvention = CallingConvention.ThisCall)]
    private static extern ushort GetCurOpenedDevPid(IntPtr self);

    // The vendor DLL exposes this read-only method, but BaySpec has not
    // supplied a public contract that labels either returned buffer as the
    // spectrometer serial number.  TOUCH therefore emits only hashes of the
    // returned identifiers and never calls them a physical serial number.
    [DllImport(DllName, EntryPoint = "?USB20BS_GetLicenseNumber@USB20BSDevOp@@QAEHPAD0@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern int GetLicenseNumber(
        IntPtr self,
        [Out] byte[] firstIdentifier,
        [Out] byte[] secondIdentifier
    );

    [DllImport(DllName, EntryPoint = "?USB20BS_SetSensorMode@USB20BSDevOp@@QAEXH@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern void SetSensorMode(IntPtr self, int mode);

    [DllImport(DllName, EntryPoint = "?USB20BS_SetIntegrationAndLVALtime@USB20BSDevOp@@QAEXKK@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern void SetIntegrationAndLvalTime(IntPtr self, uint integration, uint lval);

    [DllImport(DllName, EntryPoint = "?USB20BS_SnapShotGetData@USB20BSDevOp@@QAEHPAEJH@Z", CallingConvention = CallingConvention.ThisCall)]
    private static extern int SnapShotGetData(IntPtr self, byte[] buffer, int length, int timeout);

    private static string PtrToAnsi(IntPtr ptr)
    {
        return ptr == IntPtr.Zero ? "" : (Marshal.PtrToStringAnsi(ptr) ?? "");
    }

    private static string JsonEscape(string value)
    {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    private static string Sha256Hex(string value)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(value ?? "");
        using (SHA256 sha = SHA256.Create())
        {
            byte[] digest = sha.ComputeHash(bytes);
            StringBuilder sb = new StringBuilder(digest.Length * 2);
            for (int i = 0; i < digest.Length; i++)
            {
                sb.Append(digest[i].ToString("x2", CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }
    }

    private static string ReadNullTerminatedAscii(byte[] buffer)
    {
        if (buffer == null) return "";
        int length = 0;
        while (length < buffer.Length && buffer[length] != 0)
        {
            length++;
        }
        if (length <= 0) return "";
        string value = Encoding.ASCII.GetString(buffer, 0, length).Trim();
        if (value.Length == 0) return "";
        for (int i = 0; i < value.Length; i++)
        {
            if (char.IsControl(value[i])) return "";
        }
        return value;
    }

    private sealed class WindowsPnpIdentity
    {
        public string Status = "unavailable";
        public string InstanceIdSha256 = "";
        public int MatchCount = 0;
        public string ErrorClass = "";
    }

    private static WindowsPnpIdentity ReadWindowsPnpIdentity(ushort vid, ushort pid)
    {
        WindowsPnpIdentity result = new WindowsPnpIdentity();
        string prefix = string.Format(
            CultureInfo.InvariantCulture,
            "USB\\VID_{0:X4}&PID_{1:X4}",
            vid,
            pid
        );
        HashSet<string> matches = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using (ManagementObjectSearcher searcher = new ManagementObjectSearcher(
                "SELECT PNPDeviceID, Status, ConfigManagerErrorCode FROM Win32_PnPEntity"
            ))
            using (ManagementObjectCollection collection = searcher.Get())
            {
                foreach (ManagementObject item in collection)
                {
                    string instanceId = Convert.ToString(item["PNPDeviceID"], CultureInfo.InvariantCulture);
                    if (string.IsNullOrWhiteSpace(instanceId)) continue;
                    instanceId = instanceId.Trim();
                    if (!instanceId.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) continue;

                    string status = Convert.ToString(item["Status"], CultureInfo.InvariantCulture);
                    int configError = -1;
                    object configValue = item["ConfigManagerErrorCode"];
                    if (configValue != null)
                    {
                        int.TryParse(
                            Convert.ToString(configValue, CultureInfo.InvariantCulture),
                            NumberStyles.Integer,
                            CultureInfo.InvariantCulture,
                            out configError
                        );
                    }
                    // A directly opened USB device must also be present and
                    // healthy in Windows.  Ignore stale/non-present entries.
                    if (!string.Equals(status, "OK", StringComparison.OrdinalIgnoreCase)
                        || configError != 0)
                    {
                        continue;
                    }
                    matches.Add(instanceId.ToUpperInvariant());
                }
            }
            result.MatchCount = matches.Count;
            if (matches.Count == 1)
            {
                foreach (string value in matches)
                {
                    result.InstanceIdSha256 = Sha256Hex(value);
                    break;
                }
                result.Status = "available";
            }
            else if (matches.Count == 0)
            {
                result.Status = "not_found";
            }
            else
            {
                result.Status = "ambiguous";
            }
        }
        catch (Exception ex)
        {
            // The identity path must fail closed downstream, but the helper
            // must keep its JSON protocol valid even on a damaged WMI stack.
            result.Status = "error";
            result.ErrorClass = ex.GetType().FullName ?? "System.Exception";
        }
        return result;
    }

    private sealed class VendorIdentifierReadback
    {
        public string Status = "unavailable";
        public int? ReturnCode = null;
        public List<string> IdentifierHashes = new List<string>();
        public string ErrorClass = "";
    }

    private static VendorIdentifierReadback ReadVendorIdentifiers(IntPtr obj)
    {
        VendorIdentifierReadback result = new VendorIdentifierReadback();
        try
        {
            byte[] first = new byte[VendorIdentityBufferBytes];
            byte[] second = new byte[VendorIdentityBufferBytes];
            result.ReturnCode = GetLicenseNumber(obj, first, second);
            string firstValue = ReadNullTerminatedAscii(first);
            string secondValue = ReadNullTerminatedAscii(second);
            if (!string.IsNullOrEmpty(firstValue))
            {
                result.IdentifierHashes.Add(Sha256Hex(firstValue));
            }
            if (!string.IsNullOrEmpty(secondValue))
            {
                string secondHash = Sha256Hex(secondValue);
                if (!result.IdentifierHashes.Contains(secondHash))
                {
                    result.IdentifierHashes.Add(secondHash);
                }
            }
            result.Status = result.IdentifierHashes.Count > 0
                ? "available"
                : "empty";
        }
        catch (Exception ex)
        {
            result.Status = "error";
            result.ErrorClass = ex.GetType().FullName ?? "System.Exception";
        }
        return result;
    }

    private static string JsonStringArray(IEnumerable<string> values)
    {
        StringBuilder sb = new StringBuilder();
        sb.Append('[');
        bool first = true;
        foreach (string value in values)
        {
            if (!first) sb.Append(',');
            sb.Append('"').Append(JsonEscape(value ?? "")).Append('"');
            first = false;
        }
        sb.Append(']');
        return sb.ToString();
    }

    private static void WriteLine(string json)
    {
        Console.WriteLine(json);
        Console.Out.Flush();
    }

    private static ushort ReadBigEndianUInt16(byte[] data, int offset)
    {
        return (ushort)((data[offset] << 8) | data[offset + 1]);
    }

    private static string CountsJson(ushort[] counts)
    {
        StringBuilder sb = new StringBuilder(counts.Length * 6);
        sb.Append('[');
        for (int i = 0; i < counts.Length; i++)
        {
            if (i > 0) sb.Append(',');
            sb.Append(counts[i].ToString(CultureInfo.InvariantCulture));
        }
        sb.Append(']');
        return sb.ToString();
    }

    private static int IntArg(string[] args, string name, int defaultValue)
    {
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == name)
            {
                int value;
                if (int.TryParse(args[i + 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out value))
                {
                    return value;
                }
            }
        }
        return defaultValue;
    }

    private static bool TryOptionalIntArg(
        string[] args,
        string name,
        int defaultValue,
        out int value
    )
    {
        value = defaultValue;
        bool seen = false;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] != name) continue;
            if (seen || i + 1 >= args.Length) return false;
            int parsed;
            if (!int.TryParse(
                args[i + 1],
                NumberStyles.Integer,
                CultureInfo.InvariantCulture,
                out parsed
            ))
            {
                return false;
            }
            value = parsed;
            seen = true;
            i++;
        }
        return true;
    }

    private static bool HasArg(string[] args, string name)
    {
        foreach (string arg in args)
        {
            if (arg == name) return true;
        }
        return false;
    }

    private sealed class TimingRequest
    {
        public int IntegrationUs;
        public int TriggerFrequencyHz;
        public uint IntegrationRegister;
        public uint LvalRegister;
        public double EffectiveIntegrationUs;
    }

    private static TimingRequest DescribeTiming(int integrationUs, int triggerFrequencyHz)
    {
        // Reproduces Sense 2020 V1633's host conversion at VA 0x405200.
        // The DLL takes register values, not microseconds. Its 0x10001DC0
        // entry serializes these two uint values without converting units.
        // No vendor/device operation occurs in this function.
        uint integrationRegister = checked((uint)Math.Truncate(
            (integrationUs - 20) / 0.8 + 0.5
        ));
        uint lvalRegister = checked((uint)Math.Truncate(
            (1000000.0 / triggerFrequencyHz - 125.0) / 0.8 + 0.5
        ));
        if (lvalRegister < integrationRegister) lvalRegister = integrationRegister;
        return new TimingRequest {
            IntegrationUs = integrationUs,
            TriggerFrequencyHz = triggerFrequencyHz,
            IntegrationRegister = integrationRegister,
            LvalRegister = lvalRegister,
            EffectiveIntegrationUs = 20.0 + 0.8 * integrationRegister,
        };
    }

    private static string TimingJsonFields(TimingRequest timing)
    {
        return ",\"integration\":" + timing.IntegrationUs.ToString(CultureInfo.InvariantCulture) +
            ",\"timing_programming_contract\":\"" + TimingProgrammingContract + "\"" +
            ",\"integration_register_value\":" + timing.IntegrationRegister.ToString(CultureInfo.InvariantCulture) +
            ",\"lval_register_value\":" + timing.LvalRegister.ToString(CultureInfo.InvariantCulture) +
            ",\"trigger_frequency_hz\":" + timing.TriggerFrequencyHz.ToString(CultureInfo.InvariantCulture) +
            ",\"effective_integration_us\":" + timing.EffectiveIntegrationUs.ToString("0.########", CultureInfo.InvariantCulture) +
            ",\"timing_settings_readback_verified\":false";
    }

    private sealed class CadenceWaiter : IDisposable
    {
        private readonly EventWaitHandle stopEvent;
        private IntPtr timer = IntPtr.Zero;
        private bool disposed = false;
        private string fallbackReason = "";
        private const double MaxStopCheckMs = 2.0;

        public CadenceWaiter(EventWaitHandle stopEvent)
        {
            this.stopEvent = stopEvent;
            try
            {
                timer = CreateWaitableTimerEx(
                    IntPtr.Zero, null, CreateWaitableTimerHighResolution, TimerModifyStateAndSynchronize
                );
                if (timer == IntPtr.Zero)
                {
                    UseFallback("CreateWaitableTimerExW 错误 " + Marshal.GetLastWin32Error());
                }
            }
            catch (EntryPointNotFoundException)
            {
                UseFallback("系统不提供高分辨率等待计时器接口");
            }
        }

        public string JsonFields()
        {
            return ",\"cadence_wait_mechanism\":\"" +
                (timer != IntPtr.Zero ? "waitable_timer_high_resolution" : "stop_event_wait_fallback") + "\"" +
                ",\"high_resolution_waitable_timer_active\":" + (timer != IntPtr.Zero ? "true" : "false") +
                ",\"cadence_wait_fallback_reason\":\"" + JsonEscape(fallbackReason) + "\"" +
                ",\"cadence_wait_max_stop_check_ms\":2";
        }

        private void UseFallback(string reason)
        {
            if (timer != IntPtr.Zero)
            {
                CancelWaitableTimer(timer);
                CloseHandle(timer);
                timer = IntPtr.Zero;
            }
            fallbackReason = reason;
            WriteLine("{\"type\":\"timing_warning\",\"message\":\"高分辨率采样等待不可用；已退回可中止普通等待，实际节拍需检查\"" +
                      JsonFields() + "}");
        }

        private bool Stopping()
        {
            if (StopRequested) return true;
            if (!stopEvent.WaitOne(0)) return false;
            RecordEventStop();
            return true;
        }

        private void RecordEventStop()
        {
            if (StopRequested) return;
            StopReason = "explicit_stop_event";
            StopRequested = true;
        }

        public void WaitUntil(Stopwatch clock, double targetMs)
        {
            if (disposed) throw new ObjectDisposedException("CadenceWaiter");
            while (!Stopping())
            {
                double remainingMs = targetMs - clock.Elapsed.TotalMilliseconds;
                if (remainingMs <= 0.0) return;
                double waitMs = Math.Min(remainingMs, MaxStopCheckMs);
                if (timer != IntPtr.Zero)
                {
                    // Relative due time uses negative 100 ns units. One-shot,
                    // zero-tolerance waits do not depend on timeBeginPeriod or
                    // a foreground window. The stop event takes index priority.
                    long dueTime = -Math.Max(1L, (long)Math.Ceiling(waitMs * 10000.0));
                    if (!SetWaitableTimerEx(timer, ref dueTime, 0, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 0))
                    {
                        UseFallback("SetWaitableTimerEx 错误 " + Marshal.GetLastWin32Error());
                        continue;
                    }
                    IntPtr[] handles = { stopEvent.SafeWaitHandle.DangerousGetHandle(), timer };
                    uint result = WaitForMultipleObjects(2, handles, false, InfiniteWait);
                    GC.KeepAlive(stopEvent);
                    if (result == WaitObject0)
                    {
                        RecordEventStop();
                        return;
                    }
                    if (result != WaitObject0 + 1)
                    {
                        UseFallback("WaitForMultipleObjects 错误 " + Marshal.GetLastWin32Error());
                    }
                }
                else
                {
                    // Unsupported systems are explicit and may have coarse
                    // timing. Never replace failure with a CPU-burning spin.
                    if (stopEvent.WaitOne(Math.Max(1, (int)Math.Ceiling(waitMs))))
                    {
                        RecordEventStop();
                        return;
                    }
                }
            }
        }

        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            if (timer != IntPtr.Zero)
            {
                CancelWaitableTimer(timer);
                CloseHandle(timer);
                timer = IntPtr.Zero;
            }
        }
    }

    private static int Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        int intervalMs;
        if (
            !TryOptionalIntArg(args, "--interval-ms", 10, out intervalMs)
            || intervalMs < 5 || intervalMs > 2000
        )
        {
            WriteLine(
                "{\"type\":\"error\",\"message\":\"unsupported interval request; expected one integer from 5 to 2000 milliseconds\"}"
            );
            return 5;
        }
        int timeoutMs = Math.Max(100, IntArg(args, "--timeout-ms", 1000));
        int integration;
        if (
            !TryOptionalIntArg(args, "--integration", 70, out integration)
            || integration < 20 || integration > 10000000
        )
        {
            WriteLine(
                "{\"type\":\"error\",\"message\":\"unsupported integration request; expected one integer from 20 to 10000000 microseconds\"}"
            );
            return 5;
        }
        int triggerFrequencyHz;
        if (
            !TryOptionalIntArg(args, "--trigger-hz", 100, out triggerFrequencyHz)
            || triggerFrequencyHz < 1 || triggerFrequencyHz > 5000
        )
        {
            WriteLine(
                "{\"type\":\"error\",\"message\":\"unsupported trigger frequency request; expected one integer from 1 to 5000 hertz\"}"
            );
            return 5;
        }
        int maxFrames = IntArg(args, "--frames", 0);
        int sensorMode = IntArg(args, "--sensor-mode", 1);
        int validatedSensorMode;
        if (
            !TryOptionalIntArg(args, "--sensor-mode", 1, out validatedSensorMode)
            || HasArg(args, "--linear")
        )
        {
            WriteLine(
                "{\"type\":\"error\",\"message\":\"unsupported sensor mode; use --sensor-mode 0 or --sensor-mode 1\"}"
            );
            return 5;
        }
        sensorMode = validatedSensorMode;
        if (sensorMode != 0 && sensorMode != 1)
        {
            WriteLine(
                "{\"type\":\"error\",\"message\":\"unsupported sensor mode; expected 0 (High Sensitivity) or 1 (High Dynamic Range)\",\"sensor_mode\":" +
                sensorMode.ToString(CultureInfo.InvariantCulture) + "}"
            );
            return 5;
        }
        string sensorModeLabel = sensorMode == 0
            ? "High Sensitivity"
            : "High Dynamic Range";

        TimingRequest timing = DescribeTiming(integration, triggerFrequencyHz);
        if (HasArg(args, "--describe-timing"))
        {
            WriteLine(
                "{\"type\":\"timing_description\",\"hardware_started\":false" +
                ",\"interval_ms\":" + intervalMs.ToString(CultureInfo.InvariantCulture) +
                TimingJsonFields(timing) + "}"
            );
            return 0;
        }

        EventWaitHandle stopEvent = null;
        string stopEventName = "";
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--stop-event") stopEventName = args[i + 1];
        }
        if (stopEventName.Length > 0)
        {
            try { stopEvent = EventWaitHandle.OpenExisting(stopEventName); }
            catch (Exception ex)
            {
                WriteLine("{\"type\":\"error\",\"message\":\"无法打开受控停止事件: " + JsonEscape(ex.GetType().FullName) + "\"}");
                return 5;
            }
        }

        EventWaitHandle cadenceStopEvent = stopEvent ?? new EventWaitHandle(false, EventResetMode.ManualReset);
        CadenceWaiter cadenceWaiter = null;
        IntPtr obj = Marshal.AllocHGlobal(ObjectBytes);
        bool oneMillisecondTimerActive = false;
        try
        {
            cadenceWaiter = new CadenceWaiter(cadenceStopEvent);
            for (int i = 0; i < ObjectBytes; i++)
            {
                Marshal.WriteByte(obj, i, 0);
            }
            Ctor(obj);
            Initial(obj, IntPtr.Zero);

            int count = GetCypressUsbDevCount(obj);
            if (count <= 0)
            {
                WriteLine("{\"type\":\"error\",\"message\":\"no USB20BS device found\",\"device_count\":" + count + "}");
                return 2;
            }
            if (count != 1)
            {
                WriteLine(
                    "{\"type\":\"error\",\"message\":\"9FBG 需要且只能连接一个 USB20BS 设备\",\"device_count\":" + count + "}"
                );
                return 3;
            }

            const int deviceIndex = 0;
            int openResult = OpenDev(obj, deviceIndex);
            int ok = IsCurOpenedDevOk(obj);
            string name = PtrToAnsi(GetCurOpenedDevName(obj));
            if (ok <= 0)
            {
                WriteLine(
                    "{\"type\":\"error\",\"message\":\"USB20BS device index 0 did not report an open/ready state\",\"device_count\":" + count + ",\"device_index\":" + deviceIndex + ",\"open_result\":" + openResult + "}"
                );
                    return 4;
            }
            ushort vid = GetCurOpenedDevVid(obj);
            ushort pid = GetCurOpenedDevPid(obj);
            // Both probes are read-only.  The first is a Windows device
            // instance identity and the second is a vendor-specific opaque
            // identifier.  Neither is labelled as a physical serial unless
            // BaySpec documents that relationship.
            WindowsPnpIdentity windowsPnp = ReadWindowsPnpIdentity(vid, pid);
            VendorIdentifierReadback vendorIdentifiers = ReadVendorIdentifiers(obj);
            SetSensorMode(obj, sensorMode);
            SetIntegrationAndLvalTime(obj, timing.IntegrationRegister, timing.LvalRegister);
            uint timerResolutionResult = TimeBeginPeriod(RequestedTimerResolutionMs);
            oneMillisecondTimerActive = timerResolutionResult == 0;
            WriteLine(
                "{\"type\":\"status\",\"process_bitness\":\"x86\",\"device_count\":" + count +
                ",\"device_index\":" + deviceIndex +
                ",\"open_result\":" + openResult +
                ",\"is_open_ok\":" + ok +
                ",\"device_name\":\"" + JsonEscape(name) + "\"" +
                ",\"vid\":\"0x" + vid.ToString("X4") + "\"" +
                ",\"pid\":\"0x" + pid.ToString("X4") + "\"" +
                ",\"windows_pnp_identity_status\":\"" + JsonEscape(windowsPnp.Status) + "\"" +
                ",\"windows_pnp_instance_id_sha256\":\"" + JsonEscape(windowsPnp.InstanceIdSha256) + "\"" +
                ",\"windows_pnp_match_count\":" + windowsPnp.MatchCount +
                ",\"windows_pnp_identity_error_class\":\"" + JsonEscape(windowsPnp.ErrorClass) + "\"" +
                ",\"vendor_identifier_readback_status\":\"" + JsonEscape(vendorIdentifiers.Status) + "\"" +
                ",\"vendor_identifier_readback_return_code\":" + (
                    vendorIdentifiers.ReturnCode.HasValue
                    ? vendorIdentifiers.ReturnCode.Value.ToString(CultureInfo.InvariantCulture)
                    : "null"
                ) +
                ",\"vendor_identifier_sha256\":" + JsonStringArray(vendorIdentifiers.IdentifierHashes) +
                ",\"vendor_identifier_readback_error_class\":\"" + JsonEscape(vendorIdentifiers.ErrorClass) + "\"" +
                 ",\"sensor_mode\":" + sensorMode +
                 ",\"sensor_mode_label\":\"" + JsonEscape(sensorModeLabel) + "\"" +
                 TimingJsonFields(timing) +
                 ",\"interval_ms\":" + intervalMs +
                 ",\"timeout_ms\":" + timeoutMs +
                 ",\"spectrum_points\":" + SpectrumPoints +
                 ",\"timer_resolution_requested_ms\":" + RequestedTimerResolutionMs +
                 ",\"timer_resolution_active\":" + (oneMillisecondTimerActive ? "true" : "false") +
                 ",\"timer_resolution_result\":" + timerResolutionResult +
                 cadenceWaiter.JsonFields() +
                ",\"byte_order\":\"big_endian_uint16\"}"
            );

            byte[] buffer = new byte[SpectrumBytes];
            Stopwatch monotonicClock = Stopwatch.StartNew();
            int attempt = 0;
            int successfulFrames = 0;
            Thread stopReader = new Thread(delegate() {
                try {
                    string command;
                    while ((command = Console.ReadLine()) != null) {
                        if (command.Trim() == "stop") { StopReason = "explicit_stop_command"; StopRequested = true; cadenceStopEvent.Set(); return; }
                    }
                    // A detached/background process may not provide a usable
                    // Console stdin handle. EOF is not operator permission to
                    // stop acquisition; only the exact command stops it.
                    return;
                } catch {
                    // Source-side watchdog and process close remain available.
                    // Never turn a Console reader exception into silent stop.
                    return;
                }
            });
            stopReader.IsBackground = true;
            if (stopEvent == null) stopReader.Start();
            while (!StopRequested && (maxFrames <= 0 || successfulFrames < maxFrames))
            {
                if (stopEvent != null && stopEvent.WaitOne(0))
                {
                    StopReason = "explicit_stop_event";
                    StopRequested = true;
                    break;
                }
                // Keep the *attempt start* cadence at intervalMs.  Sleeping the
                // full interval after SnapShotGetData would make the real
                // period equal to acquisition time + intervalMs and silently
                // undersample every paper session.
                double attemptStartedMs = monotonicClock.Elapsed.TotalMilliseconds;
                int frame = attempt;
                attempt++;
                int result = SnapShotGetData(obj, buffer, buffer.Length, timeoutMs);
                long nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                double monotonicMs = monotonicClock.Elapsed.TotalMilliseconds;
                if (result > 0)
                {
                    ushort[] counts = new ushort[SpectrumPoints];
                    int peakIndex = 0;
                    ushort peakValue = 0;
                    long sum = 0;
                    for (int i = 0; i < SpectrumPoints; i++)
                    {
                        ushort value = ReadBigEndianUInt16(buffer, i * 2);
                        counts[i] = value;
                        sum += value;
                        if (value > peakValue)
                        {
                            peakValue = value;
                            peakIndex = i;
                        }
                    }
                    WriteLine(
                        "{\"type\":\"spectrum\",\"timestamp_ms\":" + nowMs +
                        ",\"monotonic_ms\":" + monotonicMs.ToString("F3", CultureInfo.InvariantCulture) +
                        ",\"frame_index\":" + frame +
                        ",\"snapshot_start_monotonic_ms\":" + attemptStartedMs.ToString("F3", CultureInfo.InvariantCulture) +
                        ",\"snapshot_result\":" + result +
                        TimingJsonFields(timing) +
                        cadenceWaiter.JsonFields() +
                        ",\"spectrum_points\":" + SpectrumPoints +
                        ",\"byte_order\":\"big_endian_uint16\"" +
                        ",\"peak_index\":" + peakIndex +
                        ",\"peak_intensity\":" + peakValue +
                        ",\"mean_intensity\":" + (sum / (double)SpectrumPoints).ToString("F3", CultureInfo.InvariantCulture) +
                        ",\"counts\":" + CountsJson(counts) + "}"
                    );
                    successfulFrames++;
                }
                else
                {
                    WriteLine("{\"type\":\"warning\",\"timestamp_ms\":" + nowMs + ",\"monotonic_ms\":" + monotonicMs.ToString("F3", CultureInfo.InvariantCulture) + ",\"frame_index\":" + frame + ",\"snapshot_result\":" + result + "}");
                }
                // A slow SDK call is intentionally not followed by catch-up
                // bursts.  Its enlarged frame interval remains visible to the
                // recorder's monotonic-timing quality gate.
                cadenceWaiter.WaitUntil(
                    monotonicClock,
                    attemptStartedMs + intervalMs
                );
            }
            WriteLine("{\"type\":\"stopped\",\"reason\":\"" + StopReason + "\",\"successful_frames\":" + successfulFrames + ",\"attempts\":" + attempt + cadenceWaiter.JsonFields() + "}");
            return 0;
        }
        catch (Exception ex)
        {
            WriteLine("{\"type\":\"error\",\"message\":\"" + JsonEscape(ex.GetType().FullName + ": " + ex.Message) + "\"}");
            return 1;
        }
        finally
        {
            if (cadenceWaiter != null) cadenceWaiter.Dispose();
            if (oneMillisecondTimerActive)
            {
                try { TimeEndPeriod(RequestedTimerResolutionMs); } catch { }
            }
            try { Dtor(obj); } catch { }
            Marshal.FreeHGlobal(obj);
            if (stopEvent != null) stopEvent.Close();
            else cadenceStopEvent.Close();
        }
    }
}

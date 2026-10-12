# 9FBG 0.1.0 设备接口与时间语义核查

本文件记录 2026-10-09 的源码、厂商资料与历史实测记录核查。适配层测试没有连接设备；后续现场采集证据另存，不由这些离线检查替代。

## 512 点光谱及波长轴

BaySpec `_USB20BSDevOpDLL.dll` 为 32 位 C++ DLL。独立的 x86 helper 调用 `USB20BS_SnapShotGetData`，按大端 uint16 解释其 1024 字节数据；输出 512 个计数原值，9FBG 不平滑、不扣背景、不做工厂强度修正、不叠加平均。厂商 DLL 内部处理未公开，因此不能声称已证明为探测器电学原始数据。

**SDK 当前接口不回传波长轴。** 9FBG 使用实际设备 F1520808 的厂商 Sense 导出文件 `Spectrum_F1520808_20260923143815.csv` 中的 512 个波长校准点，独立随软件保存且双重校验原 CSV SHA-256 和大端 float64 波长数组 SHA-256。波长轴不是截图估计、线性插值或硬编码峰位置。若更换设备或标定轴，必须提供对应实际设备标定文件；目前不能把已打开的 USB 设备物理序列号与 F1520808 自动等同。

该轴起止 1507.5447～1594.4369 nm，点间距最小 0.1570 nm、最大 0.1850 nm、平均 0.1700434442 nm、标准差 0.0075096045 nm；采用相对容差 0.0001、绝对容差 1e-7 nm 检查，**波长轴不均匀**。算法必须在实际各点波长上拟合。

原厂商 CSV 的强度列标注 `Smoothing,Yes`，因此不能将该 CSV 强度冒充未平滑原始光谱；这里只用它的实际设备标定波长轴。实时 helper 的 counts 不经过上述 Sense 显示处理。实时元数据明确记录 `raw_available=true`、`smoothing=false`、`smoothing_applied=false` 和 DLL 内部状态未知。

最初从旧配置迁移的曝光 70 µs、HDR 模式 1、采集尝试间隔 10 ms、触发请求 100 Hz 是 SDK 设置请求，并非设备寄存器读回。2026-10-09 的现场检查发现 70 µs 出现 65535 计数饱和，随后操作者授权的检查改用历史实测的 20 µs；为控制拟合积压，当前 `config/device.json` 的 helper 尝试间隔为 40 ms（25 Hz 请求），触发寄存器请求仍是 100 Hz。这两个频率分别描述 helper 调用间隔及寄存器请求，均不能替代测得的设备物理新帧率。最终交付设置以配置文件及各会话启动时保存的 SDK 原始 status 为准，不追溯改写旧会话。helper 保留 Sense 2020 V1.6.3.3 的寄存器转换合同。

## PX6D

核对厂家 `PX6D六维力传感器产品资料包/【X01】说明书/六维力传感器说明书.pdf` 的物理第 10～12 页（印刷第 07～09 页，随附渲染核查图）与既有已工作的适配层、历史本机协议核查 JSON。厂商资料确认：波特率 921600、8 位、无校验、1 停止位；AA 55 帧头、设备 ID、命令、24 字节载荷及 CRC8。读取单帧请求为 `AA 55 7F 05 01 FA`，固件请求为 `AA 55 7F 07 01 D0`。数据响应类型为 03，29 字节，六路 little-endian float32，对应 Fx/Fy/Fz（N）及 Mx/My/Mz（N·m）。CRC8 多项式 07、初值 00 已由现有历史实测协议核查支持。

厂商说明书命令举例的固件响应类型与现有工作设备读回存在文字表格差异；当前代码保持已经工作的固件类型 07 合同、13 字节与 CRC 校验。如现场握手失败，应保存实际响应并核对，不能盲目放宽数据帧协议。适配器只发送查询固件及读取当前六轴数据命令，不发送硬件校零、校准或自动回传设置。

串口按 VID 28E9、PID 018A、USB 序列 `D2F56A779C93` 唯一匹配，COM 号变化不会改变设备选择。零个或多个匹配即拒绝连接，不轮流打开未知串口。六路不滤波、不回零，保留原始包十六进制。查询采用 50 Hz 请求；实际接收率独立测量。沿用既有已工作 USB reader 的合同，不假定数据包响应地址必须回显请求地址 7F；实际响应地址原样保留且标记未核实，CRC8 始终强制校验。无效包及计算/接收 CRC、地址、原始采集时间保留在设备诊断中。2026-10-09 新适配器首次现场采集确认数据响应地址实际为 01，CRC 通过，与请求地址 7F 不同。

## 时间、帧身份与同步

两个设备当前协议均没有可验证的硬件帧序号或硬件采集时钟。`frame_id` 是每次连接内本地主机接收序号，`frame_id_source=local_receive_sequence`；`device_sequence` 和 `device_timestamp_raw` 必须为 null。BaySpec 的 helper `frame_index` 是采集尝试计数，不能冒充物理新帧序号；helper 时间戳也是主机调用返回时间。

对齐仅使用 `host_rx_monotonic_ns`，记录接收 UTC、读取起止、`stream_epoch`、`timestamp_source=host_receive_monotonic` 及 `time_quality=host_rx_approx`。光谱的 read_start/read_end 是主机读取 helper 管道区间，不能解释成曝光区间；helper 另保留 snapshot 开始/返回的局部单调计时，不能直接减去 Python 的单调时钟原点。PX6D 记录查询至包接收的往返耗时。接收时间近似不能宣称设备公共时钟硬同步。

同步器只对同一 PX6D epoch 内精确接收时间或有左右相邻帧的有限间隔做有效配对：默认相邻间隔 ≤40 ms、任一端距光谱时间 ≤25 ms。不跨断线重连插值；光谱新 epoch 不使用其首帧前的力记录。最近单帧只供诊断、`valid=false`，不会作为训练标签。拟合完成时间始终不参与匹配。最终诊断分别报告实际接收率、未知的物理新帧率、传输序号重复与数值相同计数；数值相同不算重复帧。

## 独占与受控关闭

连接由用户按钮触发。导入模块、程序启动及创建快捷方式均不打开设备。兼容旧 source 的共享 OS 字节锁：`%LOCALAPPDATA%/TOUCHmFBGShared/device-leases/bayspec.lock` 和 `px6d.lock`。这些锁由操作系统跨进程、跨端口保持，进程退出即释放；不能仅靠旧锁文件是否存在判断占用。连接前还只读核查 8660/8765 的旧程序健康状态，保护未实现共享租约的历史程序；不明确或超时则拒绝设备操作。Windows 进程快照同时检查任何同名 `BaySpecSdkStream.exe`，包括路径未知及父进程已退出的孤儿进程，发现即拒绝新连接，适配器绝不自动结束别的进程。

BaySpec 每次连接创建唯一 Windows 命名 Event，helper 每次采集循环检查，用户停止时由 Python 显式置位并在 helper finally 调用厂商析构。它不依赖 `CREATE_NO_WINDOW` 下不可靠的 Console stdin；stdin EOF 或读取异常不会无声停止采集。3 秒未退出才强制终止，`shutdown_forced=true`。Windows 子进程同时加入 kill-on-close Job，父进程退出时不会留下占用 USB 的 helper。SDK 队列最多 128 帧，加一个触发溢出的原始帧暂存；溢出会停止接收并报告本次采集不完整，不覆盖旧帧；缓冲排空后 read 抛出错误。helper 异常或 EOF 同样可见，最后的 SDK warning 不掩盖进程意外退出原因。设备超过 2 秒不再提供有效帧，watchdog 记录错误并受控关闭；诊断保留最后接收年龄、helper 退出码、停止原因、SDK 原始 status/warning，以及执行文件/DLL/来源清单哈希。关闭后 `drain_received()` 只排空内存中已接受的尾帧，不触发 SDK 或 watchdog，供主程序完整保存尾帧。PX6D 关闭时取消读取并关闭串口，再释放租约。

## 2026-10-09 现场控制结果

初次集成发现新 helper 的 SDK 返回 0/SEH 外部组件异常。只读核查发现旧 source 目录中的 helper PID 21216 仍存活，而父 PID 26344 已不存在；根进程依据确定路径与失去父进程的证据清理了旧孤儿。适配器的 15 项 P/Invoke 声明、8192 字节对象分配/构造/析构及 512 点大端缓冲合同与既有工作 helper 完全一致，未改变 ABI。新代码现会直接保护这种残留占用。

清理后，操作者确认无载荷下进行了单个新独立 helper 的 30 秒光谱控制：主时段连续 750 帧，接收率 24.97～25.00 Hz、队列 0、无 SDK warning/SEH/断流。该次 stdin 停机仍需 3 秒 fallback，停止期间额外 75 帧亦保存，总 825 帧，未冒充正常停机。改用唯一 Windows 命名 Event 后再做 10 秒控制，保存 250 帧，24.983 Hz、队列 0、无错误，helper 正常退出码 0、`shutdown_forced=false`、`last_stop_reason=explicit_stop_event`，结束后无遗留 helper。

原始数据及诊断在 `evidence/device-control-30s/`、`evidence/device-control-stop-event-10s/`；ABI 比较在前者 `abi-comparison.json`，旧孤儿核实证据在 `evidence/legacy-orphan-helper-verified.json`。这些结果支持此前异常与残留并发设备占用有关，但未通过故意重建两个采集进程抢占 USB 来宣称唯一根因。控制数据只用于无载数据/设备稳定性核查，没有同步按压标签，不能认证训练完成。

## 离线证据与验收边界

`C:/光纤触觉/TOUCH_Dev/evidence/measured-optical-first256.jsonl` 为 256 帧真实 SDK counts，原始导出抽取文件哈希为 `2258bb075ad22b4c40b9739493ee0ff659ac1304f508cebf988434e61c6311d5`。对应历史曝光为 20 µs、阶段 `no_load_check`；原记录说明物理无载荷/按压真值未验证，不含可靠同步 PX6D 按压标签，不能代替真实无载荷稳定性及按压算法验收。不能静默混用不同曝光、模式、标定轴及参考版本的采集条件；新现场实测证据由主程序单独保存。

原 TOUCH 注册环境是 `C:/光纤触觉/TOUCH_Build_Cache/20260929/build-env/Scripts/python.exe`，9FBG 不依赖、不复制、不启动该环境。9FBG 使用自己 `.venv`。小型 helper、厂商 DLL、真实标定 CSV/轴及核查来源的哈希在 `assets/sdk/SDK_PROVENANCE.json`；产品不依赖已删除旧软件目录。

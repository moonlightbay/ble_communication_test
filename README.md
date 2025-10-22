# BLE 通信封装（blecore.py）

基于 `bleak` 的蓝牙通信小轮子，适合 Windows 使用（也支持其他平台，接口相同）。

## 功能
- get_local_info(): 返回本机蓝牙适配器信息（若可用）与快速扫描摘要。
- connect(address, try_time=3): 连接指定设备地址，带重试，返回 bool 表示是否成功。
- is_connected(address): 查询连接状态。
- disconnect(address): 断开连接并清理上下文。
- get_characteristics(address): 获取设备所有服务与特征信息。
- read_characteristic(address, char_uuid): 读取特征值（成功返回 bytes，失败返回 None）。
- write_characteristic(address, char_uuid, payload, response=True): 写入特征值，返回 bool。
- start_notify(address, char_uuid, callback): 订阅特征通知，返回 bool；回调在后台线程池/协程中执行。
- stop_notify(address, char_uuid): 取消特征通知订阅，返回 bool。

设计要点：
- 后台 asyncio 事件循环线程，主线程提供同步接口，调用简单。
- 每设备独立读写锁，确保同一设备读写顺序一致；不同设备间可并发，不会互相阻塞。
- 支持通知：回调会在后台 loop 中异步调度，避免阻塞核心 BLE 事件。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

> 说明：Windows 上 `winsdk` 或 `winrt` 为可选，仅用于获取本机适配器地址。若安装失败，不影响核心读写功能。

## 快速使用

```python
from blecore import (
    get_local_info, connect, is_connected, disconnect,
    get_characteristics, read_characteristic, write_characteristic,
)

info = get_local_info()
print(info)

addr = "AA:BB:CC:11:22:33"
if not connect(addr, try_time=3):
    raise RuntimeError("device unreachable")
print("connected?", is_connected(addr))

# 查看特征
chrs = get_characteristics(addr)
for svc in chrs:
    print(svc["service_uuid"])  # and more

# 读
data = read_characteristic(addr, "0000xxxx-0000-1000-8000-00805f9b34fb")
if data is None:
    raise RuntimeError("read failed")
print("read:", data.hex())

# 写
if not write_characteristic(addr, "0000xxxx-0000-1000-8000-00805f9b34fb", "01 02 0A"):
    raise RuntimeError("write failed")

# 断开
disconnect(addr)
```

## 通知订阅示例

```python
import time
from blecore import connect, start_notify, stop_notify

DEVICE = "AA:BB:CC:11:22:33"
CHAR = "0000xxxx-0000-1000-8000-00805f9b34fb"


def on_update(address: str, char_uuid: str, payload: bytes) -> None:
    print(f"[{address} {char_uuid}] => {payload.hex()}")


if connect(DEVICE) and start_notify(DEVICE, CHAR, on_update):
    print("notify started")

try:
  # 模拟业务循环
  while True:
    time.sleep(1)
except KeyboardInterrupt:
  pass
finally:
    stop_notify(DEVICE, CHAR)
```

回调签名为 `callback(address: str, char_uuid: str, payload: bytes)`；若提供 `async def` 回调，同样会被自动调度执行，无需额外事件循环配置。

## 并发与高频读写
- 本实现不会在每次读/写前做昂贵操作，仅在必要时检查连接（快路径）；
- 同一设备的读写通过设备级锁串行，避免多个线程同时对同一设备操作导致丢包或乱序；
- 多设备之间互不影响，可在不同地址上并发调用读写。

示例：同时对两个设备进行读写。

```python
import threading
from blecore import connect, read_characteristic, write_characteristic

addr1 = "AA:BB:CC:11:22:33"
addr2 = "DD:EE:FF:44:55:66"
if not (connect(addr1) and connect(addr2)):
  raise SystemExit("connect failed")

UUID = "0000xxxx-0000-1000-8000-00805f9b34fb"

def worker(addr: str):
    for _ in range(100):
    data = read_characteristic(addr, UUID)
    if data is None:
      continue  # 失败时跳过或自行重试
    # 业务处理...
    write_characteristic(addr, UUID, data)

threads = [threading.Thread(target=worker, args=(addr1,)),
           threading.Thread(target=worker, args=(addr2,))]
[t.start() for t in threads]
[t.join() for t in threads]
```

## 常见问题
- 高频读写是否要每次检查连接？
  - 已做快路径优化：若已连接，直接执行，不额外加锁/查询；仅在未连接时进入连接锁与重试流程。
- 如何保证不同设备读写不丢数据？
  - 每设备独立的读写锁保证顺序性；而不同设备是并行执行，互不干扰。
- payload 支持的数据类型？
  - bytes/bytearray/list[int]/十六进制字符串（如 "01 02 0A" 或 "01020A"）。
- 通知回调在哪里执行？
  - 回调会被调度到后台事件循环中，如为同步函数会自动在线程池执行；可安全进行耗时逻辑但建议尽快返回。

## 许可证
MIT-like（随项目自由使用）。

---

## 并发测试脚本小贴士（稳定性验证结论）

- 在多设备并发场景下，建议按顺序建立连接并订阅（“启动节流”）：
  - 本仓库的 `test_sync.py`、`test_write.py` 已内置顺序放行与错峰时延（常量 `CONNECT_STAGGER_S`，默认 0.6 s）。
  - 实测表明该方法有效解决了开始阶段连接/订阅不稳定的问题，显著提升三设备同时成功的概率。
- 若仍偶发启动失败，可将 `CONNECT_STAGGER_S` 适当增大到 0.8 s，并为 connect/start_notify 加入 2–3 次退避重试（200/400/800 ms）。
- 数据通道选择：
  - 固件通过宏 `STREAM_CHANNELS` 选择 16/32 通道；上位机脚本通过 `EEG_CHANNELS` 与固件保持一致。
  - 32 通道时已自动限制每次通知的帧数以不超过典型 MTU 约 244B。

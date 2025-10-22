from __future__ import annotations

"""
并发场景：
- 设备A EEG：主机订阅 notify，按 test_sync.py 的单设备逻辑接收数据；
- 设备B 刺激器：主机以 10 Hz 向 WRITE 特征写入 8 字节；
- 设备C 外骨骼：主机以 10 Hz 向 WRITE 特征写入 8 字节；

流程：
1) 三设备分别建立连接（各在线程中），EEG 设备完成 start_notify 作为“就绪”；
2) 等待全部会话连接成功（EEG 还需就绪）；
3) 同步发起：EEG 发送 0xAA 开始接收；刺激器与外骨骼各自按 10 Hz 写入；
4) 到达统一测试时长后，EEG 发送 0xFF 停止，三个会话各自清理断开并汇报统计。

注意：写入内容为 8 字节主机时间戳（相对于开始写入时刻的微秒数，little-endian 64bit）。
"""

import threading
import time
from dataclasses import dataclass
from time import sleep
from typing import Iterable, List, Optional, Sequence

from blecore import (
    connect,
    disconnect,
    read_characteristic,
    start_notify,
    stop_notify,
    write_characteristic,
)

from decode import decode_stream_channels


# 固件命令
COMMAND_ON = 0xAA
COMMAND_STOP = 0xFF

# GATT UUID（与现有脚本一致）
SERVICE_UUID = "8653000a-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "8653000c-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
READ_UUID = "8653000d-43e6-47b7-9cb0-5fc21d4ae340"

# 超时与频率
CONNECT_TIMEOUT_S = 30.0
READY_TIMEOUT_S = 15.0
START_TIMEOUT_S = 5.0
WRITE_HZ = 10.0  # 10Hz 写入
CONNECT_STAGGER_S = 0.6  # 启动节流：相邻设备连接/订阅之间的错峰时延（秒）

# EEG 通道数选择（16 或 32）
EEG_CHANNELS = 32

# 写入目标开关：若写双设备不稳定，可将其中一个设为 False 做单设备写入测试
ENABLE_STIM_WRITE = True
ENABLE_EXO_WRITE = True


def _prefixed(name: str, message: str) -> str:
    return f"[{name}] {message}"


@dataclass
class DeviceConfig:
    name: str
    address: str


@dataclass
class EegResult:
    name: str
    total_received: int
    total_sent: int
    lost_frames: List[int]


@dataclass
class WriterResult:
    name: str
    writes_attempted: int
    writes_ok: int
    last_error: str | None
    read_counter: Optional[int]


class EegReceiverSession:
    """EEG 接收会话：连接+订阅+开始/停止+统计帧数与丢帧。
    与 test_sync.py 的单设备逻辑一致，但不计算延迟，仅做帧计数与丢帧检测。
    """

    def __init__(self, config: DeviceConfig, start_event: threading.Event, duration_s: float) -> None:
        self.config = config
        self._start_event = start_event
        self._connect_gate = threading.Event()  # 启动节流：等待允许后再发起连接/订阅
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._ready_event = threading.Event()
        self._started_event = threading.Event()
        self._completed_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._lock = threading.Lock()
        self._time_start_ns = 0

        self._total_received = 0
        self._lost_frames: List[int] = []
        self._last_sequence = 0
        self._duration_s = duration_s

        self._notify_active = False
        self._connected = False
        self.result: EegResult | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name=f"EEGSession-{config.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def allow_connect(self) -> None:
        """允许本会话开始连接/订阅（用于按顺序启动）。"""
        self._connect_gate.set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected_event.wait(timeout)

    def wait_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    def wait_started(self, timeout: float) -> bool:
        return self._started_event.wait(timeout)

    def join(self) -> None:
        self._completed_event.wait()
        self._thread.join()

    # 回调仅记录帧序号，用于丢帧统计
    def _notify_callback(self, address: str, char_uuid: str, data: bytes) -> None:
        frames = decode_stream_channels(data, EEG_CHANNELS)
        with self._lock:
            for frame in frames:
                self._total_received += 1
                expected = self._last_sequence + 1
                if frame.sequence > expected:
                    self._lost_frames.extend(range(expected, frame.sequence))
                self._last_sequence = frame.sequence
        self._first_frame_event.set()

    def _wait_for_first_frame(self, timeout_s: float = 5.0) -> None:
        if not self._first_frame_event.wait(timeout=timeout_s):
            raise TimeoutError(_prefixed(self.config.name, "No notification received within timeout."))

    def _run(self) -> None:
        address = self.config.address
        try:
            # 等待外部允许，避免与其它设备并发连接/订阅
            self._connect_gate.wait()
            print(_prefixed(self.config.name, "Connecting..."))
            connect(address, 3, 20)
            self._connected = True
            self._connected_event.set()

            start_notify(address, NOTIFY_UUID, self._notify_callback)
            self._notify_active = True
            self._ready_event.set()

            # 等待全局开始信号
            self._start_event.wait()
            if self._stop_event.is_set():
                self._started_event.set()
                return

            # EEG 发送开始
            self._time_start_ns = time.perf_counter_ns()
            write_characteristic(address, WRITE_UUID, bytes([COMMAND_ON]))
            print(_prefixed(self.config.name, "Sent command to turn on."))
            self._started_event.set()

            self._wait_for_first_frame()
            self._stop_event.wait(timeout=self._duration_s)

            # EEG 发送停止
            write_characteristic(address, WRITE_UUID, bytes([COMMAND_STOP]))
            print(_prefixed(self.config.name, "Sent command to stop."))
            sleep(0.5)

            sent_bytes = read_characteristic(address, READ_UUID)
            total_sent = int.from_bytes(sent_bytes or b"\x00\x00\x00\x00", "little")

            with self._lock:
                total_received = self._total_received
                lost_copy = list(self._lost_frames)

            self.result = EegResult(
                name=self.config.name,
                total_received=total_received,
                total_sent=total_sent,
                lost_frames=lost_copy,
            )
        except BaseException as exc:
            self.error = exc
            print(_prefixed(self.config.name, f"Test failed: {exc}"))
        finally:
            try:
                if self._notify_active:
                    stop_notify(address, NOTIFY_UUID)
            except Exception:
                pass
            if self._connected:
                disconnect(address)
            print(_prefixed(self.config.name, "Disconnected from device."))
            self._completed_event.set()


class WriterSession:
    """通用写入会话：连接后等待开始信号，随后以 10 Hz 循环向 WRITE 特征写入 8 字节。"""

    def __init__(self, config: DeviceConfig, start_event: threading.Event, duration_s: float) -> None:
        self.config = config
        self._start_event = start_event
        self._connect_gate = threading.Event()  # 启动节流：等待允许后再发起连接
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._ready_event = threading.Event()
        self._completed_event = threading.Event()
        self._duration_s = duration_s

        self._connected = False
        self.writes_attempted = 0
        self.writes_ok = 0
        self.last_error = None
        self.read_counter = None
        self._start_ref_ns = None

        self._thread = threading.Thread(target=self._run, name=f"WriterSession-{config.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def allow_connect(self) -> None:
        self._connect_gate.set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected_event.wait(timeout)

    def wait_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    def join(self) -> None:
        self._completed_event.wait()
        self._thread.join()

    # 以将8字节改为发送16字节
    def _payload(self) -> bytes:
        if self._start_ref_ns is None:
            self._start_ref_ns = time.perf_counter_ns()
            elapsed_us = 0
        else:
            elapsed_us = int((time.perf_counter_ns() - self._start_ref_ns) / 1000)
        return int(elapsed_us).to_bytes(16, "little")

    def _run(self) -> None:
        address = self.config.address
        try:
            self._connect_gate.wait()
            print(_prefixed(self.config.name, "Connecting..."))
            connect(address, 3, 20)
            self._connected = True
            self._connected_event.set()

            # 写入设备不需要 notify，连接即视为就绪
            self._ready_event.set()

            # 等待统一开始
            self._start_event.wait()
            if self._stop_event.is_set():
                return

            # 记录写入基准时间
            self._start_ref_ns = time.perf_counter_ns()

            # 10Hz 定时写入
            period = 1.0 / WRITE_HZ
            t0 = time.perf_counter()
            n = 0
            while (time.perf_counter() - t0) < self._duration_s and not self._stop_event.is_set():
                try:
                    self.writes_attempted += 1
                    write_characteristic(address, WRITE_UUID, self._payload(), response=False)
                    self.writes_ok += 1
                except Exception as exc:
                    self.last_error = str(exc)
                # 简单的相位校正睡眠
                n += 1
                next_t = t0 + n * period
                now = time.perf_counter()
                sleep_dur = max(0.0, next_t - now)
                if sleep_dur > 0:
                    time.sleep(sleep_dur)
            # 读取对端累计计数（READ 特征 2 字节）
            read_bytes = read_characteristic(address, READ_UUID)
            if read_bytes:
                self.read_counter = int.from_bytes(read_bytes[:2], "little")
        except BaseException as exc:
            self.last_error = str(exc)
            print(_prefixed(self.config.name, f"Writer failed: {exc}"))
        finally:
            if self._connected:
                disconnect(address)
            print(_prefixed(self.config.name, "Disconnected from device."))
            self._completed_event.set()


def run_write_test(duration_s: float = 10.0) -> tuple[EegResult | None, List[WriterResult]]:
    # 设备地址：EEG: ...:01, 刺激器: ...:04, 外骨骼: ...:05
    eeg = DeviceConfig(name="bio-eeg", address="EE:EE:EE:EE:EE:01")
    stim = DeviceConfig(name="stim", address="EE:EE:EE:EE:EE:04")
    exo = DeviceConfig(name="exo", address="EE:EE:EE:EE:EE:05")

    start_event = threading.Event()

    eeg_session = EegReceiverSession(eeg, start_event, duration_s)
    writer_sessions: List[WriterSession] = []
    if ENABLE_STIM_WRITE:
        writer_sessions.append(WriterSession(stim, start_event, duration_s))
    if ENABLE_EXO_WRITE:
        writer_sessions.append(WriterSession(exo, start_event, duration_s))

    # 启动线程
    eeg_session.start()
    for ws in writer_sessions:
        ws.start()

    try:
        # 按顺序节流建立连接（并在 EEG 上完成订阅）
        eeg_session.allow_connect()
        if not eeg_session.wait_connected(CONNECT_TIMEOUT_S):
            raise TimeoutError(_prefixed(eeg.name, "EEG did not connect in time."))
        time.sleep(CONNECT_STAGGER_S)

        for ws in writer_sessions:
            ws.allow_connect()
            if not ws.wait_connected(CONNECT_TIMEOUT_S):
                raise TimeoutError(_prefixed(ws.config.name, "Writer did not connect in time."))
            time.sleep(CONNECT_STAGGER_S)

        # EEG 订阅就绪
        if not eeg_session.wait_ready(READY_TIMEOUT_S):
            raise TimeoutError(_prefixed(eeg.name, "EEG notify setup timed out."))

        # 写入线程无需订阅，已就绪
        for ws in writer_sessions:
            if not ws.wait_ready(READY_TIMEOUT_S):
                raise TimeoutError(_prefixed(ws.config.name, "Writer not ready."))

        print("All devices ready. Broadcasting start signal.")
        start_event.set()

        # 等 EEG 确认开始（安全起见）
        if not eeg_session.wait_started(START_TIMEOUT_S):
            raise TimeoutError(_prefixed(eeg.name, "EEG did not acknowledge start."))

        # 等待全部结束
        eeg_session.join()
        for ws in writer_sessions:
            ws.join()

        eeg_result = eeg_session.result
        writer_results = [
            WriterResult(
                name=ws.config.name,
                writes_attempted=ws.writes_attempted,
                writes_ok=ws.writes_ok,
                last_error=ws.last_error,
                read_counter=ws.read_counter,
            )
            for ws in writer_sessions
        ]
        return eeg_result, writer_results
    finally:
        # 兜底：若有线程未退出，发出停止并尽量回收
        start_event.set()


def main() -> None:
    duration_s = 10.0
    eeg_result, writer_results = run_write_test(duration_s=duration_s)

    if eeg_result is not None:
        print(
            _prefixed(
                eeg_result.name,
                f"received={eeg_result.total_received}, sent={eeg_result.total_sent}, lost={len(eeg_result.lost_frames)}",
            )
        )
    for wr in writer_results:
        print(
            _prefixed(
                wr.name,
                f"writes_ok/attempted={wr.writes_ok}/{wr.writes_attempted}, read_counter={wr.read_counter}, last_error={wr.last_error}",
            )
        )


if __name__ == "__main__":
    main()

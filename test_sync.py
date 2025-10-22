from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from time import sleep
from typing import Iterable, List, Sequence

from blecore import (
    connect,
    disconnect,
    read_characteristic,
    start_notify,
    stop_notify,
    write_characteristic,
)

from decode import decode_stream_channels


COMMAND_ON = 0xAA
COMMAND_STOP = 0xFF

SERVICE_UUID = "8653000a-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "8653000c-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
READ_UUID = "8653000d-43e6-47b7-9cb0-5fc21d4ae340"
CONNECT_TIMEOUT_S = 30.0
READY_TIMEOUT_S = 15.0
START_TIMEOUT_S = 5.0
CONNECT_STAGGER_S = 0.6  # 启动节流：相邻设备连接/订阅之间的错峰时延（秒）

# EEG 通道数选择（16 或 32）
EEG_CHANNELS = 16


def _prefixed(name: str, message: str) -> str:
    return f"[{name}] {message}"


@dataclass
class DeviceConfig:
    name: str
    address: str
    duration_s: float = 20.0


@dataclass
class DeviceTestResult:
    name: str
    total_received: int
    total_sent: int
    average_delay_us: float
    lost_frames: List[int]
    delay_samples: List[tuple[int, float]]


class SyncDeviceSession:
    def __init__(self, config: DeviceConfig, start_event: threading.Event) -> None:
        self.config = config
        self._start_event = start_event
        self._connect_gate = threading.Event()  # 启动节流：等待允许后再发起连接/订阅
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._started_event = threading.Event()
        self._completed_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._connected_event = threading.Event()
        self._lock = threading.Lock()

        self._time_start_ns = 0
        self._delay_list = []
        self._lost_frames = []
        self._last_sequence = 0
        self._min_delta_us = None

        self.result = None
        self.error = None
        self._notify_active = False
        self._connected = False

        self._thread = threading.Thread(target=self._run, name=f"SyncSession-{config.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def allow_connect(self) -> None:
        """允许本会话开始连接/订阅（用于按顺序启动）。"""
        self._connect_gate.set()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    def wait_started(self, timeout: float) -> bool:
        return self._started_event.wait(timeout)

    def wait_connected(self, timeout: float) -> bool:
        return self._connected_event.wait(timeout)

    def request_stop(self) -> None:
        self._stop_event.set()

    def join(self) -> None:
        self._completed_event.wait()
        self._thread.join()

    def _notify_callback(self, address: str, char_uuid: str, data: bytes) -> None:
        arrival_time_us = (time.perf_counter_ns() - self._time_start_ns) / 1000
        frames = decode_stream_channels(data, EEG_CHANNELS)

        with self._lock:
            for frame in frames:
                raw_delta_us = arrival_time_us - frame.timestamp_us
                if (self._min_delta_us is None) or (raw_delta_us < self._min_delta_us):
                    self._min_delta_us = raw_delta_us

                # Track latency relative to the lowest transport time observed so far.
                delay_us = raw_delta_us - self._min_delta_us
                # print(_prefixed(self.config.name, f"frame{frame.sequence}, transmission delay: {delay_us}us"))
                self._delay_list.append((frame.sequence, delay_us))
                expected = self._last_sequence + 1
                if frame.sequence > expected:
                    self._lost_frames.extend(range(expected, frame.sequence))
                self._last_sequence = frame.sequence
        self._first_frame_event.set()

    def _wait_for_first_frame(self, timeout_s: float = 5.0) -> None:
        if not self._first_frame_event.wait(timeout=timeout_s):
            raise TimeoutError(_prefixed(self.config.name, "No notification received within timeout."))

    def _save_csv(self, filename: str, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(",".join(headers) + "\n")
            for row in rows:
                f.write(",".join(str(item) for item in row) + "\n")

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

            self._start_event.wait()
            if self._stop_event.is_set():
                self._started_event.set()
                return

            self._time_start_ns = time.perf_counter_ns()
            write_characteristic(address, WRITE_UUID, bytes([COMMAND_ON]))
            print(_prefixed(self.config.name, "Sent command to turn on."))
            self._started_event.set()

            self._wait_for_first_frame()

            # Wait for either an explicit stop request or the configured duration.
            self._stop_event.wait(timeout=self.config.duration_s)

            write_characteristic(address, WRITE_UUID, bytes([COMMAND_STOP]))
            stop_time_ns = time.perf_counter_ns()
            print(_prefixed(self.config.name, f"Total time: {(stop_time_ns - self._time_start_ns) / 1000}us"))
            print(_prefixed(self.config.name, "Sent command to stop."))

            sleep(1.0)

            with self._lock:
                total_received = len(self._delay_list)
                delays_copy = list(self._delay_list)
                lost_copy = list(self._lost_frames)

            print(_prefixed(self.config.name, f"Total received frames: {total_received}"))

            sent_bytes = read_characteristic(address, READ_UUID)
            if sent_bytes is None:
                raise RuntimeError(_prefixed(self.config.name, "Failed to read total frame count from device."))
            total_sent = int.from_bytes(sent_bytes, byteorder="little")
            print(_prefixed(self.config.name, f"Total sent frames according to device: {total_sent}"))

            average_delay = (
                sum(delay for _, delay in delays_copy) / total_received if total_received else 0.0
            )
            print(_prefixed(self.config.name, f"Average transmission delay: {average_delay}us"))

            # delay_filename = f"{self.config.name}_transmission_delay.csv"
            # self._save_csv(
            #     delay_filename,
            #     ("Frame Number", "Delay (us)"),
            #     delays_copy,
            # )
            # print(_prefixed(self.config.name, f"Saved transmission delays to {delay_filename}"))

            # lost_filename = f"{self.config.name}_lost_frames.csv"
            # self._save_csv(
            #     lost_filename,
            #     ("Lost Frame Numbers",),
            #     ((frame,) for frame in lost_copy),
            # )

            self.result = DeviceTestResult(
                name=self.config.name,
                total_received=total_received,
                total_sent=total_sent,
                average_delay_us=average_delay,
                lost_frames=lost_copy,
                delay_samples=delays_copy,
            )
        except BaseException as exc:
            self.error = exc
            self._ready_event.set()
            self._started_event.set()
            print(_prefixed(self.config.name, f"Test failed: {exc}"))
            self._connected_event.set()
        finally:
            try:
                if self._notify_active:
                    stop_notify(address, NOTIFY_UUID)
            except Exception:
                pass
            sleep(1)
            if self._connected:
                disconnect(address)
            print(_prefixed(self.config.name, "Disconnected from device."))
            self._completed_event.set()


def run_sync_test(devices: Sequence[DeviceConfig]) -> List[DeviceTestResult]:
    if not devices:
        return []

    start_event = threading.Event()
    sessions = [SyncDeviceSession(config, start_event) for config in devices]
    results: List[DeviceTestResult] = []

    for session in sessions:
        session.start()
    try:
        # 按顺序节流建立连接与订阅：逐个允许并等待完成
        for i, session in enumerate(sessions):
            session.allow_connect()
            if not session.wait_connected(timeout=CONNECT_TIMEOUT_S):
                raise TimeoutError(_prefixed(session.config.name, "Device did not connect in time."))
            if session.error is not None:
                raise RuntimeError(_prefixed(session.config.name, f"Setup failed: {session.error}"))
            if i < len(sessions) - 1:
                time.sleep(CONNECT_STAGGER_S)

        for session in sessions:
            if not session.wait_ready(timeout=READY_TIMEOUT_S):
                raise TimeoutError(_prefixed(session.config.name, "Notification setup timed out."))
            if session.error is not None:
                raise RuntimeError(_prefixed(session.config.name, f"Setup failed: {session.error}"))

        print("All devices ready. Broadcasting start signal.")
        start_event.set()

        for session in sessions:
            if not session.wait_started(timeout=START_TIMEOUT_S):
                if session.error is not None:
                    raise RuntimeError(_prefixed(session.config.name, f"Start failed: {session.error}"))
                raise TimeoutError(_prefixed(session.config.name, "Device did not acknowledge start."))

        collected: List[DeviceTestResult] = []
        for session in sessions:
            session.join()
            if session.error is not None:
                raise RuntimeError(_prefixed(session.config.name, f"Run failed: {session.error}"))
            if session.result is not None:
                collected.append(session.result)
        results = collected
    finally:
        for session in sessions:
            if not session._completed_event.is_set():
                session.request_stop()
        start_event.set()
        for session in sessions:
            try:
                session.join()
            except Exception:
                pass

    return results


def main() -> None:
    devices = [
        # DeviceConfig(name="bio-eeg", address="EE:EE:EE:EE:EE:01", duration_s=10.0),
        DeviceConfig(name="bio-emg", address="EE:EE:EE:EE:EE:02", duration_s=10.0),
        # DeviceConfig(name="bio-scs", address="EE:EE:EE:EE:EE:03", duration_s=10.0)
    ]

    results = run_sync_test(devices)
    for result in results:
        print(
            _prefixed(
                result.name,
                f"received={result.total_received}, sent={result.total_sent}, avg_delay_us={result.average_delay_us}",
            )
        )


if __name__ == "__main__":
    main()

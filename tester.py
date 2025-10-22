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

from decode import decode_stream


COMMAND_ON = 0xAA
COMMAND_STOP = 0xFF

SERVICE_UUID = "8653000a-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "8653000c-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
READ_UUID = "8653000d-43e6-47b7-9cb0-5fc21d4ae340"


@dataclass
class DeviceConfig:
    name: str
    address: str
    duration_s: float = 20.0


@dataclass
class DeviceTestResult:
    total_received: int
    total_sent: int
    average_delay_us: float
    lost_frames: List[int]
    delay_samples: List[tuple[int, float]]


class BleDeviceTester:
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config
        self._time_start_ns = 0
        self._delay_list: List[tuple[int, float]] = []
        self._lost_frames: List[int] = []
        self._last_sequence = 0
        self._first_frame_event = threading.Event()
        self._min_delta_us = None

    def _prefixed(self, message: str) -> str:
        return f"[{self.config.name}] {message}"

    def _notify_callback(self, address: str, char_uuid: str, data: bytes) -> None:
        arrival_time_us = (time.perf_counter_ns() - self._time_start_ns) / 1000
        frames = decode_stream(data)
        for frame in frames:
            raw_delta_us = arrival_time_us - frame.timestamp_us
            if (self._min_delta_us is None) or (raw_delta_us < self._min_delta_us):
                self._min_delta_us = raw_delta_us

            # Subtract the running minimum delta so delays stay relative to the
            # smallest observed transport time while allowing for clock drift.
            delay_us = raw_delta_us - self._min_delta_us
            print(self._prefixed(f"frame{frame.sequence}, transmission delay: {delay_us}us"))
            self._delay_list.append((frame.sequence, delay_us))
            expected = self._last_sequence + 1
            if frame.sequence > expected:
                self._lost_frames.extend(range(expected, frame.sequence))
            self._last_sequence = frame.sequence
        self._first_frame_event.set()

    def _wait_for_first_frame(self, timeout_s: float = 5.0) -> None:
        if not self._first_frame_event.wait(timeout=timeout_s):
            raise TimeoutError(self._prefixed("No notification received within timeout."))

    def _save_csv(self, filename: str, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(",".join(headers) + "\n")
            for row in rows:
                f.write(",".join(str(item) for item in row) + "\n")

    def run(self) -> DeviceTestResult:
        address = self.config.address
        print(self._prefixed("Connecting..."))
        connect(address, 3, 20)
        try:
            start_notify(address, NOTIFY_UUID, self._notify_callback)
            self._time_start_ns = time.perf_counter_ns()
            write_characteristic(address, WRITE_UUID, bytes([COMMAND_ON]))
            print(self._prefixed("Sent command to turn on."))

            self._wait_for_first_frame()
            sleep(self.config.duration_s)

            write_characteristic(address, WRITE_UUID, bytes([COMMAND_STOP]))
            stop_time_ns = time.perf_counter_ns()
            print(self._prefixed(f"Total time: {(stop_time_ns - self._time_start_ns) / 1000}us"))
            print(self._prefixed("Sent command to stop."))

            sleep(1.0)  # allow final notifications to arrive

            total_received = len(self._delay_list)
            print(self._prefixed(f"Total received frames: {total_received}"))

            sent_bytes = read_characteristic(address, READ_UUID)
            if sent_bytes is None:
                raise RuntimeError(self._prefixed("Failed to read total frame count from device."))
            total_sent = int.from_bytes(sent_bytes, byteorder="little")
            print(self._prefixed(f"Total sent frames according to device: {total_sent}"))

            average_delay = (
                sum(delay for _, delay in self._delay_list) / total_received
                if total_received
                else 0.0
            )
            print(self._prefixed(f"Average transmission delay: {average_delay}us"))

            delay_filename = f"{self.config.name}_transmission_delay.csv"
            self._save_csv(
                delay_filename,
                ("Frame Number", "Delay (us)"),
                self._delay_list,
            )
            print(self._prefixed(f"Saved transmission delays to {delay_filename}"))

            lost_filename = f"{self.config.name}_lost_frames.csv"
            self._save_csv(
                lost_filename,
                ("Lost Frame Numbers",),
                ((frame,) for frame in self._lost_frames),
            )

            return DeviceTestResult(
                total_received=total_received,
                total_sent=total_sent,
                average_delay_us=average_delay,
                lost_frames=list(self._lost_frames),
                delay_samples=list(self._delay_list),
            )
        finally:
            try:
                stop_notify(address, NOTIFY_UUID)
            except Exception:
                pass
            disconnect(address)
            print(self._prefixed("Disconnected from device."))


def run_tests(devices: Sequence[DeviceConfig]) -> List[DeviceTestResult]:
    results: List[DeviceTestResult] = []
    for device in devices:
        tester = BleDeviceTester(device)
        try:
            result = tester.run()
            results.append(result)
        except Exception as exc:  # pragma: no cover - diagnostic pathway
            print(f"[{device.name}] Test failed: {exc}")
    return results


def main() -> None:
    devices = [
        DeviceConfig(name="bio-eeg", address="EE:EE:EE:EE:EE:01", duration_s=10.0),   #脑电
        # DeviceConfig(name="bio-emg", address="EE:EE:EE:EE:EE:02", duration_s=10.0),   #肌电
        # DeviceConfig(name="bio-scs", address="EE:EE:EE:EE:EE:03", duration_s=10.0),   #脊髓信号
        # 如需测试更多设备，请在此追加 DeviceConfig。
    ]

    run_tests(devices)


if __name__ == "__main__":
    main()
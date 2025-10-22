"""
数据帧格式（单个逻辑帧）：
    [t0..t3]  = timestamp_us (uint32 LE)  # 时间戳精确到微秒
    [s0..s3]  = frame_sequence (uint32 LE)
    [后续 32B] = 16 × int16 LE 通道采样

总字节数：8 + 16*2 = 40 字节。

设备每次 Notification 会连续打包 5 帧（若流停止时剩余不足 5 帧，则为 1~4 帧）。

说明：返回的 Frame 包含字段 `timestamp_us`（微秒），同时提供兼容属性 `timestamp` 返回相同值，便于旧代码依赖 `timestamp` 名称。
"""

from __future__ import annotations
from dataclasses import dataclass
from struct import unpack_from

HEADER_SIZE = 8
CHANNEL_COUNT = 16  # 默认按 16 通道解析；可用下方带通道数的方法解析 32 通道
PAYLOAD_SIZE = CHANNEL_COUNT * 2
FRAME_SIZE = HEADER_SIZE + PAYLOAD_SIZE


@dataclass(slots=True)
class Frame:
    timestamp_us: int
    sequence: int
    samples: list[int]

    @property
    def timestamp(self) -> int:
        """向后兼容属性，返回微秒时间戳。"""
        return self.timestamp_us


def decode_frame(frame: bytes) -> Frame:
    """解码单帧数据，返回时间戳、序号与 16 通道采样。"""
    if len(frame) != FRAME_SIZE:
        raise ValueError(f"帧长度应为 {FRAME_SIZE} 字节，实际为 {len(frame)} 字节")

    timestamp_us = unpack_from("<I", frame, offset=0)[0]
    sequence = unpack_from("<I", frame, offset=4)[0]

    samples: list[int] = []
    for i in range(CHANNEL_COUNT):
        sample = unpack_from("<h", frame, offset=HEADER_SIZE + i * 2)[0]
        samples.append(sample)

    return Frame(timestamp_us=timestamp_us, sequence=sequence, samples=samples)


def decode_stream(data: bytes) -> list[Frame]:
    """解码连续数据流，返回帧对象列表。"""
    if len(data) % FRAME_SIZE != 0:
        raise ValueError("数据长度非帧大小整数倍")

    frames: list[Frame] = []
    for offset in range(0, len(data), FRAME_SIZE):
        frames.append(decode_frame(data[offset : offset + FRAME_SIZE]))
    return frames


# -------------------- 可选：按指定通道数解码（支持 16 或 32） --------------------
def decode_frame_channels(frame: bytes, channels: int) -> Frame:
    """按指定通道数解码单帧数据（channels=16 或 32）。"""
    expected = HEADER_SIZE + channels * 2
    if len(frame) != expected:
        raise ValueError(f"帧长度应为 {expected} 字节，实际为 {len(frame)} 字节")

    timestamp_us = unpack_from("<I", frame, offset=0)[0]
    sequence = unpack_from("<I", frame, offset=4)[0]

    samples: list[int] = []
    for i in range(channels):
        sample = unpack_from("<h", frame, offset=HEADER_SIZE + i * 2)[0]
        samples.append(sample)

    return Frame(timestamp_us=timestamp_us, sequence=sequence, samples=samples)


def decode_stream_channels(data: bytes, channels: int) -> list[Frame]:
    """按指定通道数解码连续数据流。"""
    frame_size = HEADER_SIZE + channels * 2
    if len(data) % frame_size != 0:
        raise ValueError("数据长度非帧大小整数倍")

    frames: list[Frame] = []
    for offset in range(0, len(data), frame_size):
        frames.append(decode_frame_channels(data[offset : offset + frame_size], channels))
    return frames
from __future__ import annotations
from blecore import (
    get_local_info, connect, is_connected, disconnect, 
    get_characteristics, read_characteristic, write_characteristic, 
    start_notify, stop_notify
)
import time
from time import sleep
from typing import Iterable
from decode import decode_stream

COMMAND_ON = 0xAA
COMMAND_STOP = 0XFF
device_address = "EE:EE:EE:EE:EE:01"
time_start = 0.0
time_stop = 0.0
delay_list = []  # (sequence, delay_us)
lost_list = []
index = 0
first_received = False
def notify_callback(address: str, char_uuid: str, data: bytes):
    global index, first_received
    arrival_time_us = (time.perf_counter_ns() - time_start) / 1000  # 单位：微秒
    frames = decode_stream(data)
    for frame in frames:
        delay_us = arrival_time_us - frame.timestamp_us
        print(f"frame{frame.sequence},transmission delay:{delay_us}us")
        delay_list.append((frame.sequence, delay_us))
        expected = index + 1
        if frame.sequence > expected:
            lost_list.extend(range(expected, frame.sequence))
        index = frame.sequence
    first_received = True
"""
写入 COMMAND_ON 启动数据传输，设备开始发送数据帧。
写入 COMMAND_STOP 停止数据传输，设备停止发送数据帧。
数据帧通过通知（Notification）方式发送，需订阅相应特征值以接收数据。
停止后，可读取特征值获取已硬件发送数据帧数量。
bio-eeg address:EE:EE:EE:EE:EE:01
SERVICE:8653000a-43e6-47b7-9cb0-5fc21d4ae340
WRITE:8653000c-43e6-47b7-9cb0-5fc21d4ae340
NOTIFY:8653000b-43e6-47b7-9cb0-5fc21d4ae340
READ:8653000d-43e6-47b7-9cb0-5fc21d4ae340
"""

def main():
    connect(device_address, 3, 20)
    start_notify(device_address, "8653000b-43e6-47b7-9cb0-5fc21d4ae340", notify_callback)
    #开始时间，单位到微秒级
    global time_start
    time_start = time.perf_counter_ns()
    write_characteristic(device_address, "8653000c-43e6-47b7-9cb0-5fc21d4ae340", bytes([COMMAND_ON]))
    print("Sent command to turn on.")
    # if control+C is pressed, stop notification and disconnect
    while not first_received:
        sleep(1)
    sleep(10)
    write_characteristic(device_address, "8653000c-43e6-47b7-9cb0-5fc21d4ae340", bytes([COMMAND_STOP]))
    time_stop = time.perf_counter_ns()
    print(f"Total time: {(time_stop - time_start) / 1000}us")
    print("Sent command to stop.")
    total_recieved_frames = len(delay_list)
    print(f"Total received frames: {total_recieved_frames}")
    eeg_sended_frames_total = read_characteristic(device_address, "8653000d-43e6-47b7-9cb0-5fc21d4ae340")
    if eeg_sended_frames_total is None:
        raise RuntimeError("Failed to read total frame count from device")
    eeg_sended_frames_total = int.from_bytes(eeg_sended_frames_total, byteorder='little')
    print(f"Total sent frames according to device: {eeg_sended_frames_total}")

    average_delay = (sum(delay for _, delay in delay_list) / len(delay_list)) if delay_list else 0
    print(f"Average transmission delay: {average_delay}us")


    # #save delay_list to a csv file, including frame number and delay time
    # with open("transmission_delay.csv", "w") as f:
    #     f.write("Frame Number,Delay (us)\n")
    #     for sequence, delay in delay_list:
    #         f.write(f"{sequence},{delay}\n")
    # print("Saved transmission delays to transmission_delay.csv")

    # #save lost_list to a csv file
    # with open("lost_frames.csv", "w") as f:
    #     f.write("Lost Frame Numbers\n")
    #     for lost_frame in lost_list:
    #         f.write(f"{lost_frame}\n")

    # try:
    #     while True:
    #         sleep(1)
    # except KeyboardInterrupt:
    stop_notify(device_address, "8653000b-43e6-47b7-9cb0-5fc21d4ae340")
    disconnect(device_address)
    print("Disconnected from device.")

if __name__ == "__main__":
    main()
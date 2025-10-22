"""
BLE 通信封装（基于 bleak），支持多设备并发、顺序读写、通知订阅与重试。

功能：
1. get_local_info()            打印/返回本机蓝牙适配器信息
2. connect(address, try_time)  扫描并连接指定地址的设备（带重试），返回是否成功
3. is_connected(address)       查询连接状态
4. disconnect(address)         断开连接
5. get_characteristics(address)查询设备特征信息
6. read_characteristic(address, char_uuid) -> Optional[bytes] 读取特征（失败返回 None）
7. write_characteristic(address, char_uuid, payload) -> bool  写入特征（成功 True）
8. start_notify(address, char_uuid, callback) -> bool        开启特征通知
9. stop_notify(address, char_uuid) -> bool                   关闭特征通知

设计说明（简要）：
- 使用一个后台 asyncio 事件循环线程统一调度，主线程提供同步接口，适合在普通脚本/线程中直接调用。
- 每个设备维护独立的连接对象与读写锁，确保同一设备的操作顺序化，不同设备之间可并发执行，避免数据乱序或丢失。
- 读/写前不强制调用额外的连接状态检查，仅在必要时检查/重连，以减少高频操作的开销；异常内部捕获并返回状态，避免打断上层业务。
- 通知回调自动调度为协程/线程池任务，确保 BLE 循环线程保持轻量。

依赖：
- bleak>=0.22.0
- Windows 上本地适配器信息可通过可选的 winsdk/winrt 获取；若不可用则优雅降级。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Dict, Iterable, List, Optional, Tuple, Union, cast

import bleak
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError


NotifyCallback = Callable[[str, str, bytes], Union[None, Awaitable[None]]]
_NotifyEntry = Tuple[NotifyCallback, Callable[[Any, bytearray], None]]


logger = logging.getLogger(__name__)

# 可选导入：用于在 Windows 上读取本机适配器地址
_win_adapter_available = False
_win_adapter_mod = None
try:  # 首选 winsdk（较新）
	import winsdk.windows.devices.bluetooth as _win_bt  # type: ignore

	_win_adapter_available = True
	_win_adapter_mod = _win_bt
except Exception:
	try:
		# 兼容早期: winrt 包（有些环境 bleak 依赖会带上）
		import winrt.windows.devices.bluetooth as _win_bt  # type: ignore

		_win_adapter_available = True
		_win_adapter_mod = _win_bt
	except Exception:
		_win_adapter_available = False
		_win_adapter_mod = None


def _format_bt_addr64(addr64: int) -> str:
	"""将 Windows BluetoothAdapter.bluetooth_address (u64) 转换为常见的 MAC 格式。"""
	# Windows 返回的是 48bit 地址放在低位的 64bit 数字，需要反序并格式化
	# 参考：https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothadapter.bluetoothaddress
	b = addr64.to_bytes(8, byteorder="little")  # little-endian
	mac = b[:6]  # 低 6 个字节
	return ":".join(f"{x:02X}" for x in mac[::-1])


@dataclass
class _ClientCtx:
	client: BleakClient
	rw_lock: asyncio.Lock  # 序列化同一设备的读/写，避免数据乱序
	connect_lock: asyncio.Lock  # 避免并发连接/重连
	notify_handlers: Dict[str, _NotifyEntry]


class BLEManager:
	"""BLE 管理器：后台事件循环 + 多设备并发安全封装。"""

	def __init__(self, loop_name: str = "BLELoop") -> None:
		self._loop = asyncio.new_event_loop()
		self._thread = threading.Thread(
			target=self._run_loop, name=loop_name, daemon=True
		)
		self._clients: Dict[str, _ClientCtx] = {}
		self._started = False

	def _normalize_address(self, address: str) -> str:
		normalized = address.strip()
		if ":" in normalized:
			normalized = normalized.upper()
		return normalized

	@staticmethod
	def _normalize_char_uuid(char_uuid: str) -> str:
		return char_uuid.strip().lower()

	# -------------------- 基础设施 --------------------
	def start(self) -> None:
		if not self._started:
			self._thread.start()
			self._started = True

	def shutdown(self) -> None:
		if not self._started:
			return
		fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
		fut.result(timeout=10)
		self._loop.call_soon_threadsafe(self._loop.stop)
		self._thread.join(timeout=5)
		self._started = False

	def _run_loop(self) -> None:
		asyncio.set_event_loop(self._loop)
		self._loop.run_forever()

	async def _shutdown_async(self) -> None:
		# 断开所有连接
		for addr in list(self._clients.keys()):
			try:
				ctx = self._clients.get(addr)
				if not ctx:
					continue
				for char_uuid in list(ctx.notify_handlers.keys()):
					try:
						await ctx.client.stop_notify(char_uuid)
					except Exception:
						pass
				ctx.notify_handlers.clear()
				if ctx.client.is_connected:
					await ctx.client.disconnect()
			except Exception:
				pass
		self._clients.clear()

	def _submit(self, coro: Coroutine[Any, Any, Any]):
		if not self._started:
			self.start()
		return asyncio.run_coroutine_threadsafe(coro, self._loop)

	def _make_notify_wrapper(
		self, address_norm: str, char_uuid_display: str, callback: NotifyCallback
	) -> Callable[[Any, bytearray], None]:
		async def _dispatch(payload: bytes) -> None:
			try:
				result = callback(address_norm, char_uuid_display, payload)
				if inspect.isawaitable(result):
					await cast(Awaitable[Any], result)
			except Exception:
				logger.exception(
					"Notification handler failed for %s %s", address_norm, char_uuid_display
				)

		def _inner(_: Any, data: bytearray) -> None:
			loop = asyncio.get_running_loop()
			loop.create_task(_dispatch(bytes(data)))

		return _inner

	# -------------------- 工具方法 --------------------
	async def _scan_once(self, timeout: float = 5.0) -> List[BLEDevice]:
		devices = await BleakScanner.discover(timeout=timeout)
		return devices

	def scan(self, timeout: float = 5.0) -> List[Tuple[str, Optional[str]]]:
		"""同步扫描附近设备，返回 (address, name)。"""
		fut = self._submit(self._scan_once(timeout))
		devices = fut.result(timeout=timeout + 5)
		return [(d.address, d.name) for d in devices]

	def _get_or_create_ctx(self, address: str) -> _ClientCtx:
		address_norm = self._normalize_address(address)
		ctx = self._clients.get(address_norm)
		if ctx is None:
			client = BleakClient(address_norm)
			ctx = _ClientCtx(
				client=client,
				rw_lock=asyncio.Lock(),
				connect_lock=asyncio.Lock(),
				notify_handlers={},
			)
			self._clients[address_norm] = ctx
		return ctx

	async def _ensure_connected(self, address: str, try_time: int = 1, timeout: float = 10.0) -> BleakClient:
		address_norm = self._normalize_address(address)
		ctx = self._get_or_create_ctx(address_norm)
		# 快路径：已连接则直接返回，避免频繁获取 connect_lock 增加延迟
		if ctx.client.is_connected:
			return ctx.client
		async with ctx.connect_lock:
			if ctx.client.is_connected:
				return ctx.client
			# 未连接，进行重试连接
			last_err: Optional[Exception] = None
			for _ in range(max(1, try_time)):
				try:
					await ctx.client.connect(timeout=timeout)
					if ctx.client.is_connected:
						print(f"Connected to {address_norm}")
						return ctx.client
				except Exception as e:
					last_err = e
				# 小间隔后再试
				await asyncio.sleep(0.2)
			# 连接失败
			if last_err:
				raise last_err
			raise RuntimeError(f"Connect to {address_norm} failed")

	# -------------------- 对外 API --------------------
	def get_local_info(self) -> Dict[str, Any]:
		"""返回本机蓝牙适配器相关信息。

		说明：PC 端通常作为 Central，不提供本地 GATT 特征；因此这里不返回“本机特征”。
		若需要查看附近外设的特征，请对具体设备调用 get_characteristics(address)。
		"""
		info: Dict[str, Any] = {
			"bleak_version": getattr(bleak, "__version__", "unknown"),
			"platform": "Windows",
		}
		# Windows: 可选获取适配器地址
		if _win_adapter_available and _win_adapter_mod is not None:
			try:
				# 异步 API：get_default_async
				loop = asyncio.new_event_loop()
				adapter = loop.run_until_complete(_win_adapter_mod.BluetoothAdapter.get_default_async())
				loop.close()
				if adapter:
					addr64 = adapter.bluetooth_address
					info["adapter_address"] = _format_bt_addr64(addr64)
					info["is_low_energy_supported"] = bool(adapter.is_low_energy_supported)
					info["is_classic_supported"] = bool(adapter.is_classic_supported)
				else:
					info["adapter_address"] = None
			except Exception as e:
				info["adapter_error"] = str(e)
		else:
			info["adapter_address"] = None
			info["note"] = "Optional winsdk/winrt not available; address may be unavailable."
		# 顺便返回一次快速扫描摘要（不阻塞太久）
		try:
			devices = self.scan(timeout=3.0)
			info["nearby_devices"] = [
				{"address": a, "name": n} for a, n in devices[:10]
			]
		except Exception:
			info["nearby_devices"] = []
		return info

	def connect(self, address: str, try_time: int = 3, timeout: float = 10.0) -> bool:
		"""扫描附近设备并连接指定地址，成功返回 True，失败返回 False。"""
		try:
			_ = self.scan(timeout=2.0)
		except Exception:
			pass

		fut = self._submit(self._ensure_connected(address, try_time=try_time, timeout=timeout))
		try:
			fut.result(timeout=timeout + 5)
			return True
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Connect timeout for %s", address)
		except Exception:
			logger.exception("Connect failed for %s", address)
		return False

	def is_connected(self, address: str) -> bool:
		ctx = self._clients.get(self._normalize_address(address))
		return bool(ctx and ctx.client.is_connected)

	def disconnect(self, address: str) -> None:
		async def _do() -> None:
			address_norm = self._normalize_address(address)
			ctx = self._clients.get(address_norm)
			if not ctx:
				return
			try:
				for char_uuid in list(ctx.notify_handlers.keys()):
					try:
						await ctx.client.stop_notify(char_uuid)
					except Exception:
						pass
				ctx.notify_handlers.clear()
				if ctx.client.is_connected:
					await ctx.client.disconnect()
					print(f"Disconnected from {address_norm}")
			finally:
				# 清理上下文，下一次会重建
				self._clients.pop(address_norm, None)

		fut = self._submit(_do())
		fut.result(timeout=10)

	def get_characteristics(self, address: str) -> List[Dict[str, Any]]:
		async def _do() -> List[Dict[str, Any]]:
			address_norm = self._normalize_address(address)
			client = await self._ensure_connected(address_norm)
			# bleak 版本兼容：有的版本提供 await client.get_services()，也有的暴露 client.services
			services_collection = None
			get_services = getattr(client, "get_services", None)
			if callable(get_services):
				maybe = get_services()
				if asyncio.iscoroutine(maybe):
					services_collection = await maybe
				else:
					services_collection = maybe
			else:
				services_collection = getattr(client, "services", None)
				if services_collection is None:
					# 尝试通过设备服务发现触发刷新（部分后端在连接时已拉取）
					# 若不可用，则抛出清晰错误
					raise RuntimeError("Bleak client has no services available; ensure connected and supported version.")

			# 归一化为可迭代
			services_iter: Iterable[Any]
			if hasattr(services_collection, "__iter__"):
				services_iter = cast(Iterable[Any], services_collection)
			else:
				inner = getattr(services_collection, "services", None)
				if inner is not None and hasattr(inner, "__iter__"):
					services_iter = cast(Iterable[Any], inner)
				else:
					raise RuntimeError("Services collection is not iterable; unsupported bleak backend/version.")

			result: List[Dict[str, Any]] = []
			for svc in services_iter:
				chars = [
					{
						"uuid": ch.uuid,
						"description": ch.description,
						"properties": list(ch.properties),
					}
					for ch in svc.characteristics
				]
				result.append({"service_uuid": svc.uuid, "characteristics": chars})
			print(f"Fetched {len(result)} services for {address_norm}")
			return result

		fut = self._submit(_do())
		try:
			return fut.result(timeout=20)
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Get characteristics timeout for %s", address)
		except BleakError:
			logger.exception("Get characteristics failed for %s", address)
		except Exception:
			logger.exception("Unexpected error getting characteristics for %s", address)
		return []

	def read_characteristic(self, address: str, char_uuid: str, timeout: float = 10.0) -> Optional[bytes]:
		async def _do() -> bytes:
			address_norm = self._normalize_address(address)
			client = await self._ensure_connected(address_norm)
			ctx = self._clients[address_norm]
			async with ctx.rw_lock:
				data = await client.read_gatt_char(char_uuid)
				print(f"Read {len(data)} bytes from {address_norm} {char_uuid}")
				return data

		fut = self._submit(_do())
		try:
			return fut.result(timeout=timeout + 5)
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Read characteristic timeout for %s %s", address, char_uuid)
		except BleakError:
			logger.exception("Read characteristic failed for %s %s", address, char_uuid)
		except Exception:
			logger.exception("Unexpected error reading %s %s", address, char_uuid)
		return None

	def write_characteristic(
		self,
		address: str,
		char_uuid: str,
		payload: Union[bytes, bytearray, List[int], str],
		response: bool = True,
		timeout: float = 10.0,
	) -> bool:
		async def _do() -> bool:
			address_norm = self._normalize_address(address)
			client = await self._ensure_connected(address_norm)
			ctx = self._clients[address_norm]
			# 统一转换 payload
			data: bytes
			if isinstance(payload, (bytes, bytearray)):
				data = bytes(payload)
			elif isinstance(payload, list):
				data = bytes(payload)
			elif isinstance(payload, str):
				# 支持 "01 02 0A" 或 "01020A" 形式的十六进制字符串
				s = payload.replace(" ", "").replace("-", "").replace(":", "")
				if len(s) % 2 != 0:
					raise ValueError("Hex string length must be even")
				data = bytes.fromhex(s)
			else:
				raise TypeError("payload must be bytes/bytearray/list[int]/hex str")

			async with ctx.rw_lock:
				await client.write_gatt_char(char_uuid, data, response=response)
			# print(f"Wrote {len(data)} bytes to {address_norm} {char_uuid}")
			return True

		fut = self._submit(_do())
		try:
			fut.result(timeout=timeout + 5)
			return True
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Write characteristic timeout for %s %s", address, char_uuid)
		except BleakError:
			logger.exception("Write characteristic failed for %s %s", address, char_uuid)
		except Exception:
			logger.exception("Unexpected error writing %s %s", address, char_uuid)
		return False

	def start_notify(
		self,
		address: str,
		char_uuid: str,
		callback: NotifyCallback,
		timeout: float = 10.0,
	) -> bool:
		if not callable(callback):
			raise TypeError("callback must be callable")

		async def _do() -> bool:
			address_norm = self._normalize_address(address)
			char_uuid_clean = char_uuid.strip()
			char_key = self._normalize_char_uuid(char_uuid_clean)
			client = await self._ensure_connected(address_norm)
			ctx = self._clients[address_norm]
			if char_key in ctx.notify_handlers:
				raise ValueError(f"Notification already active for {char_uuid_clean}")
			wrapper = self._make_notify_wrapper(address_norm, char_uuid_clean, callback)
			await client.start_notify(char_uuid_clean, wrapper)
			ctx.notify_handlers[char_key] = (callback, wrapper)
			print(f"Started notify on {address_norm} {char_uuid_clean}")
			return True

		fut = self._submit(_do())
		try:
			fut.result(timeout=timeout + 5)
			return True
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Start notify timeout for %s %s", address, char_uuid)
		except (BleakError, ValueError):
			logger.exception("Start notify failed for %s %s", address, char_uuid)
		except Exception:
			logger.exception("Unexpected error starting notify %s %s", address, char_uuid)
		return False

	def stop_notify(self, address: str, char_uuid: str, timeout: float = 10.0) -> bool:
		async def _do() -> bool:
			address_norm = self._normalize_address(address)
			ctx = self._clients.get(address_norm)
			if not ctx:
				return False
			char_uuid_clean = char_uuid.strip()
			char_key = self._normalize_char_uuid(char_uuid_clean)
			entry = ctx.notify_handlers.pop(char_key, None)
			if entry is None:
				return False
			if ctx.client.is_connected:
				try:
					await ctx.client.stop_notify(char_uuid_clean)
				except Exception:
					logger.exception(
						"Failed to stop notify for %s %s", address_norm, char_uuid_clean
					)
			print(f"Stopped notify on {address_norm} {char_uuid_clean}")
			return True

		fut = self._submit(_do())
		try:
			return fut.result(timeout=timeout + 5)
		except FutureTimeoutError:
			fut.cancel()
			logger.warning("Stop notify timeout for %s %s", address, char_uuid)
		except Exception:
			logger.exception("Unexpected error stopping notify %s %s", address, char_uuid)
		return False


# -------------------- 使用便捷实例 --------------------
_default_manager: Optional[BLEManager] = None


def _mgr() -> BLEManager:
	global _default_manager
	if _default_manager is None:
		_default_manager = BLEManager()
		_default_manager.start()
	return _default_manager


# 面向题目要求的顶层函数封装（便于直接导入使用）
def get_local_info() -> Dict[str, Any]:
	return _mgr().get_local_info()


def connect(address: str, try_time: int = 3, timeout: float = 10.0) -> bool:
	return _mgr().connect(address, try_time=try_time, timeout=timeout)


def is_connected(address: str) -> bool:
	return _mgr().is_connected(address)


def disconnect(address: str) -> None:
	_mgr().disconnect(address)


def get_characteristics(address: str) -> List[Dict[str, Any]]:
	return _mgr().get_characteristics(address)


def read_characteristic(address: str, char_uuid: str, timeout: float = 10.0) -> Optional[bytes]:
	return _mgr().read_characteristic(address, char_uuid, timeout=timeout)


def write_characteristic(
	address: str,
	char_uuid: str,
	payload: Union[bytes, bytearray, List[int], str],
	response: bool = True,
	timeout: float = 10.0,
) -> bool:
	return _mgr().write_characteristic(address, char_uuid, payload, response=response, timeout=timeout)


def start_notify(
	address: str,
	char_uuid: str,
	callback: NotifyCallback,
	timeout: float = 10.0,
) -> bool:
	return _mgr().start_notify(address, char_uuid, callback, timeout=timeout)


def stop_notify(address: str, char_uuid: str, timeout: float = 10.0) -> bool:
	return _mgr().stop_notify(address, char_uuid, timeout=timeout)


__all__ = [
	"BLEManager",
	"get_local_info",
	"connect",
	"is_connected",
	"disconnect",
	"get_characteristics",
	"read_characteristic",
	"write_characteristic",
	"start_notify",
	"stop_notify",
]


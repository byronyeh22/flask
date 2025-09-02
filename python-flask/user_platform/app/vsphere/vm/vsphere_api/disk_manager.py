# app/vsphere/vm/vsphere_api/disk_manager.py
import os
import ssl
import time
import logging
from typing import Tuple, List, Dict, Optional

# ===== 動態決定要用 mock 還是真實 vCenter =====
_USE_MOCK = os.environ.get("VSPHERE_USE_MOCK", "0") == "1"

if _USE_MOCK:
    # 你的 mock_api.py 需提供下列介面
    from .mock_api import SmartConnect, Disconnect
    from .mock_api import vim  # 模擬 pyVmomi.vim
else:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim

# ===== vCenter 連線設定（真實模式才會用到；mock 模式忽略）=====
_VCENTER_HOST = os.environ.get("VSPHERE_HOST", "127.0.0.1")
_VCENTER_USER = os.environ.get("VSPHERE_USER", "administrator@vsphere.local")
_VCENTER_PASSWORD = os.environ.get("VSPHERE_PASSWORD", "password")
_VCENTER_DISABLE_SSL_VERIFY = os.environ.get("VSPHERE_DISABLE_SSL_VERIFY", "1") == "1"

# ===== SCSI 上限與規則 =====
_MAX_SCSI_BUS = 3          # bus 0..3 共 4 顆 controller
_MAX_UNITS_PER_BUS = 16    # unit 0..15
_RESERVED_UNIT = 7         # SCSI 上保留的號碼
_OS_BOOT_SLOT = (0, 0)     # OS 系統碟慣例放在 scsi(0:0)

# --------------------------------------------------------------------------------------
# 共用核心：wait_for_task / get_obj
# --------------------------------------------------------------------------------------
def wait_for_task(task):
    """等待 vSphere Task 完成（與你原本行為一致）"""
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(1)
    if task.info.state == 'success':
        return task.info.result
    else:
        raise Exception(f"Task failed: {task.info.error.msg if task.info.error else 'Unknown error'}")

def get_obj(content, vimtype, name):
    """根據名稱尋找 vSphere 物件（與你原本行為一致）"""
    view = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
    try:
        for obj in view.view:
            if obj.name == name:
                return obj
        return None
    finally:
        try:
            view.Destroy()
        except Exception:
            pass

# --------------------------------------------------------------------------------------
# 連線裝飾器：參考你原本 vcenter_connector 行為（支援 mock 與真實）
# --------------------------------------------------------------------------------------
def vcenter_connector(func):
    """
    包住需要 si 的函式。
    - 在 mock 模式會呼叫 mock_api.SmartConnect / Disconnect
    - 在真實模式會呼叫 pyVim.connect.SmartConnect / Disconnect
    """
    def wrapper(*args, **kwargs):
        si = None
        try:
            if _USE_MOCK:
                si = SmartConnect()
            else:
                ctx = None
                if _VCENTER_DISABLE_SSL_VERIFY:
                    ctx = ssl._create_unverified_context()
                si = SmartConnect(
                    host=_VCENTER_HOST,
                    user=_VCENTER_USER,
                    pwd=_VCENTER_PASSWORD,
                    sslContext=ctx,
                    disableSslCertValidation=_VCENTER_DISABLE_SSL_VERIFY
                )
            if not si:
                raise ConnectionError("Could not connect to vCenter.")
            return func(si, *args, **kwargs)
        finally:
            if si:
                try:
                    Disconnect(si)
                except Exception:
                    pass
    return wrapper

# --------------------------------------------------------------------------------------
# 查詢 VM 磁碟
# --------------------------------------------------------------------------------------
def get_vm_disks(si, vm_name: str) -> List[Dict]:
    """查詢指定 VM 上的所有虛擬硬碟（跳過 OS 盤 0:0）"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm:
        raise ValueError(f"VM '{vm_name}' not found.")

    # 建立 controller key -> busNumber 對照
    controller_bus_by_key: Dict[int, int] = {}
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualSCSIController):
            controller_bus_by_key[dev.key] = dev.busNumber

    disks: List[Dict] = []
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            bus = controller_bus_by_key.get(dev.controllerKey, -1)
            unit = dev.unitNumber
            # 跳過 OS 系統碟位（0:0）
            if (bus, unit) == _OS_BOOT_SLOT:
                continue

            # 判斷 Provisioning 類型（延續你原本的判斷）
            provisioning = (
                "Thin" if getattr(dev.backing, "thinProvisioned", False)
                else "Thick (Eager Zeroed)" if getattr(dev.backing, "eagerlyScrub", False)
                else "Thick (Lazy Zeroed)"
            )
            disks.append({
                "key": dev.key,
                "label": dev.deviceInfo.label,
                "capacity_gb": dev.capacityInKB // (1024 * 1024),
                "provisioning": provisioning,
                "unit_number": unit,
                "controller_bus": bus,
                "vmdk_path": dev.backing.fileName
            })
    # 依 bus, unit 排序
    return sorted(disks, key=lambda d: (d["controller_bus"], d["unit_number"]))

# --------------------------------------------------------------------------------------
# 內部工具：蒐集 SCSI 狀態/建立 Controller/尋找下一個可用 slot
# --------------------------------------------------------------------------------------
def _collect_scsi_state(vm) -> Tuple[List[vim.vm.device.VirtualSCSIController], Dict[int, int], Dict[int, set]]:
    """
    回傳：
      controllers: list of controller
      controller_bus_by_key: {controllerKey -> busNumber}
      used_units_by_ctrl: {controllerKey -> set(unitNumbers)}
    """
    controllers: List[vim.vm.device.VirtualSCSIController] = []
    controller_bus_by_key: Dict[int, int] = {}
    used_units_by_ctrl: Dict[int, set] = {}

    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualSCSIController):
            controllers.append(dev)
            controller_bus_by_key[dev.key] = dev.busNumber
            used_units_by_ctrl[dev.key] = set()

    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            ctrl_key = dev.controllerKey
            if ctrl_key in used_units_by_ctrl:
                used_units_by_ctrl[ctrl_key].add(dev.unitNumber)

    return controllers, controller_bus_by_key, used_units_by_ctrl

def _ensure_scsi_controller(vm, bus_number: int) -> vim.vm.device.VirtualSCSIController:
    """
    確保指定 bus 的 SCSI controller 存在；若不存在則建立一顆。
    回傳該 bus 的 controller 物件（新的或既有）。
    """
    # 先找現有
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualSCSIController) and dev.busNumber == bus_number:
            return dev

    # 沒有就建一顆（使用 LsiLogic SAS；與常見預設相容）
    controller = vim.vm.device.ParaVirtualSCSIController() if hasattr(vim.vm.device, "ParaVirtualSCSIController") else vim.vm.device.VirtualLsiLogicSASController()
    controller.busNumber = bus_number
    controller.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
    controller.key = -101  # 任意負值，讓 vCenter 分配實際 key

    spec = vim.vm.ConfigSpec()
    dev_spec = vim.vm.device.VirtualDeviceSpec()
    dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    dev_spec.device = controller
    spec.deviceChange = [dev_spec]

    wait_for_task(vm.ReconfigVM_Task(spec=spec))

    # 重新取得，拿到實際 key
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualSCSIController) and dev.busNumber == bus_number:
            return dev

    raise RuntimeError(f"Failed to create SCSI controller for bus {bus_number}.")

def _find_next_slot_auto(vm) -> Tuple[vim.vm.device.VirtualSCSIController, int]:
    """
    自動分配下一個可用 slot：
      - 永遠跳過 OS 盤位 scsi(0:0)
      - 優先塞 bus=0：unit=1..15（跳 7）
      - bus=0 塞滿後：建立 bus=1 controller，從 1:0..15（跳 7），以此類推
    """
    # 確保 bus=0 存在
    _ensure_scsi_controller(vm, 0)

    controllers, _, used_units = _collect_scsi_state(vm)
    controllers_by_bus = {c.busNumber: c for c in controllers}
    existing_buses = sorted(controllers_by_bus.keys())

    # 先試 bus=0，從 1..15（跳 7）
    c0 = controllers_by_bus.get(0) or _ensure_scsi_controller(vm, 0)
    used0 = used_units.get(c0.key, set())
    for unit in range(_MAX_UNITS_PER_BUS):
        if unit == _RESERVED_UNIT:
            continue
        if unit == 0:  # 跳過 OS 盤位
            continue
        if unit not in used0:
            return c0, unit

    # bus=0 滿了 → 往 1.._MAX_SCSI_BUS
    for bus in range(1, _MAX_SCSI_BUS + 1):
        c = controllers_by_bus.get(bus)
        if not c:
            c = _ensure_scsi_controller(vm, bus)
            return c, 0  # 新 controller 第一顆用 0

        used = used_units.get(c.key, set())
        for unit in range(_MAX_UNITS_PER_BUS):
            if unit == _RESERVED_UNIT:
                continue
            if unit not in used:
                return c, unit

    raise Exception(f"No available SCSI slot up to bus {_MAX_SCSI_BUS}.")

# --------------------------------------------------------------------------------------
# 建立/刪除/調整磁碟
# --------------------------------------------------------------------------------------
def add_disk_to_vm(si, vm_name: str, disk_spec: Dict) -> str:
    """
    新增硬碟到指定 VM。
    - 若 disk_spec 帶 controller_id：沿用舊行為（在該 bus 自動找 unit，規則同跳 7；若 bus=0 會跳過 0:0）
    - 若未帶 controller_id：走自動分配（0:1.. → 1:0..，跳 7；必要時自動建立 controller）
    disk_spec keys:
      - size_gb (int)            : 必填
      - provision_type (str)     : 'thin' | 'thick_lazy' | 'thick_eager'
      - controller_id (int)      : 選填（建議不帶，讓系統自動決定）
    """
    size_gb = int(disk_spec["size_gb"])
    prov = (disk_spec.get("provision_type") or "").lower().strip()

    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm:
        raise ValueError(f"VM '{vm_name}' not found.")

    # 取得目標 controller 與 unit
    if "controller_id" in disk_spec and disk_spec["controller_id"] is not None:
        target_bus = int(disk_spec["controller_id"])
        ctrl = _ensure_scsi_controller(vm, target_bus)

        # 找 unit；bus=0 需跳過 0；所有 bus 跳 7
        used_units = {d.unitNumber for d in vm.config.hardware.device
                      if isinstance(d, vim.vm.device.VirtualDisk) and d.controllerKey == ctrl.key}
        start_unit = 1 if target_bus == 0 else 0
        unit = None
        for u in range(start_unit, _MAX_UNITS_PER_BUS):
            if u == _RESERVED_UNIT:
                continue
            if target_bus == 0 and u == 0:
                continue
            if u not in used_units:
                unit = u
                break
        if unit is None:
            raise Exception(f"No available unit on SCSI controller {target_bus}.")
        controller, unit_number = ctrl, unit
    else:
        controller, unit_number = _find_next_slot_auto(vm)

    # 建立磁碟 Spec
    spec = vim.vm.ConfigSpec()
    dev_spec = vim.vm.device.VirtualDeviceSpec()
    dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    dev_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create

    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode='persistent')
    if prov == 'thin':
        backing.thinProvisioned = True
    elif prov == 'thick_eager':
        backing.eagerlyScrub = True
    # thick_lazy -> 兩個 flag 都不設

    new_disk = vim.vm.device.VirtualDisk()
    new_disk.key = -1
    new_disk.controllerKey = controller.key
    new_disk.unitNumber = unit_number
    new_disk.backing = backing
    new_disk.capacityInKB = size_gb * 1024 * 1024

    dev_spec.device = new_disk
    spec.deviceChange = [dev_spec]

    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully added {size_gb}GB disk to '{vm_name}' at scsi({controller.busNumber}:{unit_number})."

def remove_disk_from_vm(si, vm_name: str, disk_key: int) -> str:
    """移除指定 VM 的硬碟（destroy VMDK）"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm:
        raise ValueError(f"VM '{vm_name}' not found.")

    target = None
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk) and dev.key == disk_key:
            target = dev
            break
    if not target:
        raise ValueError(f"Disk with key {disk_key} not found.")

    spec = vim.vm.ConfigSpec()
    dev_spec = vim.vm.device.VirtualDeviceSpec()
    dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
    dev_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.destroy
    dev_spec.device = target
    spec.deviceChange = [dev_spec]

    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully removed disk (key: {disk_key}) from '{vm_name}'."

def update_disk_size(si, vm_name: str, disk_key: int, new_size_gb: int) -> str:
    """放大指定硬碟（不可縮小）"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm:
        raise ValueError(f"VM '{vm_name}' not found.")

    disk = None
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk) and dev.key == disk_key:
            disk = dev
            break
    if not disk:
        raise ValueError(f"Disk with key {disk_key} not found.")

    current_gb = disk.capacityInKB // (1024 * 1024)
    if new_size_gb < current_gb:
        raise ValueError(f"New size ({new_size_gb}GB) cannot be smaller than current size ({current_gb}GB).")
    if new_size_gb == current_gb:
        return f"Disk size is already {new_size_gb}GB. No changes made."

    disk.capacityInKB = new_size_gb * 1024 * 1024

    spec = vim.vm.ConfigSpec()
    dev_spec = vim.vm.device.VirtualDeviceSpec()
    dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
    dev_spec.device = disk
    spec.deviceChange = [dev_spec]

    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully updated disk (key: {disk_key}) on '{vm_name}' to {new_size_gb}GB."
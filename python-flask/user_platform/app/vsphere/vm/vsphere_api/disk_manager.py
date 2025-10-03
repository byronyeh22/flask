# app/vsphere/vm/vsphere_api/disk_manager.py
import ssl
import time
import logging
from typing import Tuple, List, Dict, Optional
from app.vsphere.vm.db.vm_provisioning_manager import sync_disk_labels_to_database


from flask import current_app

logger = logging.getLogger(__name__)

# ===== SCSI 上限與規則 =====
_MAX_SCSI_BUS = 3          # bus 0..3 共 4 顆 controller
_MAX_UNITS_PER_BUS = 16    # unit 0..15
_RESERVED_UNIT = 7         # SCSI 上保留的號碼
_OS_BOOT_SLOT = (0, 0)     # OS 系統碟慣例放在 scsi(0:0)

# --------------------------------------------------------------------------------------
# 環境判斷 & 依模式載入依賴
# --------------------------------------------------------------------------------------
def _is_local_mode() -> bool:
    try:
        return (current_app and (current_app.config.get('API_MODE') or '').lower() == 'local')
    except RuntimeError:
        return False

if not _is_local_mode():
    # 真實 vCenter 連線才需要 pyVmomi
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
else:
    # local 模式不載入 pyVmomi，避免誤用
    SmartConnect = None
    Disconnect = None
    vim = None
    import requests  # 只在 local 使用 HTTP mock

# --------------------------------------------------------------------------------------
# 共用核心（僅真實模式會用到）：wait_for_task / get_obj
# --------------------------------------------------------------------------------------
if not _is_local_mode():
    def wait_for_task(task):
        """等待 vSphere Task 完成"""
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(1)
        if task.info.state == 'success':
            return task.info.result
        else:
            raise Exception(f"Task failed: {getattr(task.info.error, 'msg', 'Unknown error')}")

    def get_obj(content, vimtype, name):
        """根據名稱尋找 vSphere 物件"""
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
# 連線：參考 config.py（current_app.config）
# - local：不連 vCenter，直接走 HTTP mock
# - real ：使用 pyVmomi 連線
# --------------------------------------------------------------------------------------
def _connect_vcenter_from_config():
    """
    非 local 模式才會真的連 vCenter。
    local 模式下會由裝飾器直接跳過連線。
    """
    api_mode = (current_app.config.get('API_MODE') or '').lower()
    if api_mode == 'local':
        return None  # local 不需要 vCenter 連線

    host = current_app.config.get('VSPHERE_HOST')
    user = current_app.config.get('VSPHERE_USER')
    password = current_app.config.get('VSPHERE_PASSWORD')
    disable_verify = str(current_app.config.get('VSPHERE_DISABLE_SSL_VERIFY', '1')).lower() in ('1', 'true', 'yes')

    if not host or not user or not password:
        raise ValueError("缺少 vSphere 連線設定（VSPHERE_HOST / VSPHERE_USER / VSPHERE_PASSWORD）")

    ssl_ctx = ssl._create_unverified_context() if disable_verify else None
    si = SmartConnect(host=host, user=user, pwd=password, sslContext=ssl_ctx,
                      disableSslCertValidation=disable_verify)
    return si

def _with_connection(func):
    """裝飾器：幫需要 si 的函式自動建立/關閉連線；local 模式直接跳過連線。"""
    def wrapper(*args, **kwargs):
        if _is_local_mode():
            # local：不建立 vCenter 連線，直接呼叫
            return func(None, *args, **kwargs)

        si = None
        try:
            si = _connect_vcenter_from_config()
            return func(si, *args, **kwargs)
        finally:
            if si:
                try:
                    Disconnect(si)
                except Exception:
                    pass
    return wrapper


def _sync_all_disk_labels_after_operation(vm) -> List[Dict]:
    """在磁盤操作後，重新讀取 VM 所有磁盤的最新狀態"""
    if _is_local_mode():
        return []  # local 模式不需要同步
    disk_info = []
    
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            # 取得控制器資訊
            controller_bus = None
            for ctrl_dev in vm.config.hardware.device:
                if (isinstance(ctrl_dev, vim.vm.device.VirtualSCSIController) 
                    and ctrl_dev.key == dev.controllerKey):
                    controller_bus = ctrl_dev.busNumber
                    break
            
            # 跳過 OS 盤 (scsi 0:0)
            if controller_bus == 0 and dev.unitNumber == 0:
                continue
                
            label_text = getattr(dev.deviceInfo, "label", None)
            label_number = int(label_text.replace("Hard disk ", "")) if label_text else None
            
            disk_info.append({
                "key": dev.key,
                "controller_bus": controller_bus,
                "unit_number": dev.unitNumber,
                "label_text": label_text,
                "label_number": label_number,
                "vmdk_path": getattr(dev.backing, "fileName", None)
            })
    
    return disk_info

# --------------------------------------------------------------------------------------
# 真實模式專用工具（local 用不到）
# --------------------------------------------------------------------------------------
if not _is_local_mode():
    def _get_vm_by_name(si, vm_name: str):
        content = si.RetrieveContent()
        vm = get_obj(content, [vim.VirtualMachine], vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found.")
        return vm

    def _collect_scsi_state(vm) -> Tuple[List['vim.vm.device.VirtualSCSIController'], Dict[int, int], Dict[int, set]]:
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

    def _ensure_scsi_controller(vm, bus_number: int):
        # 先找現有
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualSCSIController) and dev.busNumber == bus_number:
                return dev

        # 沒有就建一顆（優先 ParaVirtual；否則 LsiLogic SAS）
        controller = (
            vim.vm.device.ParaVirtualSCSIController()
            if hasattr(vim.vm.device, "ParaVirtualSCSIController")
            else vim.vm.device.VirtualLsiLogicSASController()
        )
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

    def _find_next_slot_auto(vm):
        """
        自動分配下一個可用 slot：
          - 永遠跳過 OS 盤位 scsi(0:0)
          - 優先塞 bus=0：unit=1..15（跳 7）
          - bus=0 塞滿後：建立/使用 bus=1..3，依序挑 unit=0..15（跳 7）
        """
        _ensure_scsi_controller(vm, 0)  # 確保 bus=0 存在

        controllers, _, used_units = _collect_scsi_state(vm)
        controllers_by_bus = {c.busNumber: c for c in controllers}

        # 先試 bus=0，從 1..15（跳 7；避開 0:0）
        controller_bus0 = controllers_by_bus.get(0) or _ensure_scsi_controller(vm, 0)
        used_on_bus0 = used_units.get(controller_bus0.key, set())
        for unit in range(_MAX_UNITS_PER_BUS):
            if unit in (0, _RESERVED_UNIT):
                continue
            if unit not in used_on_bus0:
                return controller_bus0, unit

        # bus=0 滿了 → 往 1.._MAX_SCSI_BUS
        for bus in range(1, _MAX_SCSI_BUS + 1):
            controller = controllers_by_bus.get(bus)
            if not controller:
                controller = _ensure_scsi_controller(vm, bus)
                return controller, 0  # 新 controller 第一顆用 0

            used_on_bus = used_units.get(controller.key, set())
            for unit in range(_MAX_UNITS_PER_BUS):
                if unit == _RESERVED_UNIT:
                    continue
                if unit not in used_on_bus:
                    return controller, unit

        raise Exception(f"No available SCSI slot up to bus {_MAX_SCSI_BUS}.")

# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _parse_label_number(label_text: Optional[str]) -> Optional[int]:
    """將 vSphere deviceInfo.label（如 'Hard disk 2'）解析出數字 2。"""
    if not label_text:
        return None
    try:
        parts = label_text.strip().split()
        last = parts[-1]
        return int(last) if last.isdigit() else None
    except Exception:
        return None

def _mock_base() -> str:
    """
    取得 mock vSphere API 的 base URL。
    直接使用 config.VSPHERE_HOST，並確保補上 /mock。
    """
    host = (current_app.config.get("VSPHERE_HOST") or "").rstrip("/")
    return host if host.endswith("/mock") else host + "/mock"

# --------------------------------------------------------------------------------------
# 查詢 VM 磁碟（公開）
# --------------------------------------------------------------------------------------
@_with_connection
def get_vm_disks(si, vm_name: str) -> List[Dict]:
    """
    查詢指定 VM 上的所有虛擬硬碟（跳過 OS 盤 0:0）。vm_name 就是 vm_name_prefix。
    local：呼叫 GET /mock/vsphere/vms/<vm>/disks
    real ：讀取 pyVmomi VM 裝置
    """
    if _is_local_mode():
        # 先確保 VM 存在（避免第一次查詢報不存在）
        try:
            requests.post(f"{_mock_base()}/vsphere/vms/{vm_name}/ensure", timeout=5)
        except Exception:
            pass

        import requests
        r = requests.get(f"{_mock_base()}/vsphere/vms/{vm_name}/disks", timeout=10)
        r.raise_for_status()
        items = r.json() or []
        # 直接以 mock 結構轉換
        disks = []
        for it in items:
            disks.append({
                "key": it.get("key"),
                "label": it.get("label_text"),
                "capacity_gb": it.get("capacity_gb"),
                "provisioning": it.get("provision_type"),
                "unit_number": it.get("unit_number"),
                "controller_bus": it.get("controller_bus"),
                "vmdk_path": it.get("vmdk_path"),
            })
        return sorted(disks, key=lambda d: (d["controller_bus"], d["unit_number"]))

    # ------- 真實 pyVmomi -------
    vm = _get_vm_by_name(si, vm_name)

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
            if (bus, unit) == _OS_BOOT_SLOT:
                continue  # 跳過 OS 盤位 0:0

            provisioning = (
                "Thin" if getattr(dev.backing, "thinProvisioned", False)
                else "Thick (Eager Zeroed)" if getattr(dev.backing, "eagerlyScrub", False)
                else "Thick (Lazy Zeroed)"
            )
            disks.append({
                "key": dev.key,
                "label": getattr(dev.deviceInfo, "label", None),
                "capacity_gb": dev.capacityInKB // (1024 * 1024),
                "provisioning": provisioning,
                "unit_number": unit,
                "controller_bus": bus,
                "vmdk_path": getattr(dev.backing, "fileName", None),
            })
    return sorted(disks, key=lambda d: (d["controller_bus"], d["unit_number"]))

# --------------------------------------------------------------------------------------
# 建立/刪除/調整磁碟（公開）
# --------------------------------------------------------------------------------------
@_with_connection
def add_disk_to_vm(si, vm_name: str, disk_spec: Dict) -> Dict[str, Optional[object]]:
    """
    新增硬碟到指定 VM（vm_name 就是 vm_name_prefix）。
    disk_spec:
      - size_gb (int)            : 必填
      - provision_type (str)     : 'thin' | 'thick_lazy' | 'thick_eager'
      - controller_id (int)      : 選填（指定 bus；建議不帶）
    回傳（可直接回寫 DB）：
      controller_bus, unit_number, label_text, label_number, vmdk_path, size_gb, provision_type
    """
    size_gb = int(disk_spec["size_gb"])
    provision_type = (disk_spec.get("provision_type") or "").lower().strip()

    if _is_local_mode():
        import requests
        # 確保 VM 存在
        requests.post(f"{_mock_base()}/vsphere/vms/{vm_name}/ensure", timeout=5)

        payload = {
            "size_gb": size_gb,
            "provision_type": provision_type or "thin",
        }
        if disk_spec.get("controller_id") is not None:
            payload["controller_id"] = int(disk_spec["controller_id"])

        r = requests.post(f"{_mock_base()}/vsphere/vms/{vm_name}/disks", json=payload, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
        
        # 執行操作後重新查詢並同步最新狀態
        try:
            r_disks = requests.get(f"{_mock_base()}/vsphere/vms/{vm_name}/disks", timeout=10)
            if r_disks.status_code == 200:
                updated_disks = r_disks.json() or []
                sync_data = []
                for disk in updated_disks:
                    sync_data.append({
                        "key": disk.get("key"),
                        "controller_bus": disk.get("controller_bus"),
                        "unit_number": disk.get("unit_number"),
                        "label_text": disk.get("label_text"),
                        "label_number": disk.get("label_number"),
                        "vmdk_path": disk.get("vmdk_path")
                    })
                sync_disk_labels_to_database(vm_name, sync_data)
        except Exception as e:
            logging.warning(f"Failed to sync disk labels in local mode: {e}")
        
        return {
            "controller_bus": data.get("controller_bus"),
            "unit_number": data.get("unit_number"),
            "label_text": data.get("label_text"),
            "label_number": data.get("label_number"),
            "vmdk_path": data.get("vmdk_path"),
            "size_gb": data.get("size_gb"),
            "provision_type": data.get("provision_type"),
        }

    # ------- 真實 pyVmomi -------
    vm = _get_vm_by_name(si, vm_name)

    # 取得目標 controller 與 unit
    if "controller_id" in disk_spec and disk_spec["controller_id"] is not None:
        target_bus = int(disk_spec["controller_id"])
        controller = _ensure_scsi_controller(vm, target_bus)

        used_units = {d.unitNumber for d in vm.config.hardware.device
                      if isinstance(d, vim.vm.device.VirtualDisk) and d.controllerKey == controller.key}
        start_unit = 1 if target_bus == 0 else 0
        unit_number = None
        for candidate in range(start_unit, _MAX_UNITS_PER_BUS):
            if candidate == _RESERVED_UNIT:
                continue
            if target_bus == 0 and candidate == 0:
                continue
            if candidate not in used_units:
                unit_number = candidate
                break
        if unit_number is None:
            raise Exception(f"No available unit on SCSI controller {target_bus}.")
    else:
        controller, unit_number = _find_next_slot_auto(vm)

    # 建立磁碟 Spec
    spec = vim.vm.ConfigSpec()
    dev_spec = vim.vm.device.VirtualDeviceSpec()
    dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    dev_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create

    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode='persistent')
    if provision_type == 'thin':
        backing.thinProvisioned = True
    elif provision_type == 'thick_eager':
        backing.eagerlyScrub = True
    # 'thick_lazy' -> 兩個 flag 都不設

    new_disk = vim.vm.device.VirtualDisk()
    new_disk.key = -1
    new_disk.controllerKey = controller.key
    new_disk.unitNumber = unit_number
    new_disk.backing = backing
    new_disk.capacityInKB = size_gb * 1024 * 1024

    dev_spec.device = new_disk
    spec.deviceChange = [dev_spec]

    wait_for_task(vm.ReconfigVM_Task(spec=spec))

    # 重新讀取所有磁盤狀態並同步到資料庫
    all_disks = _sync_all_disk_labels_after_operation(vm)

    # 同步到資料庫
    sync_disk_labels_to_database(vm_name, all_disks)

    # 回讀新磁碟資訊（用 controllerKey + unitNumber 配對）
    target_disk = None
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            if dev.controllerKey == controller.key and dev.unitNumber == unit_number:
                target_disk = dev
                break

    label_text = getattr(target_disk.deviceInfo, "label", None) if target_disk else None
    vmdk_path = getattr(target_disk.backing, "fileName", None) if target_disk else None
    label_number = int(label_text.replace("Hard disk ", "")) if label_text else None

    return {
        "controller_bus": controller.busNumber,
        "unit_number": unit_number,
        "label_text": label_text,
        "label_number": label_number,
        "vmdk_path": vmdk_path,
        "size_gb": size_gb,
        "provision_type": provision_type or "thick_lazy",
        "all_updated_disks": all_disks
    }

@_with_connection
def remove_disk_from_vm(si, vm_name: str, disk_key: int) -> str:
    """移除指定 VM 的硬碟（destroy VMDK）"""
    if _is_local_mode():
        import requests
        r = requests.delete(f"{_mock_base()}/vsphere/vms/{vm_name}/disks/{int(disk_key)}", timeout=15)
        if r.status_code == 404:
            raise ValueError(f"Disk with key {disk_key} not found.")
        r.raise_for_status()

        # # 執行操作後重新查詢並同步最新狀態
        # try:
        #     r_disks = requests.get(f"{_mock_base()}/vsphere/vms/{vm_name}/disks", timeout=10)
        #     if r_disks.status_code == 200:
        #         updated_disks = r_disks.json() or []
        #         sync_data = []
        #         for disk in updated_disks:
        #             sync_data.append({
        #                 "key": disk.get("key"),
        #                 "controller_bus": disk.get("controller_bus"),
        #                 "unit_number": disk.get("unit_number"),
        #                 "label_text": disk.get("label_text"),
        #                 "label_number": disk.get("label_number"),
        #                 "vmdk_path": disk.get("vmdk_path")
        #             })
        #         sync_disk_labels_to_database(vm_name, sync_data)
        # except Exception as e:
        #     logging.warning(f"Failed to sync disk labels in local mode: {e}")

        return f"Successfully removed disk (key: {disk_key})."

    # ------- 真實 pyVmomi -------
    vm = _get_vm_by_name(si, vm_name)

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
    # # 重新讀取所有磁盤狀態並同步到資料庫
    # all_disks = _sync_all_disk_labels_after_operation(vm)

    # sync_disk_labels_to_database(vm_name, all_disks)

    return f"Successfully removed disk (key: {disk_key}) from '{vm_name}'"

@_with_connection
def update_disk_size(si, vm_name: str, disk_key: int, new_size_gb: int) -> str:
    """放大指定硬碟（不可縮小）"""
    if _is_local_mode():
        import requests
        payload = {"new_size_gb": int(new_size_gb)}
        r = requests.patch(f"{_mock_base()}/vsphere/vms/{vm_name}/disks/{int(disk_key)}",
                           json=payload, timeout=20)
        if r.status_code == 404:
            raise ValueError(f"Disk with key {disk_key} not found.")
        if r.status_code == 400:
            err = (r.json() or {}).get("error") or "Bad Request"
            raise ValueError(err)
        r.raise_for_status()
        # ✅ local 模式直接返回字串，不需要同步
        return f"Successfully updated disk (key: {disk_key}) on '{vm_name}' to {new_size_gb}GB."

    # ------- 真實 pyVmomi -------
    vm = _get_vm_by_name(si, vm_name)

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
    # 重新讀取所有磁盤狀態並同步到資料庫
    all_disks = _sync_all_disk_labels_after_operation(vm)

    sync_disk_labels_to_database(vm_name, all_disks)

    return {
        "message": f"Successfully updated disk (key: {disk_key}) on '{vm_name}' to {new_size_gb}GB",
        "all_updated_disks": all_disks
    }
# pyvmomi_disk_test.py (Version 5 - Restored Provisioning Display)
import ssl
import atexit
import time
from flask import Flask, request, render_template_string, flash, redirect, url_for
from pyVim import connect
from pyVmomi import vim, vmodl

# --- vCenter 連線資訊 ---
VCENTER_HOST = "172.26.1.60"
VCENTER_USER = "administrator@vsphere.local"
VCENTER_PASSWORD = "Gict@1688+"

# --- Flask App 設定 ---
app = Flask(__name__)
app.secret_key = 'a_final_secure_key_v5_with_provisioning'

# --- HTML 範本 (加回 Provisioning 欄位) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>pyVmomi Disk Management</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
</head>
<body class="bg-light">
    <div class="container my-5">
        <h2 class="mb-4 text-center">pyVmomi - VM Disk Management (Full CRUD)</h2>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                    {{ message }}
                    <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <div class="card mb-4 shadow-sm">
            <div class="card-header fw-bold">1. Query VM Disks</div>
            <div class="card-body">
                <form method="get" action="{{ url_for('index') }}">
                    <div class="input-group">
                        <input type="text" class="form-control" name="vm_name" placeholder="Enter VM name to query disks" value="{{ vm_name or '' }}" required>
                        <button class="btn btn-outline-primary" type="submit"><i class="bi bi-search"></i> Query</button>
                    </div>
                </form>
            </div>
        </div>

        {% if disks is defined %}
        <div class="card mb-4 shadow-sm">
            <div class="card-header fw-bold">Disks on {{ vm_name }}</div>
            <div class="card-body">
                {% if disks %}
                <div class="table-responsive">
                    <table class="table table-striped table-hover align-middle">
                        <thead>
                            <tr>
                                <th>Label</th>
                                <th>Capacity</th>
                                <th>Provisioning</th>
                                <th>SCSI Slot</th>
                                <th style="width: 25%;">Update Size (GB)</th>
                                <th>Delete</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for disk in disks %}
                            <tr>
                                <td>{{ disk.label }}<br><small class="text-muted">{{ disk.vmdk_path }}</small></td>
                                <td>{{ disk.capacity_gb }} GB</td>
                                <td><span class="badge bg-info text-dark">{{ disk.provisioning }}</span></td>
                                <td>scsi({{ disk.controller_bus }}:{{ disk.unit_number }})</td>
                                <td>
                                    <form method="post" action="{{ url_for('update_disk') }}" class="d-flex">
                                        <input type="hidden" name="vm_name" value="{{ vm_name }}">
                                        <input type="hidden" name="disk_key" value="{{ disk.key }}">
                                        <input type="number" name="new_size_gb" class="form-control form-control-sm me-2" 
                                               min="{{ disk.capacity_gb }}" value="{{ disk.capacity_gb }}" required>
                                        <button type="submit" class="btn btn-warning btn-sm" title="Apply new size">
                                            <i class="bi bi-arrow-up-circle-fill"></i>
                                        </button>
                                    </form>
                                </td>
                                <td>
                                    <form method="post" action="{{ url_for('remove_disk') }}" onsubmit="return confirm('Are you sure you want to PERMANENTLY delete this disk?');">
                                        <input type="hidden" name="vm_name" value="{{ vm_name }}">
                                        <input type="hidden" name="disk_key" value="{{ disk.key }}">
                                        <button type="submit" class="btn btn-danger btn-sm" title="Delete disk">
                                            <i class="bi bi-trash-fill"></i>
                                        </button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <p class="text-muted">No additional disks found on this VM.</p>
                {% endif %}
            </div>
        </div>
        {% endif %}

        {% if vm_name %}
        <div class="card shadow-sm">
            <div class="card-header fw-bold">2. Add New Disk to {{ vm_name }}</div>
            <div class="card-body">
                <form method="post" action="{{ url_for('add_disk') }}">
                    <input type="hidden" name="vm_name" value="{{ vm_name }}">
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="disk_size_gb" class="form-label">Disk Size (GB)</label>
                            <input type="number" class="form-control" id="disk_size_gb" name="disk_size_gb" min="1" value="10" required>
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="scsi_controller_id" class="form-label">Target SCSI Controller ID</label>
                            <input type="number" class="form-control" id="scsi_controller_id" name="scsi_controller_id" min="0" max="3" value="0" required>
                        </div>
                    </div>
                     <div class="mb-3">
                        <label for="provision_type" class="form-label">Disk Provisioning</label>
                        <select class="form-select" id="provision_type" name="provision_type">
                            <option value="thin">Thin Provision</option>
                            <option value="thick_lazy">Thick Provision Lazy Zeroed</option>
                            <option value="thick_eager">Thick Provision Eager Zeroed</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-primary"><i class="bi bi-plus-circle-fill"></i> Add Disk</button>
                </form>
            </div>
        </div>
        {% endif %}

    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# --- pyVmomi 核心邏輯 (無變動，原本就有) ---

def wait_for_task(task):
    """等待 vSphere Task 完成"""
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(1)
    if task.info.state == 'success':
        return task.info.result
    else:
        raise Exception(f"Task failed: {task.info.error.msg if task.info.error else 'Unknown error'}")

def get_obj(content, vimtype, name):
    """根據名稱尋找 vSphere 物件"""
    container = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
    obj = next((c for c in container.view if c.name == name), None)
    container.Destroy()
    return obj

def get_vm_disks(si, vm_name):
    """查詢指定 VM 上的所有虛擬硬碟"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm: raise ValueError(f"VM '{vm_name}' not found.")

    controller_map = {dev.key: dev.busNumber for dev in vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualSCSIController)}
    disks = []
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk):
            controller_bus = controller_map.get(device.controllerKey, -1)
            if device.unitNumber == 0 and controller_bus == 0: continue

            # *** 判斷 Provisioning 類型的邏輯一直都在 ***
            provisioning = "Thin" if device.backing.thinProvisioned else "Thick (Eager Zeroed)" if device.backing.eagerlyScrub else "Thick (Lazy Zeroed)"
            disks.append({
                "key": device.key, "label": device.deviceInfo.label,
                "capacity_gb": device.capacityInKB // (1024 * 1024),
                "provisioning": provisioning, "unit_number": device.unitNumber,
                "controller_bus": controller_bus, "vmdk_path": device.backing.fileName
            })
    return sorted(disks, key=lambda d: (d['controller_bus'], d['unit_number']))

def add_disk_to_vm(si, vm_name, disk_spec):
    """將硬碟新增到指定的 VM"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm: raise ValueError(f"VM '{vm_name}' not found.")

    scsi_controller = next((dev for dev in vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualSCSIController) and dev.busNumber == disk_spec['controller_id']), None)
    if not scsi_controller: raise ValueError(f"SCSI controller ID {disk_spec['controller_id']} not found.")

    used_units = {dev.unitNumber for dev in vm.config.hardware.device if dev.controllerKey == scsi_controller.key}
    unit_number = next((i for i in range(16) if i != 7 and i not in used_units), -1)
    if unit_number == -1: raise Exception(f"No available Unit Number on SCSI controller {disk_spec['controller_id']}.")

    spec = vim.vm.ConfigSpec()
    device_change = vim.vm.device.VirtualDeviceSpec(
        operation=vim.vm.device.VirtualDeviceSpec.Operation.add,
        fileOperation=vim.vm.device.VirtualDeviceSpec.FileOperation.create
    )
    new_disk = vim.vm.device.VirtualDisk(
        controllerKey=scsi_controller.key, unitNumber=unit_number, key=-1,
        backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode='persistent'),
        capacityInKB=disk_spec['size_gb'] * 1024 * 1024
    )
    if disk_spec['provision_type'] == 'thin': new_disk.backing.thinProvisioned = True
    elif disk_spec['provision_type'] == 'thick_eager': new_disk.backing.eagerlyScrub = True
    device_change.device = new_disk
    spec.deviceChange = [device_change]
    
    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully added {disk_spec['size_gb']}GB disk to '{vm_name}' at scsi({disk_spec['controller_id']}:{unit_number})."

def remove_disk_from_vm(si, vm_name, disk_key):
    """從指定 VM 移除硬碟"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm: raise ValueError(f"VM '{vm_name}' not found.")

    disk_to_remove = next((dev for dev in vm.config.hardware.device if dev.key == disk_key), None)
    if not disk_to_remove: raise ValueError(f"Disk with key {disk_key} not found.")

    spec = vim.vm.ConfigSpec()
    device_change = vim.vm.device.VirtualDeviceSpec(
        operation=vim.vm.device.VirtualDeviceSpec.Operation.remove,
        fileOperation=vim.vm.device.VirtualDeviceSpec.FileOperation.destroy,
        device=disk_to_remove
    )
    spec.deviceChange = [device_change]
    
    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully removed disk (key: {disk_key}) from '{vm_name}'."

def update_disk_size(si, vm_name, disk_key, new_size_gb):
    """更新指定硬碟的大小"""
    content = si.RetrieveContent()
    vm = get_obj(content, [vim.VirtualMachine], vm_name)
    if not vm: raise ValueError(f"VM '{vm_name}' not found.")

    disk_to_update = next((dev for dev in vm.config.hardware.device if dev.key == disk_key), None)
    if not disk_to_update: raise ValueError(f"Disk with key {disk_key} not found.")
    
    current_size_gb = disk_to_update.capacityInKB // (1024 * 1024)
    if new_size_gb < current_size_gb:
        raise ValueError(f"New size ({new_size_gb}GB) cannot be smaller than current size ({current_size_gb}GB).")

    if new_size_gb == current_size_gb:
        return f"Disk size is already {new_size_gb}GB. No changes made."

    disk_to_update.capacityInKB = new_size_gb * 1024 * 1024

    spec = vim.vm.ConfigSpec()
    device_change = vim.vm.device.VirtualDeviceSpec(
        operation=vim.vm.device.VirtualDeviceSpec.Operation.edit,
        device=disk_to_update
    )
    spec.deviceChange = [device_change]
    
    wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return f"Successfully updated disk (key: {disk_key}) on '{vm_name}' to {new_size_gb}GB."

# --- 共用的 vCenter 連線上下文管理器 ---
def vcenter_connector(func):
    """一個裝飾器，用於自動處理 vCenter 連線和斷線"""
    def wrapper(*args, **kwargs):
        si = None
        try:
            context = ssl._create_unverified_context()
            si = connect.SmartConnect(host=VCENTER_HOST, user=VCENTER_USER, pwd=VCENTER_PASSWORD, sslContext=context, disableSslCertValidation=True)
            if not si: raise ConnectionError("Could not connect to vCenter.")
            return func(si, *args, **kwargs)
        except Exception as e:
            raise e
        finally:
            if si: connect.Disconnect(si)
    return wrapper

# --- Flask 路由 ---
@app.route('/', methods=['GET'])
def index():
    vm_name = request.args.get('vm_name')
    if vm_name:
        try:
            disks = vcenter_connector(get_vm_disks)(vm_name=vm_name)
            return render_template_string(HTML_TEMPLATE, vm_name=vm_name, disks=disks)
        except Exception as e:
            flash(f"Error querying '{vm_name}': {e}", 'danger')
            return render_template_string(HTML_TEMPLATE, vm_name=vm_name)
    return render_template_string(HTML_TEMPLATE)

@app.route('/add_disk', methods=['POST'])
def add_disk():
    vm_name = request.form.get('vm_name')
    try:
        disk_spec = {
            'size_gb': int(request.form.get('disk_size_gb')),
            'provision_type': request.form.get('provision_type'),
            'controller_id': int(request.form.get('scsi_controller_id')),
        }
        result = vcenter_connector(add_disk_to_vm)(vm_name=vm_name, disk_spec=disk_spec)
        flash(result, 'success')
    except Exception as e:
        flash(f"Error adding disk to '{vm_name}': {e}", 'danger')
    return redirect(url_for('index', vm_name=vm_name))

@app.route('/remove_disk', methods=['POST'])
def remove_disk():
    vm_name = request.form.get('vm_name')
    disk_key = int(request.form.get('disk_key'))
    try:
        result = vcenter_connector(remove_disk_from_vm)(vm_name=vm_name, disk_key=disk_key)
        flash(result, 'success')
    except Exception as e:
        flash(f"Error removing disk from '{vm_name}': {e}", 'danger')
    return redirect(url_for('index', vm_name=vm_name))

@app.route('/update_disk', methods=['POST'])
def update_disk():
    vm_name = request.form.get('vm_name')
    disk_key = int(request.form.get('disk_key'))
    new_size_gb = int(request.form.get('new_size_gb'))
    try:
        result = vcenter_connector(update_disk_size)(vm_name=vm_name, disk_key=disk_key, new_size_gb=new_size_gb)
        flash(result, 'success')
    except Exception as e:
        flash(f"Error updating disk on '{vm_name}': {e}", 'danger')
    return redirect(url_for('index', vm_name=vm_name))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
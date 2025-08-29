from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
from flask import current_app


# 固定 mapping：不同 host 給不同 mock 選項
def _mock_from_host(host):
    host = (host or "").lower()
    if "host.docker.internal:5001" in host:
        return {
            "datacenters": ["sandbox-dc"],
            "clusters":    ["sandbox-cluster"],
            "esxi_hosts":  ["sandbox-esxi-01", "sandbox-esxi-02"],
            "templates":   ["sandbox-template-win", "sandbox-template-linux"],
            "networks":    ["sandbox-net-1"],
            "datastores":  ["sandbox-ds-1"],
            "vm_name":     ["sbx-vm-1", "sbx-vm-2"],
        }
    elif "host.docker.internal:5002" in host:
        return {
            "datacenters": ["qat-dc"],
            "clusters":    ["qat-cluster"],
            "esxi_hosts":  ["qat-esxi-01"],
            "templates":   ["qat-template-win"],
            "networks":    ["qat-net-1", "qat-net-2"],
            "datastores":  ["qat-ds-1"],
            "vm_name":     ["qat-vm-1"],
        }
    elif "host.docker.internal:5003" in host:
        return {
            "datacenters": ["uat-dc"],
            "clusters":    ["uat-cluster"],
            "esxi_hosts":  ["uat-esxi-01", "uat-esxi-02"],
            "templates":   ["uat-template-linux"],
            "networks":    ["uat-net-1"],
            "datastores":  ["uat-ds-1", "uat-ds-2"],
            "vm_name":     ["uat-vm-1", "uat-vm-2"],
        }
    else:
        # fallback 預設
        return {
            "datacenters": ["default-dc"],
            "clusters":    ["default-cluster"],
            "esxi_hosts":  ["default-esxi"],
            "templates":   ["default-template"],
            "networks":    ["default-network"],
            "datastores":  ["default-datastore"],
            "vm_name":     ["default-vm"],
        }

def get_vsphere_objects(host, user, password):
    # 根據 API_MODE 決定是連線真實 vSphere 還是回傳模擬資料
    if current_app.config.get('API_MODE') == 'local':
        print("Running in local mode. Returning mock vSphere data.")
        return _mock_from_host(host)

    # 實際連線邏輯
    context = ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=password, sslContext=context)
    content = si.RetrieveContent()

    def get(view_type):
        return content.viewManager.CreateContainerView(content.rootFolder, [view_type], True).view

    datacenters = []
    clusters = []
    for dc in content.rootFolder.childEntity:
        if isinstance(dc, vim.Datacenter):
            datacenters.append(dc.name)
            for cluster in dc.hostFolder.childEntity:
                if isinstance(cluster, vim.ClusterComputeResource):
                    clusters.append(cluster.name)

    vm_name = [vm.name for vm in get(vim.VirtualMachine) if vm.config and not vm.config.template]
    templates = [vm.name for vm in get(vim.VirtualMachine) if vm.config and vm.config.template]
    networks = [net.name for net in get(vim.Network)]
    datastores = [ds.name for ds in get(vim.Datastore)]
    # 【新增】取得 ESXi 主機列表
    esxi_hosts = [host.name for host in get(vim.HostSystem)]

    Disconnect(si)

    return {
        "datacenters": sorted(datacenters),
        "clusters": sorted(clusters),
        "templates": sorted(templates),
        "networks": sorted(networks),
        "datastores": sorted(datastores),
        "vm_name": sorted(vm_name),
        "esxi_hosts": sorted(esxi_hosts),  # 【新增】將 ESXi 主機列表加入回傳資料
    }

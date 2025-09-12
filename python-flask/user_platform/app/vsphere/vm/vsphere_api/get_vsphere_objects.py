from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
from flask import current_app


# 固定 mapping：不同 host 給不同 mock 選項
def _mock_from_host(host):
    host = (host or "").lower()
    
    # 新增 host_datastores 映射關係
    host_datastores = {
        "sandbox-esxi-01": ["sandbox-ds-1", "sandbox-ds-common"],
        "sandbox-esxi-02": ["sandbox-ds-2", "sandbox-ds-common"],
        "qat-esxi-01": ["qat-ds-1", "qat-ds-2"],
        "uat-esxi-01": ["uat-ds-1"],
        "uat-esxi-02": ["uat-ds-2", "uat-ds-common"],
        "sandbox2-esxi-01": ["sandbox2-ds-1"],
        "sandbox2-esxi-02": ["sandbox2-ds-2", "sandbox2-ds-common"],
        "default-esxi": ["default-datastore"],
    }
    
    if "host.docker.internal:5001" in host:
        return {
            "datacenters": ["sandbox-dc"],
            "clusters": ["sandbox-cluster"],
            "esxi_hosts": ["sandbox-esxi-01", "sandbox-esxi-02"],
            "templates": ["sandbox-template-win", "sandbox-template-linux"],
            "networks": ["sandbox-net-1"],
            "datastores": ["sandbox-ds-1", "sandbox-ds-2", "sandbox-ds-common"],  # 所有可能的 datastore
            "vm_name": ["sandbox-vm-1", "sandbox-vm-2"],
            "host_datastores": host_datastores,  # 新增映射關係
        }
    elif "host.docker.internal:5002" in host:
        return {
            "datacenters": ["qat-dc"],
            "clusters": ["qat-cluster"],
            "esxi_hosts": ["qat-esxi-01"],
            "templates": ["qat-template-win"],
            "networks": ["qat-net-1", "qat-net-2"],
            "datastores": ["qat-ds-1", "qat-ds-2"],
            "vm_name": ["qat-vm-1"],
            "host_datastores": host_datastores,
        }
    elif "host.docker.internal:5003" in host:
        return {
            "datacenters": ["uat-dc"],
            "clusters": ["uat-cluster"],
            "esxi_hosts": ["uat-esxi-01", "uat-esxi-02"],
            "templates": ["uat-template-linux"],
            "networks": ["uat-net-1"],
            "datastores": ["uat-ds-1", "uat-ds-2", "uat-ds-common"],
            "vm_name": ["uat-vm-1", "uat-vm-2"],
            "host_datastores": host_datastores,
        }
    elif "host.docker.internal:5005" in host:
        return {
            "datacenters": ["sandbox2-dc"],
            "clusters": ["sandbox2-cluster"],
            "esxi_hosts": ["sandbox2-esxi-01", "sandbox2-esxi-02"],
            "templates": ["sandbox2-template-win", "sandbox2-template-linux"],
            "networks": ["sandbox2-net-1"],
            "datastores": ["sandbox2-ds-1", "sandbox2-ds-2", "sandbox2-ds-common"],
            "vm_name": ["sandbox2-vm-1", "sandbox2-vm-2"],
            "host_datastores": host_datastores,
        }
    else:
        # fallback 預設
        return {
            "datacenters": ["default-dc"],
            "clusters": ["default-cluster"],
            "esxi_hosts": ["default-esxi"],
            "templates": ["default-template"],
            "networks": ["default-network"],
            "datastores": ["default-datastore"],
            "vm_name": ["default-vm"],
            "host_datastores": host_datastores,
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
    # 取得 ESXi 主機列表
    esxi_hosts = [host.name for host in get(vim.HostSystem)]
    
    # 建立 ESXi 主機到 datastore 的映射
    host_datastores = {}
    for host_system in get(vim.HostSystem):
        host_name = host_system.name
        # 取得該主機可訪問的所有 datastore
        host_datastores[host_name] = [ds.name for ds in host_system.datastore]

    Disconnect(si)

    return {
        "datacenters": sorted(datacenters),
        "clusters": sorted(clusters),
        "templates": sorted(templates),
        "networks": sorted(networks),
        "datastores": sorted(datastores),
        "vm_name": sorted(vm_name),
        "esxi_hosts": sorted(esxi_hosts),
        "host_datastores": host_datastores,  # 新增映射關係
    }
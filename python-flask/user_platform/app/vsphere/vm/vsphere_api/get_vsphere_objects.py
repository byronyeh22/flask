from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
from flask import current_app

def get_vsphere_objects(host, user, password):
    # 根據 API_MODE 決定是連線真實 vSphere 還是回傳模擬資料
    if current_app.config['API_MODE'] == 'local':
        print("Running in local mode. Returning mock vSphere data.")
        return {
            "datacenters": ["mock-dc-1", "mock-dc-2"],
            "clusters": ["mock-cluster-a", "mock-cluster-b"],
            "templates": ["mock-template-win", "mock-template-linux"],
            "networks": ["mock-network-1", "mock-network-2"],
            "datastores": ["mock-datastore-1", "mock-datastore-2"],
            "vm_name": ["mock-vm-1", "mock-vm-2"],
        }

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

    # all_vms = get(vim.VirtualMachine)

    vm_name = [vm.name for vm in get(vim.VirtualMachine) if vm.config and not vm.config.template]
    templates = [vm.name for vm in get(vim.VirtualMachine) if vm.config and vm.config.template]
    networks = [net.name for net in get(vim.Network)]
    datastores = [ds.name for ds in get(vim.Datastore)]

    Disconnect(si)

    return {
        "datacenters": sorted(datacenters),
        "clusters": sorted(clusters),
        "templates": sorted(templates),
        "networks": sorted(networks),
        "datastores": sorted(datastores),
        "vm_name": sorted(vm_name),
    }

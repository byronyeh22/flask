from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl

def get_vsphere_objects(host, user, password):
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

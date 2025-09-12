from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
from flask import current_app


# 固定 mapping：不同 host 給不同 mock 選項
def _mock_from_host(host):
    host = (host or "").lower()

    # 修改後的級聯關係映射 - 每個 datacenter 下有多組 cluster 和 host
    datacenter_clusters = {
        "sandbox-dc": ["sandbox-cluster-1", "sandbox-cluster-2", "sandbox-cluster-3"],
        "qat-dc": ["qat-cluster-1", "qat-cluster-2"],
        "uat-dc": ["uat-cluster-1", "uat-cluster-2", "uat-cluster-3"],
        "sandbox2-dc": ["sandbox2-cluster-1", "sandbox2-cluster-2"],
    }

    cluster_esxi_hosts = {
        # Sandbox datacenter clusters
        "sandbox-cluster-1": ["sandbox-esxi-01", "sandbox-esxi-02", "sandbox-esxi-03"],
        "sandbox-cluster-2": ["sandbox-esxi-04", "sandbox-esxi-05"],
        "sandbox-cluster-3": ["sandbox-esxi-06"],

        # QAT datacenter clusters
        "qat-cluster-1": ["qat-esxi-01", "qat-esxi-02"],
        "qat-cluster-2": ["qat-esxi-03", "qat-esxi-04", "qat-esxi-05"],

        # UAT datacenter clusters
        "uat-cluster-1": ["uat-esxi-01", "uat-esxi-02"],
        "uat-cluster-2": ["uat-esxi-03", "uat-esxi-04"],
        "uat-cluster-3": ["uat-esxi-05", "uat-esxi-06", "uat-esxi-07"],

        # Sandbox2 datacenter clusters
        "sandbox2-cluster-1": ["sandbox2-esxi-01", "sandbox2-esxi-02"],
        "sandbox2-cluster-2": ["sandbox2-esxi-03", "sandbox2-esxi-04", "sandbox2-esxi-05"],
    }

    host_datastores = {
        # Sandbox hosts
        "sandbox-esxi-01": ["sandbox-ds-1", "sandbox-ds-common"],
        "sandbox-esxi-02": ["sandbox-ds-2", "sandbox-ds-common"],
        "sandbox-esxi-03": ["sandbox-ds-3", "sandbox-ds-common"],
        "sandbox-esxi-04": ["sandbox-ds-4", "sandbox-ds-shared"],
        "sandbox-esxi-05": ["sandbox-ds-5", "sandbox-ds-shared"],
        "sandbox-esxi-06": ["sandbox-ds-6"],

        # QAT hosts
        "qat-esxi-01": ["qat-ds-1", "qat-ds-shared"],
        "qat-esxi-02": ["qat-ds-2", "qat-ds-shared"],
        "qat-esxi-03": ["qat-ds-3", "qat-ds-common"],
        "qat-esxi-04": ["qat-ds-4", "qat-ds-common"],
        "qat-esxi-05": ["qat-ds-5", "qat-ds-common"],

        # UAT hosts
        "uat-esxi-01": ["uat-ds-1", "uat-ds-shared"],
        "uat-esxi-02": ["uat-ds-2", "uat-ds-shared"],
        "uat-esxi-03": ["uat-ds-3", "uat-ds-common"],
        "uat-esxi-04": ["uat-ds-4", "uat-ds-common"],
        "uat-esxi-05": ["uat-ds-5", "uat-ds-cluster3"],
        "uat-esxi-06": ["uat-ds-6", "uat-ds-cluster3"],
        "uat-esxi-07": ["uat-ds-7", "uat-ds-cluster3"],

        # Sandbox2 hosts
        "sandbox2-esxi-01": ["sandbox2-ds-1", "sandbox2-ds-common"],
        "sandbox2-esxi-02": ["sandbox2-ds-2", "sandbox2-ds-common"],
        "sandbox2-esxi-03": ["sandbox2-ds-3", "sandbox2-ds-cluster2"],
        "sandbox2-esxi-04": ["sandbox2-ds-4", "sandbox2-ds-cluster2"],
        "sandbox2-esxi-05": ["sandbox2-ds-5", "sandbox2-ds-cluster2"],
    }

    if "host.docker.internal:5001" in host:
        # 更新對應的 lists 包含所有相關項目
        all_sandbox_esxi = []
        all_sandbox_datastores = []
        for cluster in datacenter_clusters["sandbox-dc"]:
            all_sandbox_esxi.extend(cluster_esxi_hosts[cluster])
        for host_name in all_sandbox_esxi:
            all_sandbox_datastores.extend(host_datastores[host_name])

        return {
            "datacenters": ["sandbox-dc"],
            "clusters": datacenter_clusters["sandbox-dc"],
            "esxi_hosts": all_sandbox_esxi,
            "templates": ["sandbox-template-win", "sandbox-template-linux"],
            "networks": ["sandbox-net-1", "sandbox-net-2"],
            "datastores": list(set(all_sandbox_datastores)),  # 去重
            "vm_name": ["sandbox-vm-1", "sandbox-vm-2"],
            "datacenter_clusters": datacenter_clusters,
            "cluster_esxi_hosts": cluster_esxi_hosts,
            "host_datastores": host_datastores,
        }
    elif "host.docker.internal:5002" in host:
        all_qat_esxi = []
        all_qat_datastores = []
        for cluster in datacenter_clusters["qat-dc"]:
            all_qat_esxi.extend(cluster_esxi_hosts[cluster])
        for host_name in all_qat_esxi:
            all_qat_datastores.extend(host_datastores[host_name])

        return {
            "datacenters": ["qat-dc"],
            "clusters": datacenter_clusters["qat-dc"],
            "esxi_hosts": all_qat_esxi,
            "templates": ["qat-template-win", "qat-template-linux"],
            "networks": ["qat-net-1", "qat-net-2", "qat-net-3"],
            "datastores": list(set(all_qat_datastores)),
            "vm_name": ["qat-vm-1", "qat-vm-2"],
            "datacenter_clusters": datacenter_clusters,
            "cluster_esxi_hosts": cluster_esxi_hosts,
            "host_datastores": host_datastores,
        }
    elif "host.docker.internal:5003" in host:
        all_uat_esxi = []
        all_uat_datastores = []
        for cluster in datacenter_clusters["uat-dc"]:
            all_uat_esxi.extend(cluster_esxi_hosts[cluster])
        for host_name in all_uat_esxi:
            all_uat_datastores.extend(host_datastores[host_name])

        return {
            "datacenters": ["uat-dc"],
            "clusters": datacenter_clusters["uat-dc"],
            "esxi_hosts": all_uat_esxi,
            "templates": ["uat-template-linux", "uat-template-win"],
            "networks": ["uat-net-1", "uat-net-2"],
            "datastores": list(set(all_uat_datastores)),
            "vm_name": ["uat-vm-1", "uat-vm-2", "uat-vm-3"],
            "datacenter_clusters": datacenter_clusters,
            "cluster_esxi_hosts": cluster_esxi_hosts,
            "host_datastores": host_datastores,
        }
    elif "host.docker.internal:5005" in host:
        all_sandbox2_esxi = []
        all_sandbox2_datastores = []
        for cluster in datacenter_clusters["sandbox2-dc"]:
            all_sandbox2_esxi.extend(cluster_esxi_hosts[cluster])
        for host_name in all_sandbox2_esxi:
            all_sandbox2_datastores.extend(host_datastores[host_name])

        return {
            "datacenters": ["sandbox2-dc"],
            "clusters": datacenter_clusters["sandbox2-dc"],
            "esxi_hosts": all_sandbox2_esxi,
            "templates": ["sandbox2-template-win", "sandbox2-template-linux"],
            "networks": ["sandbox2-net-1"],
            "datastores": list(set(all_sandbox2_datastores)),
            "vm_name": ["sandbox2-vm-1", "sandbox2-vm-2"],
            "datacenter_clusters": datacenter_clusters,
            "cluster_esxi_hosts": cluster_esxi_hosts,
            "host_datastores": host_datastores,
        }
    else:
        # fallback 預設
        default_datacenter_clusters = {"default-dc": ["default-cluster"]}
        default_cluster_esxi_hosts = {"default-cluster": ["default-esxi"]}
        default_host_datastores = {"default-esxi": ["default-datastore"]}

        return {
            "datacenters": ["default-dc"],
            "clusters": ["default-cluster"],
            "esxi_hosts": ["default-esxi"],
            "templates": ["default-template"],
            "networks": ["default-network"],
            "datastores": ["default-datastore"],
            "vm_name": ["default-vm"],
            "datacenter_clusters": default_datacenter_clusters,
            "cluster_esxi_hosts": default_cluster_esxi_hosts,
            "host_datastores": default_host_datastores,
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
    datacenter_clusters = {}
    cluster_esxi_hosts = {}
    host_datastores = {}

    # 遍歷所有 Datacenter
    for dc in content.rootFolder.childEntity:
        if isinstance(dc, vim.Datacenter):
            datacenter_name = dc.name
            datacenters.append(datacenter_name)
            datacenter_clusters[datacenter_name] = []

            # 遍歷 Datacenter 下的 Cluster
            for cluster in dc.hostFolder.childEntity:
                if isinstance(cluster, vim.ClusterComputeResource):
                    cluster_name = cluster.name
                    clusters.append(cluster_name)
                    datacenter_clusters[datacenter_name].append(cluster_name)
                    cluster_esxi_hosts[cluster_name] = []

                    # 遍歷 Cluster 下的 ESXi Host
                    for host_system in cluster.host:
                        host_name = host_system.name
                        cluster_esxi_hosts[cluster_name].append(host_name)

                        # 建立 ESXi Host 到 Datastore 的映射
                        host_datastores[host_name] = [ds.name for ds in host_system.datastore]

    # 取得其他資源
    vm_name = [vm.name for vm in get(vim.VirtualMachine) if vm.config and not vm.config.template]
    templates = [vm.name for vm in get(vim.VirtualMachine) if vm.config and vm.config.template]
    networks = [net.name for net in get(vim.Network)]
    datastores = [ds.name for ds in get(vim.Datastore)]
    esxi_hosts = [host.name for host in get(vim.HostSystem)]

    Disconnect(si)

    return {
        "datacenters": sorted(datacenters),
        "clusters": sorted(clusters),
        "templates": sorted(templates),
        "networks": sorted(networks),
        "datastores": sorted(datastores),
        "vm_name": sorted(vm_name),
        "esxi_hosts": sorted(esxi_hosts),
        "datacenter_clusters": datacenter_clusters,
        "cluster_esxi_hosts": cluster_esxi_hosts,
        "host_datastores": host_datastores,
    }
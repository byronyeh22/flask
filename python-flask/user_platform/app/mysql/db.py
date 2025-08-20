import mysql.connector

DB_CONFIG = {
    "host": "172.26.1.176",
    # "host": "localhost",
    "user": "root",
    "password": "rootpassword",
    "database": "user_platform"
}

def init_db():
    # 建立 DB
    conn = mysql.connector.connect(
        host=DB_CONFIG['host'],
        user=DB_CONFIG['user'],
        password=DB_CONFIG['password']
    )
    cursor = conn.cursor()
    cursor.execute("CREATE DATABASE IF NOT EXISTS user_platform")
    cursor.close()
    conn.close()

    # 重新連線到 DB
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # workflow_runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            workflow_id INT AUTO_INCREMENT PRIMARY KEY,
            triggered_by VARCHAR(100),
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'PENDING_APPROVAL',
            failed_message TEXT,
            request_payload TEXT NULL
        )
    """)

    # jira_tickets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jira_tickets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            workflow_id INT,
            ticket_id VARCHAR(50),
            project_key VARCHAR(50),
            summary TEXT,
            description TEXT,
            status VARCHAR(50),
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE
        )
    """)

    # gitlab_pipelines
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gitlab_pipelines (
            id INT AUTO_INCREMENT PRIMARY KEY,
            workflow_id INT,
            pipeline_id VARCHAR(100),
            job_id VARCHAR(100),
            project_name VARCHAR(255),
            branch VARCHAR(255),
            commit_sha VARCHAR(100),
            status VARCHAR(50),
            triggered_by VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP NULL,
            duration INT,
            web_url TEXT,

            # 紀錄 VM Configuration Parameters（來自環境變數）
            action_type VARCHAR(50),
            environment VARCHAR(50),
            resource VARCHAR(50),
            os_type VARCHAR(50),
            vsphere_datacenter VARCHAR(100),
            vsphere_cluster VARCHAR(100),
            vsphere_network VARCHAR(100),
            vsphere_template VARCHAR(100),
            vsphere_datastore VARCHAR(100),
            vm_name_prefix VARCHAR(100),
            vm_instance_type VARCHAR(50),
            vm_num_cpus INT,
            vm_memory INT,
            vm_additional_disks_json TEXT,
            vm_ipv4_gateway VARCHAR(50),
            netbox_prefix VARCHAR(100),
            netbox_tenant VARCHAR(100),
            FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE
        )
    """)

    # vm_configurations：新增 scsi_controller_count（預設 1）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vm_configurations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            environment VARCHAR(100) NOT NULL,
            resource VARCHAR(50) NOT NULL,
            os_type VARCHAR(50) NOT NULL,
            vsphere_datacenter VARCHAR(100),
            vsphere_cluster VARCHAR(100),
            vsphere_network VARCHAR(100),
            vsphere_template VARCHAR(200),
            vsphere_datastore VARCHAR(200),
            vm_name_prefix VARCHAR(200) NOT NULL,
            vm_instance_type VARCHAR(100),
            vm_num_cpus INT NOT NULL DEFAULT 1,
            vm_memory INT NOT NULL DEFAULT 1024,
            vm_scsi_controller_count TINYINT NOT NULL DEFAULT 1,
            vm_ipv4_ip VARCHAR(64),
            vm_ipv4_gateway VARCHAR(64),
            netbox_prefix VARCHAR(64),
            netbox_tenant VARCHAR(64),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_vm (environment, vm_name_prefix)
        )
    """)

    # vm_disks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vm_disks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            vm_configuration_id INT NOT NULL,

            -- 用來穩定對應 vSphere 的 SCSI 位址
            scsi_controller TINYINT NOT NULL DEFAULT 0,  -- 0..3
            unit_number INT NOT NULL,                   -- 0..6, 8..15

            -- 給 UI 顯示的 Hard disk N（連號、可變動）
            ui_disk_number INT NULL,

            size INT NOT NULL,
            disk_provisioning VARCHAR(50) NOT NULL,
            status VARCHAR(50) DEFAULT 'PENDING_CREATION',
            vmdk_path VARCHAR(255) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,

            -- 同一台 VM 上 (bus, unit) 必須唯一
            CONSTRAINT uq_vm_scsi UNIQUE (vm_configuration_id, scsi_controller, unit_number),
            FOREIGN KEY (vm_configuration_id) REFERENCES vm_configurations(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

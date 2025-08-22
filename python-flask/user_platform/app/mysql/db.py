import mysql.connector
from flask import current_app # 導入 current_app

def init_db():
    """
    Initialize the MySQL database and ensure all required tables exist.
    """
    db_config = {
        "host": current_app.config['DB_HOST'],
        "user": current_app.config['DB_USER'],
        "password": current_app.config['DB_PASSWORD'],
        "database": current_app.config['DB_NAME']
    }

    # 建立資料庫（若不存在）
    conn = mysql.connector.connect(
        host=db_config["host"],
        user=db_config["user"],
        password=db_config["password"]
    )
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_config['database']}")
    cursor.close()
    conn.close()

    # 連線至目標資料庫
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()

    # workflow_runs：紀錄請求生命週期
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            workflow_id INT AUTO_INCREMENT PRIMARY KEY,
            created_by VARCHAR(100) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_by VARCHAR(100) NULL,
            approved_at TIMESTAMP NULL,
            updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
            cancelled_by  VARCHAR(100) NULL,
            cancelled_at  TIMESTAMP NULL,
            status ENUM('DRAFT','PENDING_APPROVAL','RETURNED','IN_PROGRESS','SUCCESS','FAILED','CANCELLED')
                DEFAULT 'DRAFT',
            failed_message TEXT,
            request_payload JSON NULL,
            INDEX idx_status (status),
            INDEX idx_created_by (created_by),
            INDEX idx_approved_by (approved_by),
            INDEX idx_cancelled_by (cancelled_by)
        )
    """)

    # jira_tickets：儲存對應 Jira Ticket 資訊
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

    # gitlab_pipelines：儲存 GitLab pipeline 執行資訊
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
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP NULL,
            duration INT,
            web_url TEXT,
            FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE
        )
    """)

    # vm_configurations：VM 基本設定
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

    # vm_disks：紀錄 VM 硬碟資訊
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vm_disks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            vm_configuration_id INT NOT NULL,
            -- 穩定對應 vSphere 的 SCSI 位址
            scsi_controller TINYINT NOT NULL DEFAULT 0,  -- 0..3
            unit_number INT NOT NULL,                    -- 0..6, 8..15
            -- UI 顯示用 Hard Disk N（可變動連號）
            ui_disk_number INT NULL,
            size INT NOT NULL,
            disk_provisioning VARCHAR(50) NOT NULL,
            status VARCHAR(50) DEFAULT 'PENDING_CREATION',
            vmdk_path VARCHAR(255) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
            CONSTRAINT uq_vm_scsi UNIQUE (vm_configuration_id, scsi_controller, unit_number),
            FOREIGN KEY (vm_configuration_id) REFERENCES vm_configurations(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()

def get_db_connection():
    """Create and return a new database connection using DB_CONFIG."""
    # 修正: 將 DB_CONFIG 的定義移入函式內部
    db_config = {
        "host": current_app.config['DB_HOST'],
        "user": current_app.config['DB_USER'],
        "password": current_app.config['DB_PASSWORD'],
        "database": current_app.config['DB_NAME']
    }
    return mysql.connector.connect(**db_config)
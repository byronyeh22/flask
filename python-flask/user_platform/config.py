import os

class Config:
    # --- API Mode Control ---
    # 可用的模式: 'dev' (開發/正式), 'local' (本地模擬)
    # 預設為 'dev'
    API_MODE = os.environ.get("API_MODE", "local")

    # --- Encryption Key ---
    # !! IMPORTANT !!
    # This key MUST be provided as an environment variable.
    # The application will fail to start if it is not set.
    FERNET_KEY = os.environ.get("FERNET_KEY")
    if not FERNET_KEY:
        raise ValueError("No FERNET_KEY set for Flask application. Please set it as an environment variable on docker-compose.yml file.")


# --- 動態設定，根據 API_MODE 的值載入不同的連線資訊 ---

if Config.API_MODE == 'dev':
    # 開發環境 (Dev/Production) 連線設定
    # 這裡的變數使用 os.environ.get()，允許透過環境變數覆寫

    # --- Database Configuration ---
    Config.DB_HOST = os.environ.get("DB_HOST", "172.26.1.176")
    Config.DB_USER = os.environ.get("DB_USER", "root")
    Config.DB_PASSWORD = os.environ.get("DB_PASSWORD", "rootpassword")
    Config.DB_NAME = os.environ.get("DB_NAME", "user_platform")

    # --- vSphere Configuration ---
    Config.VSPHERE_HOST = os.environ.get("VSPHERE_HOST", "172.26.1.60")
    Config.VSPHERE_USER = os.environ.get("VSPHERE_USER", "administrator@vsphere.local")
    Config.VSPHERE_PASSWORD = os.environ.get("VSPHERE_PASSWORD", "Gict@1688+")

    # --- GitLab Configuration ---
    Config.GITLAB_URL = os.environ.get("GITLAB_URL", "http://172.26.1.176:8080")
    Config.GITLAB_PRIVATE_TOKEN = os.environ.get("GITLAB_PRIVATE_TOKEN", "glpat-L8_6TMifNGL6uby92h_f")
    Config.GITLAB_TRIGGER_TOKEN = os.environ.get("GITLAB_TRIGGER_TOKEN", "glptt-71bf6f53fecc5a5234ba177ae8bdbb53fe973db3")
    Config.GITLAB_PROJECT_ID = os.environ.get("GITLAB_PROJECT_ID", "15")
    Config.GITLAB_BRANCH = os.environ.get("GITLAB_BRANCH", "main")

    # --- Jira Configuration ---
    Config.JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://sanbox888twuat.atlassian.net")
    Config.JIRA_USER = os.environ.get("JIRA_USER", "srv.sra@sanbox888.tw")
    Config.JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "ATATT3xFfGF0R9x--AYy2vPdYkG25_w52yHTrGG4wGfBwMbnsyxDMoFmSPL54MtfecWNeoLQR2_0hY73MhBh0m1njA057j8b-9qdFX4TPVlngRu9mkYq1p9TVdXei1_a0FcSt_GgaK2Ae7f8fU8v-PiDSfljnMr63Ce1TuiFApMSdxeFih-_WUE=346474B9")
    Config.JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SJT")

else: #Config.API_MODE == 'local'
    # 本地模擬環境 (Mock API) 連線設定
    # 這裡的設定通常是固定值，不需要從環境變數讀取

    # --- Database Configuration ---
    Config.DB_HOST = "my_mysql"
    Config.DB_USER = "root"
    Config.DB_PASSWORD = "rootpassword"
    Config.DB_NAME = "user_platform"

    # --- vSphere Configuration ---
    Config.VSPHERE_HOST = "http://host.docker.internal:5001"
    Config.VSPHERE_USER = "mock_user"
    Config.VSPHERE_PASSWORD = "mock_password"

    # --- GitLab Configuration ---
    Config.GITLAB_URL = "http://host.docker.internal:5001/mock/gitlab"
    Config.GITLAB_PRIVATE_TOKEN = "mock-token"
    Config.GITLAB_TRIGGER_TOKEN = "mock-token"
    Config.GITLAB_PROJECT_ID = "15"
    Config.GITLAB_BRANCH = "main"

    # --- Jira Configuration ---
    # Config.JIRA_BASE_URL = "http://127.0.0.1:5001/mock/jira"
    # Config.JIRA_USER = "mock_user"
    # Config.JIRA_API_TOKEN = "mock_token"
    # Config.JIRA_PROJECT_KEY = "MOCK"

    Config.JIRA_BASE_URL = "https://byronyeh22.atlassian.net"
    Config.JIRA_USER = "byron.yeh22@gmail.com"
    Config.JIRA_API_TOKEN = "ATATT3xFfGF0hMkSGSDBMlM13zQ8hN2eVBU8wvdT83V4Y65SD8414qm0ueKYE90HtkOb672dWdjPfCs34zW1DGq-ksedSjqZDaL04Vd3DsypHz1aPYZg9FXBl0ChMO9C_Mm4etjQI0mgPdFFYGCKKuxT-NvhGG-UE6bTEeGP-jsjfLheQAH6B6s=B38A967D"
    Config.JIRA_PROJECT_KEY = "SJT"

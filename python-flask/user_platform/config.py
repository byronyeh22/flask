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
    # --- 開發/正式環境：讀取 DEV_ 前綴的變數 ---

    # --- Database Configuration ---
    Config.DB_HOST = os.getenv("DEV_DB_HOST")
    Config.DB_USER = os.getenv("DEV_DB_USER")
    Config.DB_PASSWORD = os.getenv("DEV_DB_PASSWORD")
    Config.DB_NAME = os.getenv("DEV_DB_NAME")

    # --- vSphere Configuration ---
    Config.VSPHERE_HOST = os.getenv("DEV_VSPHERE_HOST")
    Config.VSPHERE_USER = os.getenv("DEV_VSPHERE_USER")
    Config.VSPHERE_PASSWORD = os.getenv("DEV_VSPHERE_PASSWORD")

    # --- GitLab Configuration ---
    Config.GITLAB_URL = os.getenv("DEV_GITLAB_URL")
    Config.GITLAB_PRIVATE_TOKEN = os.getenv("DEV_GITLAB_PRIVATE_TOKEN")
    Config.GITLAB_TRIGGER_TOKEN = os.getenv("DEV_GITLAB_TRIGGER_TOKEN")
    Config.GITLAB_PROJECT_ID = os.getenv("DEV_GITLAB_PROJECT_ID")
    Config.GITLAB_BRANCH = os.getenv("DEV_GITLAB_BRANCH")

    # --- Jira Configuration ---
    Config.JIRA_BASE_URL = os.getenv("DEV_JIRA_BASE_URL")
    Config.JIRA_USER = os.getenv("DEV_JIRA_USER")
    Config.JIRA_API_TOKEN = os.getenv("DEV_JIRA_API_TOKEN")
    Config.JIRA_PROJECT_KEY = os.getenv("DEV_JIRA_PROJECT_KEY")

else: # Config.API_MODE == 'local'
    # --- 本地模擬環境 (Mock API)：讀取 LOCAL_ 前綴的變數 ---

    # --- Database Configuration ---
    Config.DB_HOST = os.getenv("LOCAL_DB_HOST")
    Config.DB_USER = os.getenv("LOCAL_DB_USER")
    Config.DB_PASSWORD = os.getenv("LOCAL_DB_PASSWORD")
    Config.DB_NAME = os.getenv("LOCAL_DB_NAME")

    # --- vSphere Configuration ---
    Config.VSPHERE_HOST = os.getenv("LOCAL_VSPHERE_HOST")
    Config.VSPHERE_USER = os.getenv("LOCAL_VSPHERE_USER")
    Config.VSPHERE_PASSWORD = os.getenv("LOCAL_VSPHERE_PASSWORD")

    # --- GitLab Configuration ---
    Config.GITLAB_URL = os.getenv("LOCAL_GITLAB_URL")
    Config.GITLAB_PRIVATE_TOKEN = os.getenv("LOCAL_GITLAB_PRIVATE_TOKEN")
    Config.GITLAB_TRIGGER_TOKEN = os.getenv("LOCAL_GITLAB_TRIGGER_TOKEN")
    Config.GITLAB_PROJECT_ID = os.getenv("LOCAL_GITLAB_PROJECT_ID")
    Config.GITLAB_BRANCH = os.getenv("LOCAL_GITLAB_BRANCH")

    # --- Jira Configuration ---
    Config.JIRA_BASE_URL = os.getenv("LOCAL_JIRA_BASE_URL")
    Config.JIRA_USER = os.getenv("LOCAL_JIRA_USER")
    Config.JIRA_API_TOKEN = os.getenv("LOCAL_JIRA_API_TOKEN")
    Config.JIRA_PROJECT_KEY = os.getenv("LOCAL_JIRA_PROJECT_KEY")


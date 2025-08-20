import os

class Config:
    # DB
    # DB_HOST = os.environ.get("DB_HOST", "172.26.1.176")
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "rootpassword")
    DB_NAME = os.environ.get("DB_NAME", "user_platform")

# python/app/db/mysql.py
import os
import mysql.connector

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "172.26.1.176"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", "rootpassword"),
    "database": os.getenv("DB_NAME", "forti_db"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "connection_timeout": int(os.getenv("DB_CONN_TIMEOUT", "10")),
    # 需要連線池可後續改為 mysql.connector.pooling.MySQLConnectionPool
}

def get_db_connection():
    return mysql.connector.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        port=DB_CONFIG["port"],
        connection_timeout=DB_CONFIG["connection_timeout"],
    )


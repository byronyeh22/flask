from __future__ import annotations
from typing import Any, Dict, List, Optional
import time
import random

import mysql.connector
from mysql.connector import errors as db_errors

from app.db.mysql import get_db_connection


def _set_session_pragmas(cur) -> None:
    """
    降低被 gap lock 影響的機率、縮短鎖等待時間。
    沒有權限也沒關係，失敗就忽略。
    """
    try:
        cur.execute("SET SESSION innodb_lock_wait_timeout = 5")
    except Exception:
        pass
    try:
        # READ COMMITTED 可減少 Next-Key/GAP locks（外鍵/唯一檢查除外）
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    except Exception:
        pass


def list_devices() -> List[Dict[str, Any]]:
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, name, host, port, is_active, verify_ssl, created_at
            FROM forti_devices
            ORDER BY id DESC
        """)
        rows = cur.fetchall()
        return rows
    finally:
        cur.close(); conn.close()


def get_device(device_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, name, host, port, api_token, is_active, verify_ssl, created_at
            FROM forti_devices WHERE id=%s
        """, (device_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def _insert_device_once(name: str, host: str, port: int, api_token: str,
                        is_active: int = 1, verify_ssl: int = 1) -> int:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        _set_session_pragmas(cur)
        cur.execute("""
            INSERT INTO forti_devices (name, host, port, api_token, is_active, verify_ssl)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (name, host, port, api_token, is_active, verify_ssl))
        new_id = cur.lastrowid
        conn.commit()
        return int(new_id)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close(); conn.close()


def create_device(name: str, host: str, port: int, api_token: str,
                  is_active: int = 1, verify_ssl: int = 1) -> int:
    """
    對常見的 1205（lock wait timeout）與 1213（deadlock）做重試（指數退避）。
    """
    retries = 2  # 總共嘗試 1+retries 次
    delay = 0.2
    for attempt in range(retries + 1):
        try:
            return _insert_device_once(name, host, port, api_token, is_active, verify_ssl)
        except db_errors.DatabaseError as e:
            if getattr(e, "errno", None) in (1205, 1213) and attempt < retries:
                time.sleep(delay + random.random() * 0.2)
                delay *= 2
                continue
            # 其他錯誤或已用盡重試，拋出
            raise


def update_device(device_id: int, name: str, host: str, port: int,
                  is_active: int, verify_ssl: int,
                  api_token: Optional[str] = None) -> None:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        _set_session_pragmas(cur)
        if api_token is not None and api_token != "":
            cur.execute("""
                UPDATE forti_devices
                SET name=%s, host=%s, port=%s, is_active=%s, verify_ssl=%s, api_token=%s
                WHERE id=%s
            """, (name, host, port, is_active, verify_ssl, api_token, device_id))
        else:
            cur.execute("""
                UPDATE forti_devices
                SET name=%s, host=%s, port=%s, is_active=%s, verify_ssl=%s
                WHERE id=%s
            """, (name, host, port, is_active, verify_ssl, device_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close(); conn.close()


def delete_device(device_id: int) -> None:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        _set_session_pragmas(cur)
        cur.execute("DELETE FROM forti_devices WHERE id=%s", (device_id,))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close(); conn.close()


def list_device_vdoms(device_id: int) -> List[str]:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT vdom FROM forti_device_vdoms
            WHERE device_id=%s AND is_active=1
            ORDER BY vdom ASC
        """, (device_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()


def upsert_device_vdoms(device_id: int, vdoms: List[str]) -> None:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        _set_session_pragmas(cur)
        cur.execute("DELETE FROM forti_device_vdoms WHERE device_id=%s", (device_id,))
        for v in vdoms:
            v = (v or "").strip()
            if not v:
                continue
            cur.execute("""
                INSERT INTO forti_device_vdoms (device_id, vdom, is_active)
                VALUES (%s, %s, 1)
            """, (device_id, v))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close(); conn.close()


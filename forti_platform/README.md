# Platform Template

一個基於 **Flask** 的後台管理平台模板，提供使用者驗證、權限控制、側邊欄動態渲染、資料庫連線與模板繼承等功能，適合作為各類管理系統的開發基礎。

---

## 功能特色

- **使用者登入 / 登出**
  - 基於 Session 驗證
  - 密碼加密與驗證
- **權限與裝飾器控制**
  - 登入驗證 (`@login_required`)
  - 權限驗證 (`@permission_required`)
- **動態側邊欄**
  - 從資料庫讀取側邊欄結構
  - 依使用者角色與權限顯示對應選單
- **統一模板結構**
  - `base.html` 提供全域佈局
  - `404.html` 自訂錯誤頁
- **資料庫連線管理**
  - MySQL 連線封裝 (`app/db/mysql.py`)
- **可擴展模組化架構**
  - 各功能以 Blueprint 分離
  - 可獨立新增模組與模板

---

## 專案結構
```bash
forti_platform/
│
├── platform_template.sql # 初始資料庫 SQL 腳本
└── python/
├── run.py # Flask 啟動檔
└── app/
├── init.py # 建立 Flask App
├── auth/ # 認證模組
├── db/ # 資料庫連線
├── decorators/ # 權限與登入檢查
├── main/ # 主頁模組
├── sidebar/ # 側邊欄模組
├── templates/ # 全域模板
└── utils/ # 工具函式
```
---

## 系統需求

- Python 3.11+
- MySQL 5.7+ / MariaDB 10.4+
- pip 套件：
```bash
flask
pymysql
bcrypt
mysql-connector-python
```
---

## 初始化資料庫
```bash
mysql -u root -p platform_template < ../platform_template.sql
```

## 開發建議
```bash
新增模組時，在 app/ 內建立資料夾
- __init__.py
- routes.py
- templates/
- db/（如需）
註冊 Blueprint 至 app/__init__.py
透過 app/decorators/decorators.py 控制權限
```
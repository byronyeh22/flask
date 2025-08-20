import time
import threading
from app.mysql.db import get_db_connection
from app.vsphere.vm.db.update_gitlab_pipeline_details import update_gitlab_pipeline_details
from app.vsphere.vm.gitlab_api.get_pipeline_status_from_gitlab import get_pipeline_status_from_gitlab

def monitor_pipelines():
    while True:
        print("\n🚀 開始 GitLab pipeline 監控...")

        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        try:
            cursor.execute("""
                SELECT pipeline_id 
                FROM gitlab_pipelines 
                WHERE status NOT IN ('success', 'failed', 'canceled') 
                  AND created_at >= NOW() - INTERVAL 1 DAY
            """)
            pipelines = cursor.fetchall()

            for pipeline in pipelines:
                pipeline_id = pipeline["pipeline_id"]
                print(f"🔍 檢查 pipeline {pipeline_id}")
                gitlab_result = get_pipeline_status_from_gitlab(pipeline_id)

                if gitlab_result["success"]:
                    update_gitlab_pipeline_details(db_conn, pipeline_id, gitlab_result)
                else:
                    print(f"⚠️ Pipeline {pipeline_id} 查詢失敗: {gitlab_result['error']}")

        except Exception as e:
            print(f"❌ Pipeline 監控錯誤: {e}")

        finally:
            cursor.close()
            db_conn.close()

        time.sleep(60)  # 等待下一輪查詢


def start_monitor_thread():
    thread = threading.Thread(target=monitor_pipelines, daemon=True)
    thread.start()
    print("✅ Pipeline Monitor Thread 已啟動")

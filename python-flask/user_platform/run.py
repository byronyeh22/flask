import sys
import os

# Add the current directory to the Python path so that local modules can be imported
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Import the Flask application factory function
from app import create_app

# Create the Flask app instance using the factory pattern
app = create_app()

if __name__ == "__main__":
    # app.run(host="0.0.0.0", port=16888, debug=True, use_reloader=True)
	app.run(
	    host="0.0.0.0",
	    port=16888,
	    debug=False,         # 顯示帶 console 的錯誤頁（Werkzeug debugger）、更詳細的日誌
	    use_reloader=True   # 監看程式檔案變動，自動重啟（關閉可避免雙進程/重複 thread）
	)
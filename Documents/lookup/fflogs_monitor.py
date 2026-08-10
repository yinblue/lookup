import http.server
import json
import os
import socketserver
import threading
import time
from datetime import datetime, timezone
import requests

# ============================================================
# 1. 從環境變數讀取機密資訊 (避免推送到 GitHub 外露)
# ============================================================
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

TARGET_CHARACTERS = [
    {"name": "Puyeee Afrie", "server": "Bahamut", "region": "JP"},
    {"name": "Orca Delphinidae", "server": "Bahamut", "region": "JP"},
    {"name": "Shalem Silveria", "server": "Bahamut", "region": "JP"},
]

CHECK_INTERVAL_SECONDS = 300
SEEN_REPORTS_FILE = "seen_reports.json"
API_URL = "https://www.fflogs.com/api/v2/client"
TOKEN_URL = "https://www.fflogs.com/oauth/token"


# ============================================================
# 防 Render 免費層休眠的微型 HTTP 服務 (Dummy Server)
# ============================================================
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))

    class SimpleHandler(http.server.SimpleHTTPRequestHandler):

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"FF Logs Monitor is Running!")

        def log_message(self, format, *args):
            return  # 關閉 HTTP 請求日誌以保持 Log 乾淨

    with socketserver.TCPServer(("", port), SimpleHandler) as httpd:
        print(f"Dummy HTTP Server listening on port {port}")
        httpd.serve_forever()


# --- (中間保留原本的 FFLogsMonitor 與 check_updates 類別和邏輯) ---


def main():
    # 啟動背景 Dummy HTTP 伺服器給 Render 檢查存活
    threading.Thread(target=run_dummy_server, daemon=True).start()

    monitor = FFLogsMonitor()
    print("============================================")
    print(" FF Logs 常駐監控服務已於 Render 啟動")
    print("============================================")

    while True:
        try:
            check_updates(monitor)
        except Exception as e:
            print(f"❌ 執行過程發生未預期例外: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

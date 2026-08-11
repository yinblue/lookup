import http.server
import json
import os
import socketserver
import threading
import time
from datetime import datetime, timezone
import requests

# ============================================================
# 1. 從環境變數讀取機密資訊 (在 Render 後台設定)
# ============================================================
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 監控角色清單
TARGET_CHARACTERS = [
    {"name": "Puyeee Afrie", "server": "Bahamut", "region": "JP"},
    {"name": "Orca Delphinidae", "server": "Bahamut", "region": "JP"},
    {"name": "Oha Epocan", "server": "Atomos", "region": "JP"},
    {"name": "Ayase Yuina", "server": "Typhon", "region": "JP"},
]

# ⚡【手動補發區】已填入需要補發通知的 Log Code
FORCE_CODES = []

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
            return

    with socketserver.TCPServer(("", port), SimpleHandler) as httpd:
        print(f"Dummy HTTP Server listening on port {port}")
        httpd.serve_forever()


# ============================================================
# 2. OAuth Token 與狀態管理
# ============================================================
class FFLogsMonitor:

    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0
        self.seen_reports = self.load_seen_reports()

    def get_token(self):
        now = time.time()
        if self.access_token and now < self.token_expires_at - 60:
            return self.access_token

        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 刷新 OAuth Token..."
        )
        r = requests.post(
            TOKEN_URL,
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self.access_token = data["access_token"]
        self.token_expires_at = now + data.get("expires_in", 86400)
        return self.access_token

    def graphql(self, query, variables=None):
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        for attempt in range(3):
            try:
                r = requests.post(
                    API_URL,
                    headers=headers,
                    json={"query": query, "variables": variables or {}},
                    timeout=30,
                )
                if r.status_code == 200:
                    return r.json()
                elif r.status_code in [429, 500, 502, 503, 504]:
                    time.sleep(2)
                else:
                    r.raise_for_status()
            except requests.exceptions.RequestException:
                time.sleep(2)
        return None

    def load_seen_reports(self):
        if os.path.exists(SEEN_REPORTS_FILE):
            try:
                with open(SEEN_REPORTS_FILE, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def save_seen_reports(self):
        with open(SEEN_REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(self.seen_reports), f, indent=2)

    def send_discord_notify(self, title, description, url):
        if not DISCORD_WEBHOOK_URL:
            return
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "url": url,
                    "color": 3447003,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        try:
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        except Exception as e:
            print(f"Discord 通知發送失敗: {e}")


# ============================================================
# 3. 核心監控邏輯
# ============================================================
QUERY_RECENT_REPORTS = """
query ($name: String!, $serverRegion: String!, $serverSlug: String!) {
    characterData {
        character(name: $name, serverRegion: $serverRegion, serverSlug: $serverSlug) {
            name
            recentReports(limit: 10) {
                data {
                    code
                    startTime
                    title
                    owner { name }
                }
            }
        }
    }
}
"""

QUERY_REPORT_DETAILS = """
query ($code: String!) {
    reportData {
        report(code: $code) {
            code
            title
            owner { name }
            fights {
                id
                name
                encounterID
                startTime
                endTime
                fightPercentage
                bossPercentage
                kill
            }
        }
    }
}
"""


def check_forced_reports(monitor):
    """手動補發指定 Code 的 Log 通知"""
    if not FORCE_CODES:
        return

    print(f"\n⚡ 偵測到手動補發清單，準備發送通知...")
    for code in FORCE_CODES:
        details = monitor.graphql(QUERY_REPORT_DETAILS, {"code": code})
        report_data = (
            details.get("data", {}).get("reportData", {}).get("report")
            if details
            else None
        )

        if report_data:
            fights = report_data.get("fights", []) or []
            title = report_data.get("title", "無標題")
            uploader = report_data.get("owner", {}).get("name", "未知")

            report_url = f"https://www.fflogs.com/reports/{code}"
            msg = f"**【手動補發通知】**\n**上傳者:** {uploader}\n**包含 Pull 數:** {len(fights)}"
            monitor.send_discord_notify(
                f"📊 補發 Log 通知：{title}", msg, report_url
            )
            print(f"✅ 已成功補發 Code: {code} ({title}) 的 Discord 通知！")

            monitor.seen_reports.add(code)
            monitor.save_seen_reports()


def init_cold_start(monitor):
    """冷啟動載入現有歷史 Log"""
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 進行冷啟動初始化：載入現有歷史 Log..."
    )
    count = 0
    for char in TARGET_CHARACTERS:
        res = monitor.graphql(
            QUERY_RECENT_REPORTS,
            {
                "name": char["name"],
                "serverRegion": char["region"],
                "serverSlug": char["server"],
            },
        )

        if not res or "data" not in res:
            continue

        character_obj = res["data"].get("characterData", {}).get("character")
        if not character_obj:
            continue

        reports = character_obj.get("recentReports", {}).get("data", []) or []
        for r in reports:
            code = r["code"]
            if code in FORCE_CODES:
                continue
            if code not in monitor.seen_reports:
                monitor.seen_reports.add(code)
                count += 1

    monitor.save_seen_reports()
    print(
        f"✅ 冷啟動完成！已將 {count} 份現有 Log 預先標記為已讀，防止重複洗版通知。\n"
    )


def check_updates(monitor):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 掃描新 Log 中..."
    )

    for char in TARGET_CHARACTERS:
        res = monitor.graphql(
            QUERY_RECENT_REPORTS,
            {
                "name": char["name"],
                "serverRegion": char["region"],
                "serverSlug": char["server"],
            },
        )

        if not res or "data" not in res:
            continue

        character_obj = res["data"].get("characterData", {}).get("character")
        if not character_obj:
            continue

        reports = character_obj.get("recentReports", {}).get("data", []) or []

        for r in reports:
            code = r["code"]

            if code not in monitor.seen_reports:
                print(
                    f"\n🎉 發現新上傳的 Report! Code: {code} (角色: {char['name']})"
                )

                details = monitor.graphql(QUERY_REPORT_DETAILS, {"code": code})
                report_data = (
                    details.get("data", {})
                    .get("reportData", {})
                    .get("report")
                    if details
                    else None
                )

                if report_data:
                    fights = report_data.get("fights", []) or []
                    title = report_data.get("title", "無標題")
                    uploader = report_data.get("owner", {}).get("name", "未知")

                    report_url = f"https://www.fflogs.com/reports/{code}"
                    msg = f"**角色:** {char['name']}\n**上傳者:** {uploader}\n**包含 Pull 數:** {len(fights)}"
                    monitor.send_discord_notify(
                        f"📊 偵測到新 Log 上傳：{title}", msg, report_url
                    )

                monitor.seen_reports.add(code)
                monitor.save_seen_reports()


# ============================================================
# 4. 常駐腳本入口
# ============================================================
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    monitor = FFLogsMonitor()
    char_list = [f"{c['name']} ({c['server']})" for c in TARGET_CHARACTERS]
    print("============================================")
    print(" FF Logs 常駐監控服務已於 Render 啟動")
    print(f" 監控角色: {char_list}")
    print("============================================")

    # 1. 優先執行手動補發
    check_forced_reports(monitor)

    # 2. 執行冷啟動預載
    init_cold_start(monitor)

    while True:
        try:
            check_updates(monitor)
        except Exception as e:
            print(f"❌ 執行過程發生未預期例外: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

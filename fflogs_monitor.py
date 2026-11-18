import http.server
import json
import os
import socket
import socketserver
import threading
import time
from datetime import datetime, timedelta, timezone
import requests

# 設定全域 socket 逾時，防止任何連線永久卡死
socket.setdefaulttimeout(15)

# ============================================================
# 1. 從環境變數讀取機密資訊與角色清單 (在 Render 後台設定)
# ============================================================
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

DEFAULT_CHARACTERS = []

env_target_chars = os.environ.get("TARGET_CHARACTERS", "")
if env_target_chars:
    try:
        TARGET_CHARACTERS = json.loads(env_target_chars)
    except Exception as e:
        print(f"⚠️ 解析 Render 環境變數 TARGET_CHARACTERS 失敗，改用程式內預設值: {e}")
        TARGET_CHARACTERS = DEFAULT_CHARACTERS
else:
    TARGET_CHARACTERS = DEFAULT_CHARACTERS

FORCE_CODES = []
BACKFILL_START_DATE = ""

CHECK_INTERVAL_SECONDS = 300
COLD_START_IGNORE_MINUTES = 60
SEEN_REPORTS_FILE = "seen_reports.json"
API_URL = "https://www.fflogs.com/api/v2/client"
TOKEN_URL = "https://www.fflogs.com/oauth/token"

global_monitor = None


def format_timestamp(ts_ms):
    if not ts_ms:
        return "未知"
    tz_tw = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=tz_tw)
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_date_to_ms(date_str):
    tz_tw = timezone(timedelta(hours=8))
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz_tw)
    return dt.timestamp() * 1000


# ============================================================
# 多執行緒防休眠 HTTP 服務（解決單執行緒連線卡死問題）
# ============================================================
class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))

    class SimpleHandler(http.server.BaseHTTPRequestHandler):

        def do_GET(self):
            try:
                if self.path == "/seen":
                    self.send_response(200)
                    self.send_header("Content-type", "application/json; charset=utf-8")
                    self.end_headers()
                    seen_list = list(global_monitor.seen_reports) if global_monitor else []
                    response_data = {
                        "total_count": len(seen_list),
                        "seen_codes": seen_list,
                    }
                    self.wfile.write(
                        json.dumps(response_data, indent=2, ensure_ascii=False).encode("utf-8")
                    )
                else:
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"OK")
            except Exception:
                pass

        def log_message(self, format, *args):
            return

    with ThreadingHTTPServer(("", port), SimpleHandler) as httpd:
        print(f"Threading HTTP Server listening on port {port}")
        httpd.serve_forever()


# ============================================================
# 2. OAuth Token 與狀態管理
# ============================================================
class FFLogsMonitor:

    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0
        self.seen_reports = self.load_seen_reports()

    def get_token(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self.access_token and now < self.token_expires_at - 60:
            return self.access_token

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 刷新 OAuth Token...")
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
        for attempt in range(3):
            try:
                # 若前一次失敗，強制刷新 Token
                token = self.get_token(force_refresh=(attempt > 0))
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                r = requests.post(
                    API_URL,
                    headers=headers,
                    json={"query": query, "variables": variables or {}},
                    timeout=30,
                )
                if r.status_code == 200:
                    res_json = r.json()
                    # 驗證 GraphQL 內部是否回傳錯誤（即使 HTTP 狀態為 200）
                    if "errors" in res_json:
                        print(f"⚠️ GraphQL 回傳錯誤 (第 {attempt + 1} 次): {res_json['errors']}")
                        time.sleep(3)
                        continue
                    return res_json
                elif r.status_code in [401, 429, 500, 502, 503, 504]:
                    print(f"⚠️ FF Logs API 回傳狀態碼 {r.status_code}，3 秒後重試...")
                    self.access_token = None
                    time.sleep(3)
                else:
                    r.raise_for_status()
            except Exception as e:
                print(f"⚠️ API 請求異常 (第 {attempt + 1} 次): {e}")
                time.sleep(3)
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
        """發送 Discord 通知，並回傳 True/False 確認是否真正送達"""
        if not DISCORD_WEBHOOK_URL:
            print("❌ 未設定 DISCORD_WEBHOOK_URL")
            return False

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

        for attempt in range(3):
            try:
                r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
                if r.status_code in [200, 204]:
                    return True
                elif r.status_code == 429:
                    retry_after = r.json().get("retry_after", 3)
                    print(f"⚠️ Discord 觸發速率限制，等待 {retry_after} 秒後重試...")
                    time.sleep(retry_after)
                else:
                    print(f"⚠️ Discord 回傳異常狀態碼 {r.status_code}: {r.text}")
                    time.sleep(2)
            except Exception as e:
                print(f"⚠️ Discord 連線失敗 (第 {attempt + 1} 次): {e}")
                time.sleep(2)

        return False


# ============================================================
# 3. 核心監控與補發邏輯
# ============================================================
QUERY_RECENT_REPORTS = """
query ($name: String!, $serverRegion: String!, $serverSlug: String!, $limit: Int!) {
    characterData {
        character(name: $name, serverRegion: $serverRegion, serverSlug: $serverSlug) {
            name
            recentReports(limit: $limit) {
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
            startTime
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


def send_report_embed(monitor, code, char_name, is_backfill=False):
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
        upload_time = format_timestamp(report_data.get("startTime"))

        report_url = f"https://www.fflogs.com/reports/{code}"
        prefix = "📊 補發歷史 Log 通知：" if is_backfill else "📊 偵測到新 Log 上傳："
        msg = (
            f"**角色:** {char_name}\n"
            f"**上傳者:** {uploader}\n"
            f"**記錄時間:** {upload_time}\n"
            f"**包含 Pull 數:** {len(fights)}"
        )
        # 只有當 Discord 確實回傳成功時，才回傳 True
        if monitor.send_discord_notify(f"{prefix}{title}", msg, report_url):
            print(f"✅ 已成功送出通知: [{upload_time}] {title} ({code})")
            return True
        else:
            print(f"❌ 通知送出失敗，保留為未讀待下輪重試: {code}")
            return False
    return False


def backfill_since_date(monitor):
    if not BACKFILL_START_DATE:
        return

    try:
        start_ms = parse_date_to_ms(BACKFILL_START_DATE)
    except ValueError:
        print(f"❌ BACKFILL_START_DATE 格式錯誤 ({BACKFILL_START_DATE})")
        return

    print(f"\n📅 啟動歷史區間補發：搜尋 {BACKFILL_START_DATE} 至今的紀錄...")
    pending_reports = {}

    for char in TARGET_CHARACTERS:
        res = monitor.graphql(
            QUERY_RECENT_REPORTS,
            {
                "name": char["name"],
                "serverRegion": char["region"],
                "serverSlug": char["server"],
                "limit": 50,
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
            r_start = r.get("startTime", 0)
            if r_start >= start_ms and code not in monitor.seen_reports:
                if code not in pending_reports:
                    pending_reports[code] = {
                        "code": code,
                        "startTime": r_start,
                        "char_name": char["name"],
                    }

    sorted_reports = sorted(pending_reports.values(), key=lambda x: x["startTime"])
    for item in sorted_reports:
        code = item["code"]
        if send_report_embed(monitor, code, item["char_name"], is_backfill=True):
            monitor.seen_reports.add(code)
            monitor.save_seen_reports()
        time.sleep(1.5)


def check_forced_reports(monitor):
    if not FORCE_CODES:
        return

    print("\n⚡ 偵測到手動補發清單，準備發送通知...")
    for code in FORCE_CODES:
        if send_report_embed(monitor, code, "手動指定", is_backfill=True):
            monitor.seen_reports.add(code)
            monitor.save_seen_reports()
        time.sleep(1.5)


def init_cold_start(monitor):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 進行冷啟動初始化：載入超過 {COLD_START_IGNORE_MINUTES} 分鐘前的歷史 Log..."
    )
    count = 0
    cutoff_ms = (time.time() - COLD_START_IGNORE_MINUTES * 60) * 1000

    for char in TARGET_CHARACTERS:
        res = monitor.graphql(
            QUERY_RECENT_REPORTS,
            {
                "name": char["name"],
                "serverRegion": char["region"],
                "serverSlug": char["server"],
                "limit": 10,
            },
        )
        if not res or "data" not in res:
            continue

        character_obj = res["data"].get("characterData", {}).get("character")
        if not character_obj:
            print(f"⚠️ 冷啟動找不到角色: {char['name']} ({char['server']}-{char['region']})")
            continue

        reports = character_obj.get("recentReports", {}).get("data", []) or []
        for r in reports:
            code = r["code"]
            if code in FORCE_CODES:
                continue
            if r.get("startTime", 0) > cutoff_ms:
                continue
            if code not in monitor.seen_reports:
                monitor.seen_reports.add(code)
                count += 1

    monitor.save_seen_reports()
    print(f"✅ 冷啟動完成！已將 {count} 份舊 Log 預先標記為已讀。\n")


def check_updates(monitor):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 掃描新 Log 中... (當前已讀筆數: {len(monitor.seen_reports)})"
    )

    for char in TARGET_CHARACTERS:
        res = monitor.graphql(
            QUERY_RECENT_REPORTS,
            {
                "name": char["name"],
                "serverRegion": char["region"],
                "serverSlug": char["server"],
                "limit": 10,
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
                print(f"\n🎉 發現新上傳的 Report! Code: {code} (角色: {char['name']})")
                # 嚴格驗證：只有 Discord 真正送出成功才寫入已讀
                if send_report_embed(monitor, code, char["name"], is_backfill=False):
                    monitor.seen_reports.add(code)
                    monitor.save_seen_reports()
                time.sleep(1.5)


# ============================================================
# 4. 常駐腳本入口
# ============================================================
def main():
    global global_monitor

    threading.Thread(target=run_dummy_server, daemon=True).start()

    monitor = FFLogsMonitor()
    global_monitor = monitor

    char_list = [f"{c['name']} ({c['server']})" for c in TARGET_CHARACTERS]
    print("============================================")
    print(" FF Logs 常駐監控服務已於 Render 啟動")
    print(f" 監控角色 ({len(TARGET_CHARACTERS)} 位): {char_list}")
    print("============================================")

    backfill_since_date(monitor)
    check_forced_reports(monitor)
    init_cold_start(monitor)

    while True:
        try:
            check_updates(monitor)
        except Exception as e:
            print(f"❌ 執行過程發生未預期例外: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

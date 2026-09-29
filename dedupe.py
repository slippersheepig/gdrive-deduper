import os
import time
import logging
from collections import defaultdict
import requests
import schedule
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 环境变量配置
CLIENT_ID = os.getenv("GDRIVE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GDRIVE_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GDRIVE_REFRESH_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SCAN_SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "03:00")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 过滤掉回收站、文件夹以及 Google Drive 快捷方式
DEFAULT_QUERY = (
    "trashed = false and "
    "mimeType != 'application/vnd.google-apps.folder' and "
    "mimeType != 'application/vnd.google-apps.shortcut'"
)
TARGET_MIME_QUERY = os.getenv("MIME_QUERY", DEFAULT_QUERY)

SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram 推送失败: {e}")

def get_file_type_category(mime: str) -> str:
    if not mime:
        return "其它"
    if "video" in mime:
        return "视频"
    if "image" in mime:
        return "图片"
    if "audio" in mime:
        return "音频"
    if any(x in mime for x in ["pdf", "document", "sheet", "text", "msword"]):
        return "文档"
    if any(x in mime for x in ["zip", "rar", "7z", "tar", "gzip"]):
        return "压缩包"
    return "其它"

def run_dedupe():
    logging.info("========== 开始执行 Google Drive 全盘文件查重任务 ==========")
    if DRY_RUN:
        logging.info("当前运行模式: [DRY-RUN 试运行] (仅打印，不实际执行移入回收站)")
    else:
        logging.warning("当前运行模式: [LIVE 生产模式] (将重复文件直接移入回收站)")

    try:
        service = get_drive_service()
    except Exception as e:
        logging.error(f"获取 Google Drive 客户端授权失败: {e}")
        send_telegram(f"❌ *Google Drive 去重失败*: 授权异常\n`{e}`")
        return

    page_token = None
    all_files = []
    
    # 1. 遍历拉取全盘文件元数据
    try:
        while True:
            response = service.files().list(
                q=TARGET_MIME_QUERY,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, size, createdTime)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            files = response.get("files", [])
            all_files.extend(files)
            page_token = response.get("nextPageToken", None)
            if not page_token:
                break
    except HttpError as e:
        logging.error(f"查询 Google Drive 文件失败: {e}")
        send_telegram(f"❌ *Google Drive 扫描失败*: API 错误\n`{e}`")
        return

    logging.info(f"扫描完毕，共获取 {len(all_files)} 个对象。")

    # 2. 按 md5Checksum 分组（过滤掉 0 字节文件与无 MD5 的云端原生文件）
    groups = defaultdict(list)
    skipped_zero_or_no_md5 = 0

    for f in all_files:
        md5 = f.get("md5Checksum")
        size = int(f.get("size", 0))
        # 保护机制：没有 MD5 或体积为 0 字节的文件跳过比对
        if not md5 or size == 0:
            skipped_zero_or_no_md5 += 1
            continue
        groups[md5].append(f)

    if skipped_zero_or_no_md5 > 0:
        logging.info(f"已自动跳过 {skipped_zero_or_no_md5} 个 0 字节文件或无 MD5 记录的对象。")

    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
    total_dupes = sum(len(v) - 1 for v in duplicate_groups.values())
    logging.info(f"发现 {len(duplicate_groups)} 组重复文件，包含多余文件 {total_dupes} 个。")

    trashed_count = 0
    reclaimed_bytes = 0
    category_stats = defaultdict(int)

    # 3. 筛选并执行清理（默认保留创建时间最早的文件）
    for md5, flist in duplicate_groups.items():
        # 按创建时间排序，最早创建的文件保留
        flist.sort(key=lambda x: x.get("createdTime", ""))
        keeper = flist[0]
        to_trash = flist[1:]

        logging.info(f"保留原件: {keeper['name']} (ID: {keeper['id']})")
        for item in to_trash:
            f_id = item["id"]
            f_name = item["name"]
            f_size = int(item.get("size", 0))
            f_mime = item.get("mimeType", "")
            cat = get_file_type_category(f_mime)
            category_stats[cat] += 1

            logging.info(f"  -> 标记重复 [{cat}]: {f_name} ({round(f_size / 1024 / 1024, 2)} MB)")

            if not DRY_RUN:
                try:
                    service.files().update(fileId=f_id, body={"trashed": True}).execute()
                    trashed_count += 1
                    reclaimed_bytes += f_size
                except HttpError as err:
                    logging.error(f"清理文件 {f_name} 失败: {err}")
            else:
                trashed_count += 1
                reclaimed_bytes += f_size

    reclaimed_gb = round(reclaimed_bytes / (1024 ** 3), 2)
    cat_summary = ", ".join([f"{k}: {v}个" for k, v in category_stats.items()]) if category_stats else "无"

    summary_text = (
        f"📊 *Google Drive 全盘自动去重报告*\n"
        f"- 扫描文件总数: `{len(all_files)}`\n"
        f"- 发现重复组数: `{len(duplicate_groups)}`\n"
        f"- {'预估清理' if DRY_RUN else '已移入回收站'}: `{trashed_count}` 个文件\n"
        f"- 类型分布: `{cat_summary}`\n"
        f"- 释放空间: `{reclaimed_gb} GB`\n"
        f"- 运行模式: `{'Dry-Run (模拟运行)' if DRY_RUN else 'Live (已删除)'}`"
    )
    logging.info(summary_text.replace("*", "").replace("`", ""))
    send_telegram(summary_text)

def main():
    logging.info(f"全盘去重服务已启动，设定每日定时执行时间: {SCAN_SCHEDULE_TIME}")
    # 容器启动先跑一次
    run_dedupe()
    
    # 每日定时调度
    schedule.every().day.at(SCAN_SCHEDULE_TIME).do(run_dedupe)

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()

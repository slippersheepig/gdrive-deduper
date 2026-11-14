import os
import re
import time
import logging
from collections import defaultdict, deque
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
RAW_FOLDER_ID = os.getenv("FOLDER_ID", "").strip()
DRIVE_CORPORA = os.getenv("DRIVE_CORPORA", "").strip() # 如需跨团队盘搜索可配置为 allDrives

DEFAULT_QUERY = (
    "trashed = false and "
    "mimeType != 'application/vnd.google-apps.folder' and "
    "mimeType != 'application/vnd.google-apps.shortcut'"
)
TARGET_MIME_QUERY = os.getenv("MIME_QUERY", DEFAULT_QUERY)

SCOPES = ["https://www.googleapis.com/auth/drive"]

class DisjointSet:
    """并查集数据结构，用于无缝合并多维度（MD5 + 视频严格元数据）重复文件"""
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: int, j: int):
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j

def parse_folder_ids(raw_str: str) -> list[str]:
    """提取纯文件夹 ID，支持逗号分隔多个，并自动过滤 URL 前缀"""
    if not raw_str:
        return []
    ids = []
    for item in raw_str.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        # 兼容用户直接粘贴浏览器完整 URL 的情况
        match = re.search(r"folders/([a-zA-Z0-9_-]+)", cleaned)
        if match:
            ids.append(match.group(1))
        else:
            ids.append(cleaned)
    return ids

TARGET_FOLDER_IDS = parse_folder_ids(RAW_FOLDER_ID)

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

def scan_recursive_folders(service, root_folder_ids: list[str]) -> list[dict]:
    """针对指定文件夹进行多层递归扫描（广度优先遍历 BFS）"""
    all_files = []
    folder_queue = deque(root_folder_ids)
    visited_folders = set(root_folder_ids)
    total_folders_scanned = 0

    while folder_queue:
        current_folder = folder_queue.popleft()
        total_folders_scanned += 1
        page_token = None
        query = f"'{current_folder}' in parents and trashed = false"

        while True:
            list_kwargs = {
                "q": query,
                "spaces": "drive",
                "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, size, createdTime, videoMediaMetadata)",
                "pageSize": 1000,
                "pageToken": page_token,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if DRIVE_CORPORA:
                list_kwargs["corpora"] = DRIVE_CORPORA

            response = service.files().list(**list_kwargs).execute()

            for item in response.get("files", []):
                mime = item.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    if item["id"] not in visited_folders:
                        visited_folders.add(item["id"])
                        folder_queue.append(item["id"])
                elif mime != "application/vnd.google-apps.shortcut":
                    all_files.append(item)

            page_token = response.get("nextPageToken", None)
            if not page_token:
                break

    logging.info(f"递归扫描完成：遍历了 {total_folders_scanned} 个文件夹，捕获 {len(all_files)} 个文件。")
    return all_files

def scan_full_drive(service) -> list[dict]:
    """未指定目录时进行全盘扫描"""
    all_files = []
    page_token = None
    while True:
        list_kwargs = {
            "q": TARGET_MIME_QUERY,
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, size, createdTime, videoMediaMetadata)",
            "pageSize": 1000,
            "pageToken": page_token,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if DRIVE_CORPORA:
            list_kwargs["corpora"] = DRIVE_CORPORA

        response = service.files().list(**list_kwargs).execute()

        all_files.extend(response.get("files", []))
        page_token = response.get("nextPageToken", None)
        if not page_token:
            break
    logging.info(f"全盘扫描完成：共获取 {len(all_files)} 个对象。")
    return all_files

def run_dedupe():
    target_desc = f"指定目录 [{', '.join(TARGET_FOLDER_IDS)}]" if TARGET_FOLDER_IDS else "全盘文件"
    logging.info(f"========== 开始执行 Google Drive 去重任务 ({target_desc}) ==========")
    if DRY_RUN:
        logging.info("当前运行模式: [DRY-RUN 试运行] (仅打印，不实际执行移入回收站)")
    else:
        logging.warning("当前运行模式: [LIVE 生产模式] (将重复文件直接移入回收站)")

    try:
        service = get_drive_service()
    except Exception as e:
        logging.error(f"获取 Google Drive 授权失败: {e}")
        send_telegram(f"❌ *Google Drive 去重失败*: 授权异常\n`{e}`")
        return

    try:
        if TARGET_FOLDER_IDS:
            all_files = scan_recursive_folders(service, TARGET_FOLDER_IDS)
        else:
            all_files = scan_full_drive(service)
    except HttpError as e:
        logging.error(f"扫描文件失败: {e}")
        send_telegram(f"❌ *Google Drive 扫描失败*: API 错误\n`{e}`")
        return

    # 过滤 0 字节文件
    valid_files = []
    skipped_count = 0
    for f in all_files:
        size = int(f.get("size", 0))
        if size == 0:
            skipped_count += 1
            continue
        valid_files.append(f)

    if skipped_count > 0:
        logging.info(f"已跳过 {skipped_count} 个 0 字节空文件。")

    # 双轨去重聚类（MD5 精确哈希匹配 + 视频严格元数据指纹匹配）
    dsu = DisjointSet(len(valid_files))
    md5_map = defaultdict(list)
    vmeta_map = defaultdict(list)

    for idx, f in enumerate(valid_files):
        size = int(f.get("size", 0))
        md5 = f.get("md5Checksum")
        if md5:
            md5_map[md5].append(idx)

        # 视频特征指纹：当且仅当文件大小、分辨率、毫秒时长全部一致且有效时聚类
        vmd = f.get("videoMediaMetadata")
        if vmd:
            try:
                dur = int(vmd.get("durationMillis", 0))
                w = int(vmd.get("width", 0))
                h = int(vmd.get("height", 0))
                if dur > 0 and w > 0 and h > 0:
                    vkey = (size, w, h, dur)
                    vmeta_map[vkey].append(idx)
            except (ValueError, TypeError):
                pass

    # 1. 合并 MD5 相同的组
    for indices in md5_map.values():
        for i in range(1, len(indices)):
            dsu.union(indices[0], indices[i])

    # 2. 合并视频元数据完全一致的组（即使 MD5 不同或缺失）
    for indices in vmeta_map.values():
        for i in range(1, len(indices)):
            dsu.union(indices[0], indices[i])

    # 聚类成最终重复组
    cluster_groups = defaultdict(list)
    for idx, f in enumerate(valid_files):
        md5 = f.get("md5Checksum")
        vmd = f.get("videoMediaMetadata")
        has_vmeta = False
        if vmd:
            try:
                has_vmeta = (
                    int(vmd.get("durationMillis", 0)) > 0
                    and int(vmd.get("width", 0)) > 0
                    and int(vmd.get("height", 0)) > 0
                )
            except (ValueError, TypeError):
                pass

        # 仅当文件具有 MD5 或具有有效视频元数据时才纳入聚合
        if not md5 and not has_vmeta:
            continue

        root = dsu.find(idx)
        cluster_groups[root].append(f)

    duplicate_groups = {k: v for k, v in cluster_groups.items() if len(v) > 1}
    total_dupes = sum(len(v) - 1 for v in duplicate_groups.values())
    logging.info(f"发现 {len(duplicate_groups)} 组重复文件，包含多余文件 {total_dupes} 个。")

    trashed_count = 0
    reclaimed_bytes = 0
    category_stats = defaultdict(int)
    md5_match_count = 0
    vmeta_match_count = 0

    for root_id, flist in duplicate_groups.items():
        # 保留创建时间最早的一份
        flist.sort(key=lambda x: x.get("createdTime", ""))
        keeper = flist[0]
        to_trash = flist[1:]

        # 判断并记录匹配规则
        md5_set = {x.get("md5Checksum") for x in flist if x.get("md5Checksum")}
        is_pure_md5 = (len(md5_set) == 1 and len(flist) == sum(1 for x in flist if x.get("md5Checksum")))

        if is_pure_md5:
            md5_match_count += 1
            reason_str = "MD5 精确匹配"
        else:
            vmeta_match_count += 1
            vmd = next((x.get("videoMediaMetadata") for x in flist if x.get("videoMediaMetadata")), {})
            dur_sec = round(int(vmd.get("durationMillis", 0)) / 1000, 2)
            reason_str = (
                f"视频严格元数据匹配 (大小: {round(int(keeper.get('size', 0)) / 1024 / 1024, 2)} MB, "
                f"分辨率: {vmd.get('width')}x{vmd.get('height')}, 时长: {dur_sec}s)"
            )

        logging.info(f"保留原件 [{reason_str}]: {keeper['name']} (ID: {keeper['id']})")
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
        f"📊 *Google Drive 自动去重报告*\n"
        f"- 扫描范围: `{target_desc}`\n"
        f"- 扫描文件总数: `{len(all_files)}`\n"
        f"- 发现重复组数: `{len(duplicate_groups)}` (MD5匹配: {md5_match_count}组, 视频元数据匹配: {vmeta_match_count}组)\n"
        f"- {'预估清理' if DRY_RUN else '已移入回收站'}: `{trashed_count}` 个文件\n"
        f"- 类型分布: `{cat_summary}`\n"
        f"- 释放空间: `{reclaimed_gb} GB`\n"
        f"- 运行模式: `{'Dry-Run (模拟运行)' if DRY_RUN else 'Live (已删除)'}`"
    )
    logging.info(summary_text.replace("*", "").replace("`", ""))
    send_telegram(summary_text)

def main():
    logging.info(f"去重服务已启动，设定每日定时执行时间: {SCAN_SCHEDULE_TIME}")
    run_dedupe()
    schedule.every().day.at(SCAN_SCHEDULE_TIME).do(run_dedupe)

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()

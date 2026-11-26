import logging
import os
import re
import time
from collections import defaultdict, deque

import requests
import schedule
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 环境变量配置
CLIENT_ID = os.getenv("GDRIVE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GDRIVE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("GDRIVE_REFRESH_TOKEN", "").strip()
DRY_RUN = (os.getenv("DRY_RUN", "").strip() or "true").lower() not in {"false", "0", "no"}
RAW_SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "").strip() or "03:00"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RAW_FOLDER_ID = os.getenv("FOLDER_ID", "").strip()
DRIVE_CORPORA = os.getenv("DRIVE_CORPORA", "").strip()  # 如需跨团队盘搜索可配置为 allDrives

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DEFAULT_QUERY = (
    f"trashed = false and "
    f"mimeType != '{FOLDER_MIME}' and "
    f"mimeType != '{SHORTCUT_MIME}'"
)
TARGET_MIME_QUERY = os.getenv("MIME_QUERY", "").strip() or DEFAULT_QUERY

SCOPES = ["https://www.googleapis.com/auth/drive"]
FILE_FIELDS = (
    "nextPageToken, "
    "files(id, name, mimeType, md5Checksum, size, createdTime, videoMediaMetadata, imageMediaMetadata)"
)
API_NUM_RETRIES = 3
FOLDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


class DisjointSet:
    """并查集数据结构，用于无缝合并多维度（MD5 + 视频/图片严格元数据）重复文件"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:
            next_i = self.parent[i]
            self.parent[i] = root
            i = next_i
        return root

    def union(self, i: int, j: int) -> None:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i == root_j:
            return
        if self.rank[root_i] < self.rank[root_j]:
            self.parent[root_i] = root_j
        elif self.rank[root_i] > self.rank[root_j]:
            self.parent[root_j] = root_i
        else:
            self.parent[root_j] = root_i
            self.rank[root_i] += 1


def normalize_schedule_time(raw_time: str) -> str:
    """标准化定时时间格式，兼容形如 3:00 的单数字小时写法"""
    cleaned = raw_time.strip()
    if re.fullmatch(r"\d:\d{2}(:\d{2})?", cleaned):
        return f"0{cleaned}"
    return cleaned


def parse_folder_ids(raw_str: str) -> list[str]:
    """提取纯文件夹 ID，支持逗号分隔多个，自动过滤 URL 前缀并保序去重"""
    if not raw_str:
        return []
    ids = []
    seen = set()
    for item in raw_str.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        # 兼容用户直接粘贴浏览器完整 URL 的情况（支持 /folders/ID 与 ?id=ID 格式）
        match = re.search(r"(?:folders/|(?:\?|&)id=)([a-zA-Z0-9_-]+)", cleaned)
        folder_id = match.group(1) if match else cleaned
        if not FOLDER_ID_PATTERN.fullmatch(folder_id):
            logging.warning(f"忽略格式异常的文件夹 ID: {cleaned}")
            continue
        if folder_id not in seen:
            seen.add(folder_id)
            ids.append(folder_id)
    return ids


SCAN_SCHEDULE_TIME = normalize_schedule_time(RAW_SCHEDULE_TIME)
TARGET_FOLDER_IDS = parse_folder_ids(RAW_FOLDER_ID)


def get_drive_service():
    missing = [
        name
        for name, val in (
            ("GDRIVE_CLIENT_ID", CLIENT_ID),
            ("GDRIVE_CLIENT_SECRET", CLIENT_SECRET),
            ("GDRIVE_REFRESH_TOKEN", REFRESH_TOKEN),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"缺少必要的环境变量: {', '.join(missing)}")

    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def sanitize_md_code(value: object) -> str:
    """清理内联代码片段中的反引号，避免破坏 Telegram Markdown 解析"""
    return str(value).replace("`", "'")


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 400:
            fallback_payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text.replace("*", "").replace("`", ""),
            }
            resp = requests.post(url, json=fallback_payload, timeout=10)
        if not resp.ok:
            logging.error(f"Telegram 推送失败 (HTTP {resp.status_code}): {resp.text}")
    except requests.RequestException as e:
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
    if any(x in mime for x in ("pdf", "document", "sheet", "text", "msword")):
        return "文档"
    if any(x in mime for x in ("zip", "rar", "7z", "tar", "gzip")):
        return "压缩包"
    return "其它"


def get_file_size(file_obj: dict) -> int:
    try:
        return int(file_obj.get("size") or 0)
    except (ValueError, TypeError):
        return 0


def extract_video_key(file_obj: dict, size: int) -> tuple[int, int, int, int] | None:
    """视频特征指纹：当且仅当文件大小、分辨率、毫秒时长全部一致且大于 0 时返回有效键"""
    vmd = file_obj.get("videoMediaMetadata")
    if not isinstance(vmd, dict):
        return None
    try:
        dur = int(vmd.get("durationMillis") or 0)
        w = int(vmd.get("width") or 0)
        h = int(vmd.get("height") or 0)
    except (ValueError, TypeError):
        return None
    if size > 0 and dur > 0 and w > 0 and h > 0:
        return (size, w, h, dur)
    return None


def extract_image_key(file_obj: dict, size: int) -> tuple[int, int, int] | None:
    """图片特征指纹：当且仅当文件大小（精确到字节）、分辨率全部一致且大于 0 时返回有效键"""
    imd = file_obj.get("imageMediaMetadata")
    if not isinstance(imd, dict):
        return None
    try:
        w = int(imd.get("width") or 0)
        h = int(imd.get("height") or 0)
    except (ValueError, TypeError):
        return None
    if size > 0 and w > 0 and h > 0:
        return (size, w, h)
    return None


def list_files_page(service, query: str, page_token: str | None = None) -> dict:
    list_kwargs = {
        "q": query,
        "spaces": "drive",
        "fields": FILE_FIELDS,
        "pageSize": 1000,
        "pageToken": page_token,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }
    if DRIVE_CORPORA:
        list_kwargs["corpora"] = DRIVE_CORPORA
    return service.files().list(**list_kwargs).execute(num_retries=API_NUM_RETRIES)


def scan_recursive_folders(service, root_folder_ids: list[str]) -> list[dict]:
    """针对指定文件夹进行多层递归扫描（广度优先遍历 BFS）"""
    all_files = []
    seen_file_ids = set()
    unique_roots = list(dict.fromkeys(root_folder_ids))
    folder_queue = deque(unique_roots)
    visited_folders = set(unique_roots)
    total_folders_scanned = 0

    while folder_queue:
        current_folder = folder_queue.popleft()
        total_folders_scanned += 1
        page_token = None
        query = f"'{current_folder}' in parents and trashed = false"

        while True:
            response = list_files_page(service, query, page_token)

            for item in response.get("files", []):
                item_id = item.get("id")
                if not item_id:
                    continue
                mime = item.get("mimeType", "")
                if mime == FOLDER_MIME:
                    if item_id not in visited_folders:
                        visited_folders.add(item_id)
                        folder_queue.append(item_id)
                elif mime != SHORTCUT_MIME and item_id not in seen_file_ids:
                    seen_file_ids.add(item_id)
                    all_files.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    logging.info(f"递归扫描完成：遍历了 {total_folders_scanned} 个文件夹，捕获 {len(all_files)} 个文件。")
    return all_files


def scan_full_drive(service) -> list[dict]:
    """未指定目录时进行全盘扫描"""
    all_files = []
    seen_file_ids = set()
    page_token = None
    query = TARGET_MIME_QUERY
    if "trashed" not in query.lower():
        query = f"({query}) and trashed = false"

    while True:
        response = list_files_page(service, query, page_token)

        for item in response.get("files", []):
            item_id = item.get("id")
            mime = item.get("mimeType", "")
            if not item_id or mime in (FOLDER_MIME, SHORTCUT_MIME):
                continue
            if item_id not in seen_file_ids:
                seen_file_ids.add(item_id)
                all_files.append(item)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logging.info(f"全盘扫描完成：共获取 {len(all_files)} 个对象。")
    return all_files


def run_dedupe() -> None:
    if RAW_FOLDER_ID and not TARGET_FOLDER_IDS:
        err_msg = f"FOLDER_ID 配置无效，未解析到任何合法文件夹 ID: {RAW_FOLDER_ID}"
        logging.error(err_msg)
        send_telegram(f"❌ *Google Drive 去重失败*: 目录配置异常\n`{sanitize_md_code(err_msg)}`")
        return

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
        send_telegram(f"❌ *Google Drive 去重失败*: 授权异常\n`{sanitize_md_code(e)}`")
        return

    try:
        if TARGET_FOLDER_IDS:
            all_files = scan_recursive_folders(service, TARGET_FOLDER_IDS)
        else:
            all_files = scan_full_drive(service)
    except (HttpError, GoogleAuthError, OSError) as e:
        logging.error(f"扫描文件失败: {e}")
        send_telegram(f"❌ *Google Drive 扫描失败*: API 或网络错误\n`{sanitize_md_code(e)}`")
        return
    except Exception as e:
        logging.exception(f"扫描阶段发生未预期异常: {e}")
        send_telegram(f"❌ *Google Drive 扫描失败*: 未知异常\n`{sanitize_md_code(e)}`")
        return

    # 过滤 0 字节文件并按文件 ID 兜底去重
    valid_files = []
    seen_ids = set()
    skipped_count = 0
    for f in all_files:
        f_id = f.get("id")
        if not f_id or f_id in seen_ids:
            continue
        seen_ids.add(f_id)
        if get_file_size(f) <= 0:
            skipped_count += 1
            continue
        valid_files.append(f)

    if skipped_count > 0:
        logging.info(f"已跳过 {skipped_count} 个 0 字节空文件。")

    # 多维去重聚类（MD5 精确匹配 + 视频元数据指纹 + 图片元数据指纹）
    dsu = DisjointSet(len(valid_files))
    md5_map = defaultdict(list)
    vmeta_map = defaultdict(list)
    imeta_map = defaultdict(list)

    for idx, f in enumerate(valid_files):
        size = get_file_size(f)
        md5 = f.get("md5Checksum")
        if md5:
            md5_map[md5].append(idx)

        vkey = extract_video_key(f, size)
        if vkey:
            vmeta_map[vkey].append(idx)

        ikey = extract_image_key(f, size)
        if ikey:
            imeta_map[ikey].append(idx)

    # 1. 合并 MD5 相同的组
    for indices in md5_map.values():
        for i in range(1, len(indices)):
            dsu.union(indices[0], indices[i])

    # 2. 合并视频元数据完全一致的组（即使 MD5 不同或缺失）
    for indices in vmeta_map.values():
        for i in range(1, len(indices)):
            dsu.union(indices[0], indices[i])

    # 3. 合并图片元数据完全一致的组（即使 MD5 不同或缺失）
    for indices in imeta_map.values():
        for i in range(1, len(indices)):
            dsu.union(indices[0], indices[i])

    # 聚类成最终重复组（无任何特征的文件不会参与合并，其连通分量长度必为 1）
    cluster_groups = defaultdict(list)
    for idx, f in enumerate(valid_files):
        cluster_groups[dsu.find(idx)].append(f)

    duplicate_groups = {k: v for k, v in cluster_groups.items() if len(v) > 1}
    total_dupes = sum(len(v) - 1 for v in duplicate_groups.values())
    logging.info(f"发现 {len(duplicate_groups)} 组重复文件，包含多余文件 {total_dupes} 个。")

    trashed_count = 0
    failed_count = 0
    reclaimed_bytes = 0
    category_stats = defaultdict(int)
    md5_match_count = 0
    vmeta_match_count = 0
    imeta_match_count = 0

    for flist in duplicate_groups.values():
        # 保留创建时间最早的一份（缺失创建时间排后，相同时间按 ID 稳定排序）
        flist.sort(key=lambda x: (x.get("createdTime") or "9999", x.get("id", "")))
        keeper = flist[0]
        to_trash = flist[1:]

        # 判断并记录匹配规则
        md5_list = [x.get("md5Checksum") for x in flist]
        is_pure_md5 = bool(md5_list[0]) and len(set(md5_list)) == 1

        keeper_size_mb = round(get_file_size(keeper) / (1024 ** 2), 2)
        if is_pure_md5:
            md5_match_count += 1
            reason_str = "MD5 精确匹配"
        else:
            vkey = next(
                (k for x in flist if (k := extract_video_key(x, get_file_size(x))) is not None),
                None,
            )
            ikey = next(
                (k for x in flist if (k := extract_image_key(x, get_file_size(x))) is not None),
                None,
            )

            if vkey:
                _, w, h, dur = vkey
                vmeta_match_count += 1
                dur_sec = round(dur / 1000, 2)
                reason_str = (
                    f"视频严格元数据匹配 (大小: {keeper_size_mb} MB, "
                    f"分辨率: {w}x{h}, 时长: {dur_sec}s)"
                )
            elif ikey:
                _, w, h = ikey
                imeta_match_count += 1
                reason_str = (
                    f"图片严格元数据匹配 (大小: {keeper_size_mb} MB, "
                    f"分辨率: {w}x{h})"
                )
            else:
                reason_str = "元数据匹配"

        logging.info(f"保留原件 [{reason_str}]: {keeper.get('name', '')} (ID: {keeper['id']})")
        for item in to_trash:
            f_id = item["id"]
            if f_id == keeper["id"]:
                continue
            f_name = item.get("name", f_id)
            f_size = get_file_size(item)
            f_mime = item.get("mimeType", "")
            cat = get_file_type_category(f_mime)

            logging.info(f"  -> 标记重复 [{cat}]: {f_name} ({round(f_size / (1024 ** 2), 2)} MB)")

            if not DRY_RUN:
                try:
                    service.files().update(
                        fileId=f_id,
                        body={"trashed": True},
                        supportsAllDrives=True,
                    ).execute(num_retries=API_NUM_RETRIES)
                except (HttpError, GoogleAuthError, OSError) as err:
                    failed_count += 1
                    logging.error(f"清理文件 {f_name} 失败: {err}")
                    continue

            trashed_count += 1
            reclaimed_bytes += f_size
            category_stats[cat] += 1

    reclaimed_gb = round(reclaimed_bytes / (1024 ** 3), 2)
    cat_summary = ", ".join(f"{k}: {v}个" for k, v in category_stats.items()) if category_stats else "无"

    match_parts = [f"MD5: {md5_match_count}组"]
    if vmeta_match_count > 0:
        match_parts.append(f"视频元数据: {vmeta_match_count}组")
    if imeta_match_count > 0:
        match_parts.append(f"图片元数据: {imeta_match_count}组")
    match_summary = ", ".join(match_parts)

    failed_line = f"- 清理失败: `{failed_count}` 个文件\n" if failed_count > 0 else ""
    summary_text = (
        f"📊 *Google Drive 自动去重报告*\n"
        f"- 扫描范围: `{sanitize_md_code(target_desc)}`\n"
        f"- 扫描文件总数: `{len(all_files)}`\n"
        f"- 发现重复组数: `{len(duplicate_groups)}` ({match_summary})\n"
        f"- {'预估清理' if DRY_RUN else '已移入回收站'}: `{trashed_count}` 个文件\n"
        f"{failed_line}"
        f"- 类型分布: `{cat_summary}`\n"
        f"- 释放空间: `{reclaimed_gb} GB`\n"
        f"- 运行模式: `{'Dry-Run (模拟运行)' if DRY_RUN else 'Live (已删除)'}`"
    )
    logging.info(summary_text.replace("*", "").replace("`", ""))
    send_telegram(summary_text)


def main() -> None:
    try:
        schedule.every().day.at(SCAN_SCHEDULE_TIME).do(run_dedupe)
    except schedule.ScheduleValueError as e:
        logging.error(f"定时时间格式无效 (SCHEDULE_TIME={RAW_SCHEDULE_TIME!r}): {e}")
        send_telegram(f"❌ *Google Drive 去重启动失败*: 定时时间配置无效\n`{sanitize_md_code(e)}`")
        return

    logging.info(f"去重服务已启动，设定每日定时执行时间: {SCAN_SCHEDULE_TIME}")
    run_dedupe()

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logging.info("收到退出信号，去重服务已停止。")


if __name__ == "__main__":
    main()

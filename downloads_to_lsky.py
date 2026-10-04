#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downloads_to_lsky.py — 把 ~/Downloads 里的图片/视频定时同步到 Lsky Pro。

行为（依据需求）：
  1. 每次运行扫描 downloads_dir 下的图片/视频（可递归）。
  2. 对“尚未上传过”的文件：上传到 Lsky Pro（内网地址，绕开隧道 QUIC 坑），
     并在 manifest 记录 sha256 / 本地路径 / Lsky URL / key / 上传时间。
  3. 已上传过的文件：永不重复上传（按内容 sha256 去重）。
  4. 上传满 retention_days（默认 2）天后：删除本地文件（云端副本保留在 Lsky）。
     未到 retention_days：保留本地，但不再上传。

仅删除：manifest 中“已成功上传且超过保留期”的、且确实位于 downloads_dir 内的文件，
不会删除任何未上传/未跟踪的文件。

依赖：仅标准库（http.client / urllib 不可用时的纯实现）。

配置：~/.config/lsky_uploader/secrets.json
  {
    "lsky_base_url": "http://192.168.0.107:8888",
    "token": "<Lsky personal access token>",
    "downloads_dir": "~/Downloads",
    "retention_days": 2,
    "min_age_minutes": 5,        // 跳过最近 N 分钟内修改过的文件（避免上传半截下载）
    "extensions": ["jpg","png", ...]
  }
"""
import os
import sys
import json
import time
import uuid
import mimetypes
import hashlib
import datetime
import logging
import http.client
import urllib.parse

CONFIG_PATH = os.path.expanduser("~/.config/lsky_uploader/secrets.json")
STATE_DIR = os.path.expanduser("~/.config/lsky_uploader")
MANIFEST_PATH = os.path.join(STATE_DIR, "manifest.json")
LOG_PATH = os.path.join(STATE_DIR, "uploader.log")

DEFAULTS = {
    "lsky_base_url": "http://192.168.0.107:8888",
    "token": "",
    "downloads_dir": "~/Downloads",
    "retention_days": 2,
    "min_age_minutes": 5,
    "extensions": ["jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "tif",
                   "tiff", "mp4", "mov", "avi", "mkv", "webm", "m4v", "mpg", "mpeg",
                   "flv", "wmv", "3gp", "ogv"],
}

SKIP_DIR_PREFIXES = (".",)  # 跳过隐藏目录（.Trashes / .com.apple... / 等）


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    cfg["downloads_dir"] = os.path.expanduser(cfg["downloads_dir"])
    return cfg


def setup_logging(dry_run):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    if dry_run:
        logging.info("=== DRY-RUN 模式：不会上传/删除/写 manifest ===")


# --------------------------- 上传（流式 multipart，纯标准库） ---------------------------

class _MultipartBody:
    """按需生成 multipart/form-data 体的流式读取器，避免把大视频整个读进内存。"""

    def __init__(self, file_path, field="file"):
        self.field = field
        self.fn = os.path.basename(file_path)
        self.ct = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.size = os.path.getsize(file_path)
        self.f = open(file_path, "rb")
        self._boundary_str = None
        self.preamble = (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            "Content-Type: %s\r\n\r\n" % (self._boundary(), field, self.fn, self.ct)
        ).encode()
        self.epilogue = ("\r\n--%s--\r\n" % self._boundary()).encode()
        self.length = len(self.preamble) + self.size + len(self.epilogue)
        self._state = 0  # 0 preamble, 1 file, 2 epilogue, 3 done

    def _boundary(self):
        if self._boundary_str is None:
            self._boundary_str = "----b" + uuid.uuid4().hex
        return self._boundary_str

    def read(self, n=-1):
        if n == -1:
            n = 65536
        out = []
        remaining = n
        while remaining > 0 and self._state < 3:
            if self._state == 0:
                take = self.preamble[:remaining]
                out.append(take)
                self.preamble = self.preamble[len(take):]
                if not self.preamble:
                    self._state = 1
                remaining -= len(take)
            elif self._state == 1:
                data = self.f.read(remaining)
                if not data:
                    self._state = 2
                    continue
                out.append(data)
                remaining -= len(data)
            else:  # epilogue
                take = self.epilogue[:remaining]
                out.append(take)
                self.epilogue = self.epilogue[len(take):]
                if not self.epilogue:
                    self._state = 3
                remaining -= len(take)
        return b"".join(out)

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def upload_file(file_path, cfg):
    """上传单个文件到 Lsky。返回 (ok:bool, info:dict)。"""
    base = cfg["lsky_base_url"].rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    use_ssl = parsed.scheme == "https"

    body = _MultipartBody(file_path)
    headers = {
        "Authorization": "Bearer " + cfg["token"],
        "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=" + body._boundary(),
        "Content-Length": str(body.length),
    }
    for attempt in range(3):
        try:
            conn = (http.client.HTTPSConnection if use_ssl else http.client.HTTPConnection)(
                host, port, timeout=600
            )
            conn.request("POST", "/api/v1/upload", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            conn.close()
            body.close()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"status": False, "message": raw[:200]}
            if resp.status == 429:  # 触发每日/每小时额度，今日停止上传
                logging.warning("上传被限流(429)，今日额度用尽，停止本次上传: %s", file_path)
                return False, {"status": False, "message": "rate limited", "_stop": True}
            ok = bool(data.get("status")) and resp.status == 200
            return ok, data
        except Exception as e:  # 网络异常等
            body.close()
            if attempt < 2:
                logging.warning("上传异常 %s，3s 后重试: %s", e, file_path)
                time.sleep(3)
                continue
            return False, {"status": False, "message": str(e)}
    return False, {"status": False, "message": "重试耗尽"}


def delete_remote(key, cfg):
    """从 Lsky 删除已上传的文件（仅测试/清理用，日常不调用）。"""
    base = cfg["lsky_base_url"].rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    use_ssl = parsed.scheme == "https"
    headers = {"Authorization": "Bearer " + cfg["token"], "Accept": "application/json"}
    try:
        conn = (http.client.HTTPSConnection if use_ssl else http.client.HTTPConnection)(
            host, port, timeout=60
        )
        conn.request("DELETE", "/api/v1/images/" + key, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        return bool(json.loads(raw).get("status")) if raw else False
    except Exception as e:
        logging.warning("删除远端失败 %s: %s", key, e)
        return False


# --------------------------- 扫描 / 去重 / 保留期 ---------------------------

def iter_candidate_files(downloads_dir, extensions):
    exts = set(e.lower() for e in extensions)
    for root, dirs, files in os.walk(downloads_dir):
        dirs[:] = [d for d in dirs if not d.startswith(SKIP_DIR_PREFIXES)]
        for name in files:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext in exts:
                yield os.path.join(root, name)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_manifest():
    try:
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_manifest(manifest):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_PATH)


def seed_existing(cfg):
    """把当前 Downloads 里的既有媒体标记为‘忽略’（不上传、不删除），只处理部署后新增的。"""
    downloads_dir = cfg["downloads_dir"]
    manifest = load_manifest()
    path_index = {e.get("path"): k for k, e in manifest.items() if e.get("path")}
    count = 0
    for path in iter_candidate_files(downloads_dir, cfg["extensions"]):
        if path in path_index:
            continue
        key = "pre:" + path
        manifest[key] = {"path": path, "name": os.path.basename(path),
                         "preexisting": True, "uploaded": False, "deleted_local": False}
        path_index[path] = key
        count += 1
    save_manifest(manifest)
    logging.info("已把 %d 个既有文件标记为‘忽略(不上传/不删除)’", count)


def run(cfg, dry_run=False, retention_override=None, upload_all=False):
    retention_days = retention_override if retention_override is not None else cfg["retention_days"]
    min_age_minutes = cfg["min_age_minutes"]
    downloads_dir = cfg["downloads_dir"]
    manifest = load_manifest()

    # 安全护栏：首次运行（manifest 为空）且未显式 --upload-all 时，
    # 自动把当前 Downloads 既有媒体标记为“忽略”，避免一次性上传/删除用户历史文件。
    # dry-run 下不写 manifest（仅预览）。
    if not manifest and not upload_all and not dry_run:
        logging.info("manifest 为空：自动把当前 Downloads 既有媒体标记为‘忽略’（仅处理后续新增）")
        seed_existing(cfg)
        manifest = load_manifest()

    # 路径索引：path -> key（用于“已上传/已忽略”文件不重复计算 sha）
    path_index = {}
    for k, e in manifest.items():
        if e.get("path"):
            path_index[e.get("path")] = k

    now = datetime.datetime.now()
    uploaded_count = 0
    skipped_count = 0
    failed_count = 0
    deleted_count = 0

    logging.info("扫描 %s (扩展名=%d 种, 保留期=%d 天)", downloads_dir, len(cfg["extensions"]), retention_days)
    try:
        os.listdir(downloads_dir)
    except PermissionError:
        logging.error(
            "无法读取 %s：macOS 隐私(TCC)拒绝后台进程访问。请在「系统设置 → 隐私与安全性 → "
            "完全磁盘访问」中添加 /usr/bin/python3（或本脚本），否则定时任务无法扫描 Downloads。",
            downloads_dir,
        )
        return {"uploaded": 0, "skipped": 0, "failed": 0, "deleted": 0}

    candidates = list(iter_candidate_files(downloads_dir, cfg["extensions"]))
    logging.info("发现候选媒体文件 %d 个", len(candidates))

    for path in candidates:
        try:
            st = os.stat(path)
        except OSError:
            continue
        mtime = datetime.datetime.fromtimestamp(st.st_mtime)
        age_min = (now - mtime).total_seconds() / 60.0
        if age_min < min_age_minutes:
            logging.debug("跳过（修改于 %.1f 分钟内，可能未下载完）: %s", age_min, path)
            continue

        existing_sha = path_index.get(path)
        if existing_sha is not None:
            e = manifest[existing_sha]
            if e.get("preexisting"):
                skipped_count += 1
                logging.debug("跳过（既有文件，忽略）: %s", path)
                continue
            if e.get("uploaded") and not e.get("deleted_local"):
                skipped_count += 1
                logging.debug("跳过（已上传且在保留期内）: %s", path)
                continue
            if e.get("uploaded") and e.get("deleted_local"):
                # 内容已在 Lsky，本地已被删；不重复上传
                skipped_count += 1
                continue

        # 新路径：计算 sha，检查内容是否已上传过
        sha = sha256_of(path)
        if sha in manifest:
            e = manifest[sha]
            if e.get("uploaded"):
                # 同内容已在 Lsky（可能换了文件名/路径），更新路径并跳过
                e["path"] = path
                e["name"] = os.path.basename(path)
                e["size"] = st.st_size
                skipped_count += 1
                logging.info("内容已在 Lsky（按 sha 去重），更新路径: %s", path)
                continue

        # 真正的新文件 -> 上传
        if dry_run:
            logging.info("[DRY] 将上传: %s (%.2f MB)", path, st.st_size / 1048576)
            uploaded_count += 1  # 仅统计“将要”
            continue

        ok, data = upload_file(path, cfg)
        if ok:
            d = data.get("data", {})
            manifest[sha] = {
                "path": path,
                "name": os.path.basename(path),
                "size": st.st_size,
                "sha256": sha,
                "lsky_url": d.get("links", {}).get("url", ""),
                "key": d.get("key", ""),
                "uploaded_at": now_iso(),
                "uploaded": True,
                "deleted_local": False,
            }
            uploaded_count += 1
            logging.info("已上传: %s -> %s", path, d.get("links", {}).get("url", ""))
            time.sleep(1)  # 温和限速
        else:
            failed_count += 1
            logging.warning("上传失败: %s | %s", path, data.get("message", ""))
            if data.get("_stop"):
                logging.warning("今日上传额度用尽，停止本次上传，剩余文件留待后续运行")
                break

    # 保留期：删除本地（仅针对已上传且超期）
    if not dry_run:
        for sha, e in list(manifest.items()):
            if not e.get("uploaded") or e.get("deleted_local"):
                continue
            try:
                uploaded_at = datetime.datetime.fromisoformat(e["uploaded_at"])
            except (KeyError, ValueError):
                continue
            age_days = (now - uploaded_at).total_seconds() / 86400.0
            if age_days >= retention_days:
                lp = e.get("path", "")
                if lp and lp.startswith(downloads_dir) and os.path.exists(lp):
                    try:
                        os.remove(lp)
                        e["deleted_local"] = True
                        deleted_count += 1
                        logging.info("已到保留期，删除本地: %s", lp)
                    except OSError as ex:
                        logging.warning("删除本地失败 %s: %s", lp, ex)
                else:
                    # 文件已不在（用户手动删或路径失效）
                    e["deleted_local"] = True

    if not dry_run:
        save_manifest(manifest)

    logging.info("本次结果: 新上传=%d, 跳过(已上传)=%d, 失败=%d, 删本地=%d",
                 uploaded_count, skipped_count, failed_count, deleted_count)
    return {"uploaded": uploaded_count, "skipped": skipped_count,
            "failed": failed_count, "deleted": deleted_count}


def main():
    dry_run = "--dry-run" in sys.argv
    upload_all = "--upload-all" in sys.argv
    retention_override = None
    for a in sys.argv[1:]:
        if a.startswith("--retention-days="):
            try:
                retention_override = float(a.split("=", 1)[1])
            except ValueError:
                pass
    setup_logging(dry_run)
    cfg = load_config()
    if "--seed-existing" in sys.argv:
        if not os.path.isdir(cfg["downloads_dir"]):
            logging.error("downloads_dir 不存在: %s", cfg["downloads_dir"])
            sys.exit(2)
        seed_existing(cfg)
        return
    if not cfg.get("token"):
        logging.error("缺少 Lsky token，请在 %s 配置", CONFIG_PATH)
        sys.exit(2)
    if not os.path.isdir(cfg["downloads_dir"]):
        logging.error("downloads_dir 不存在: %s", cfg["downloads_dir"])
        sys.exit(2)
    run(cfg, dry_run=dry_run, retention_override=retention_override, upload_all=upload_all)


if __name__ == "__main__":
    main()

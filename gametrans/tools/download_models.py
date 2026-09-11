"""离线翻译模型下载器：拉取 NLLB CT2（CTranslate2）模型到本地模型目录。

设计：
- 模型目录见 config.models_dir()，默认 D:/GameTrans/models（可用 GAMETRANS_HOME 改）。
- snapshot_download 支持断点续传，中断后重跑即可继续；下载成功后会自动清掉
  它留在模型目录里的 .cache（中断残留的 .incomplete 可能有数 GB）。
- 国内网络：开梯子直连官方即可；无代理时用 --endpoint https://hf-mirror.com 走镜像。

用法：
    python -m gametrans.tools.download_models                        # 官网下载 600M
    python -m gametrans.tools.download_models --endpoint https://hf-mirror.com  # 走镜像
    python -m gametrans.tools.download_models --dir D:/models --repo <repo_id>
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

# 国内镜像默认值（可用环境变量 HF_ENDPOINT 覆盖）
MODEL_REPOS = {
    # NLLB-200 distilled 600M，CTranslate2 格式（含 model.bin / sentencepiece / config）
    "nllb-200-distilled-600M": "entai2965/nllb-200-distilled-600M-ctranslate2",
    # 1.3B：OpenNMT 官方 CT2 int8 版（model.bin 约 1.3GB，自带分词器）
    "nllb-200-distilled-1.3B": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
}
# CT2 运行需要 model.bin + config.json + vocabulary；
# 分词需要 sentencepiece.bpe.model + tokenizer 配套文件（AutoTokenizer 本地加载用）。
ALLOW_PATTERNS = [
    "model.bin",
    "config.json",
    "sentencepiece.bpe.model",
    "shared_vocabulary.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "README.md",
]


def default_model_dir(variant: str) -> Path:
    from ..config import app_data_dir
    return app_data_dir() / "models" / variant


def download(repo_id: str, local_dir: Path,
             endpoint: str = "https://huggingface.co",
             disable_xet: bool = True) -> bool:
    """下载 CT2 模型文件到 local_dir。返回是否成功。"""
    if not repo_id:
        print("未配置模型仓库 repo_id，请用 --repo 指定。", file=sys.stderr)
        return False
    os.environ["HF_ENDPOINT"] = endpoint
    if disable_xet:
        os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import snapshot_download

    print(f"[download] repo={repo_id}")
    print(f"[download] endpoint={os.environ['HF_ENDPOINT']}")
    print(f"[download] local_dir={local_dir}")
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(local_dir),
            allow_patterns=ALLOW_PATTERNS,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[download] 失败: {exc}", file=sys.stderr)
        return False
    # 校验关键文件
    ok_files = ["model.bin", "config.json", "sentencepiece.bpe.model"]
    missing = [f for f in ok_files if not (local_dir / f).exists()]
    if missing:
        print(f"[download] 缺少文件: {missing}", file=sys.stderr)
        return False
    # 清掉 HF 下载缓存：snapshot_download 会在目标目录下留 .cache，
    # 中断重下时里面会堆积 .incomplete 临时文件（实测可达 2GB+），
    # 与模型本体无关，属于纯垃圾，下载成功后即可删除。
    cache_dir = local_dir / ".cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        print(f"[download] 已清理下载缓存 {cache_dir}")
    print(f"[download] 完成。模型文件就绪于 {local_dir}")
    return True


def download_direct(repo_id: str, local_dir: Path,
                    endpoint: str = "https://huggingface.co",
                    patterns: list | None = None) -> bool:
    """直连 resolve URL 逐文件下载（不依赖 snapshot_download / Xet）。

    为什么需要它：新版 huggingface_hub 对大文件走 Xet/CAS 存储，国内经常连不上；
    即使禁用 Xet，snapshot 的临时文件机制也会在中断后留下 GB 级 .incomplete。
    这里改成最朴素的做法：HEAD 拿大小 → GET + Range 断点续传 → 直接写入目标路径，
    网络抖动后重跑即可接着下。
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    base = endpoint.rstrip("/")
    files = patterns or ALLOW_PATTERNS
    session = requests.Session()
    # 网络抖动自动重试（连接超时/5xx），配合 Range 续传可长期稳定跑完大文件
    retry = Retry(total=5, backoff_factor=2,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["HEAD", "GET"]))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    ok_files: list = []

    for name in files:
        url = f"{base}/{repo_id}/resolve/main/{name}"
        dest = local_dir / name
        total = 0
        try:
            head = session.head(url, allow_redirects=True, timeout=20)
            if head.status_code == 404:
                print(f"[direct] 跳过（仓库无此文件）: {name}")
                continue
            total = int(head.headers.get("Content-Length") or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"[direct] HEAD 失败 {name}: {exc}", file=sys.stderr)

        pos = dest.stat().st_size if dest.exists() else 0
        if total and pos == total:
            print(f"[direct] 已完整: {name} ({pos / 1024 / 1024:.1f} MB)")
            ok_files.append(name)
            continue
        if total and pos > total:  # 上次写入异常，重来
            pos = 0

        # 单文件最多尝试 3 轮（每轮都从当前断点续传），抵抗网络抖动
        finished = False
        for attempt in range(1, 4):
            pos = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": f"bytes={pos}-"} if pos else {}
            try:
                with session.get(url, headers=headers, stream=True, timeout=60) as r:
                    if r.status_code == 404:
                        print(f"[direct] 跳过（仓库无此文件）: {name}")
                        finished = True
                        break
                    if r.status_code == 416:
                        # Range 越界：本地文件已达服务端长度，视为已完整
                        print(f"[direct] 已完整: {name}")
                        finished = True
                        break
                    if r.status_code not in (200, 206):
                        print(f"[direct] {name} HTTP {r.status_code}", file=sys.stderr)
                        time.sleep(2)
                        continue
                    mode = "ab" if (pos and r.status_code == 206) else "wb"
                    if mode == "wb":
                        pos = 0
                    done = pos
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    last_pct = -5
                    with open(dest, mode) as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done * 100 // total
                                if pct - last_pct >= 5:  # 每 5% 打一行，避免刷屏
                                    last_pct = pct
                                    print(f"[direct] {name} {pct}% "
                                          f"({done / 1024 / 1024:.0f}/"
                                          f"{total / 1024 / 1024:.0f} MB)")
            except Exception as exc:  # noqa: BLE001
                print(f"[direct] {name} 第 {attempt} 轮中断: "
                      f"{str(exc)[:90]}（续传重试）", file=sys.stderr)
                time.sleep(2)
                continue

            done = dest.stat().st_size if dest.exists() else 0
            if total and done != total:
                print(f"[direct] {name} 未下完（{done}/{total}），续传重试",
                      file=sys.stderr)
                continue
            finished = True
            break
        if not finished:
            print(f"[direct] {name} 连续失败，稍后可重跑续传", file=sys.stderr)
            return False
        ok_files.append(name)

    # 只校验必需文件；分词器可能是 tokenizer.json 或 sentencepiece.bpe.model
    need = ["model.bin", "config.json"]
    missing = [f for f in need if not (local_dir / f).exists()]
    has_tok = ((local_dir / "tokenizer.json").exists()
               or (local_dir / "sentencepiece.bpe.model").exists())
    if missing or not has_tok:
        print(f"[direct] 缺少文件: {missing or '分词器'}", file=sys.stderr)
        return False
    print(f"[direct] 完成。模型就绪于 {local_dir}（{len(ok_files)} 个文件）")
    return True


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 NLLB 本地翻译模型")
    parser.add_argument("--variant", default="nllb-200-distilled-600M",
                        choices=list(MODEL_REPOS))
    parser.add_argument("--dir", default="", help="目标目录（默认 AppData/models/<variant>）")
    parser.add_argument("--repo", default="", help="覆盖模型仓库 repo_id")
    parser.add_argument("--endpoint",
                        default="https://huggingface.co",
                        help="HF 端点：官网或镜像（直连用官网，无代理时用 https://hf-mirror.com）")
    parser.add_argument("--no-disable-xet", action="store_true",
                        help="不禁用 Xet（开代理且代理可访问 AWS 时更稳定）")
    parser.add_argument("--direct", action="store_true",
                        help="直连 resolve URL 逐文件下载（断点续传，最稳；推荐网络不佳时使用）")
    args = parser.parse_args(argv)

    repo = args.repo or MODEL_REPOS.get(args.variant, "")
    local_dir = Path(args.dir) if args.dir else default_model_dir(args.variant)
    if args.direct:
        os.environ.setdefault("HF_ENDPOINT", args.endpoint)
        local_dir.mkdir(parents=True, exist_ok=True)
        print(f"[direct] repo={repo} endpoint={args.endpoint} dir={local_dir}")
        return 0 if download_direct(repo, local_dir, args.endpoint) else 1
    return 0 if download(repo, local_dir,
                         endpoint=args.endpoint,
                         disable_xet=not args.no_disable_xet) else 1


if __name__ == "__main__":
    sys.exit(main())

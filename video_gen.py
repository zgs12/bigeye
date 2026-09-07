"""
大眼视频生成器 — 智谱 CogVideoX-3 真 AI 视频。
支持：文生视频、图生视频（image_url + prompt）、首尾帧。
异步接口：提交任务 → 轮询 async-result → 下载 mp4 落盘。
价格：约 1 元 / 次（以智谱平台为准）。
"""

import os, json, random, time, requests
from datetime import datetime

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "generated", "videos")
os.makedirs(OUTPUT_DIR, exist_ok=True)

BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


def _load_video_config():
    """读取视频配置。key 优先 video_api_key，退回 image_api_key（同一智谱账号）。"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {
        "api_key": cfg.get("video_api_key") or cfg.get("image_api_key", ""),
        "model": cfg.get("video_model", "cogvideox-3"),
    }


def _headers(key):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def submit_video(prompt: str, image_url: str = None, start_image_url: str = None,
                 end_image_url: str = None, with_audio: bool = True,
                 size: str = "1920x1080", fps: int = 30, quality: str = "quality",
                 model: str = None) -> dict:
    """提交视频生成任务（异步）。返回 {"task_id": "..."} 或 {"error": "..."}。"""
    cfg = _load_video_config()
    key = cfg["api_key"]
    if not key:
        return {"error": "未配置智谱 API key（model_config.json 的 image_api_key / video_api_key）"}
    try:
        payload = {
            "model": model or cfg["model"],
            "quality": quality,
            "with_audio": with_audio,
            "size": size,
            "fps": fps,
        }
        if image_url:
            # 图生视频：image_url + prompt 描述动态
            payload["image_url"] = image_url
            if not prompt:
                prompt = "让画面自然动起来，保持主体与风格一致，细节稳定，镜头缓慢移动。"
        if start_image_url or end_image_url:
            payload["image_url"] = start_image_url
            payload["end_image_url"] = end_image_url
        payload["prompt"] = prompt

        resp = requests.post(f"{BASE_URL}/videos/generations",
                             headers=_headers(key), json=payload, timeout=60)
        if resp.status_code != 200:
            return {"error": f"提交失败 HTTP {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        task_id = data.get("id")
        if not task_id:
            return {"error": f"响应无任务 id: {str(data)[:300]}"}
        return {"task_id": task_id, "raw": data}
    except Exception as e:
        return {"error": f"提交异常: {e}"}


def poll_video(task_id: str, timeout: int = 300, interval: int = 5) -> dict:
    """轮询异步任务直到完成或超时。返回 {"status": "success|processing|failed|timeout", ...}。"""
    cfg = _load_video_config()
    key = cfg["api_key"]
    if not key:
        return {"status": "failed", "error": "未配置智谱 API key"}
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/async-result/{task_id}",
                                headers=_headers(key), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                last = data
                # 智谱异步结果: task_status = SUCCESS/PROCESSING/FAIL
                st = str(data.get("task_status", "")).upper()
                if st in ("SUCCESS", "SUCCEED", "SUCCEEDED"):
                    video_url = (data.get("video_result") or [{}])[0].get("url") \
                        if isinstance(data.get("video_result"), list) \
                        else data.get("video_url") or data.get("url")
                    return {"status": "success", "video_url": video_url, "raw": data}
                if st in ("FAIL", "FAILED", "ERROR"):
                    return {"status": "failed", "error": str(data.get("error", data))[:300], "raw": data}
            elif resp.status_code == 404:
                return {"status": "failed", "error": f"任务 {task_id} 不存在（可能过期）"}
        except Exception as e:
            last = {"poll_error": str(e)}
        time.sleep(interval)
    return {"status": "timeout", "last": last, "hint": f"生成超时（>{timeout}s），可稍后用 task_id={task_id} 继续查询"}


def download_video(video_url: str, output_dir: str = None) -> str:
    """下载视频文件。返回文件名（output_dir 模式）或 /generated/videos/xxx.mp4 URL。"""
    resp = requests.get(video_url, timeout=120)
    if resp.status_code != 200:
        return ""
    filename = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000,9999)}.mp4"
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return filename
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(resp.content)
    return f"/generated/videos/{filename}"


def generate_video(prompt: str, image_url: str = None, output_dir: str = None,
                   wait_timeout: int = 300, with_audio: bool = True,
                   size: str = "1920x1080", fps: int = 30) -> dict:
    """完整流程：提交 → 轮询 → 下载。返回 dict。"""
    sub = submit_video(prompt, image_url=image_url, with_audio=with_audio, size=size, fps=fps)
    if "error" in sub:
        return sub
    task_id = sub["task_id"]
    print(f"[video_gen] 任务已提交: {task_id}，开始轮询…")
    res = poll_video(task_id, timeout=wait_timeout)
    if res["status"] != "success":
        res["task_id"] = task_id
        return res
    video_url = res.get("video_url")
    if not video_url:
        return {"status": "failed", "task_id": task_id, "error": "成功但未取到视频 URL", "raw": res.get("raw")}
    saved = download_video(video_url, output_dir)
    return {
        "status": "success",
        "task_id": task_id,
        "video_url": video_url,
        "file": saved,
        "size": size,
        "fps": fps,
    }


def generate_video_from_json(data: dict) -> dict:
    """JSON RPC 入口，供 tools/video_gen_tool.py 调用。"""
    try:
        prompt = (data.get("prompt") or "").strip()
        if not prompt and not data.get("image_url"):
            return {"error": "prompt（或 image_url）必填"}
        return generate_video(
            prompt=prompt,
            image_url=data.get("image_url"),
            output_dir=data.get("output_dir") or None,
            wait_timeout=data.get("wait_timeout", 300),
            with_audio=bool(data.get("with_audio", True)),
            size=data.get("size", "1920x1080"),
            fps=data.get("fps", 30),
        )
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import sys
    # 命令行自测：python video_gen.py "文字描述" [image_url] [output_dir]
    p = sys.argv[1] if len(sys.argv) > 1 else "一只橘猫在草地上追蝴蝶，阳光明媚，镜头缓缓推进"
    img = sys.argv[2] if len(sys.argv) > 2 else None
    out = sys.argv[3] if len(sys.argv) > 3 else None
    r = generate_video(p, image_url=img, output_dir=out)
    print(json.dumps(r, ensure_ascii=False, indent=1))

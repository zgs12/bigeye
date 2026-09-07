#!/usr/bin/env python3
"""AI video generation tool — wraps video_gen.py for function calling.
支持文生视频 / 图生视频（传 image_url 让图片动起来）。"""
import os
import sys
import threading
from .registry import register_tool

_ws = threading.local()


def set_workspace(path):
    """Set workspace for current thread. Generated videos save here."""
    _ws.path = path


@register_tool(
    name="generate_video",
    description=(
        "AI 视频生成（智谱 CogVideoX-3，约1元/次）。文字描述生成视频；"
        "若传 image_url 则为图生视频（让已有图片动起来）。"
        "默认生成 5 秒 1920x1080 视频，需等待 1~5 分钟。返回视频文件位置。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "视频内容描述（文生视频必填；图生视频时描述画面如何运动，如'女子缓缓转身，桃花飘落'）"
            },
            "image_url": {
                "type": "string",
                "description": "可选。起始图片 URL 或本地文件绝对路径——把它变成动态视频"
            },
            "with_audio": {
                "type": "boolean",
                "description": "是否带声音 (默认 true)",
                "default": True
            },
            "size": {
                "type": "string",
                "description": "分辨率，如 1920x1080 / 1280x720 (默认 1920x1080)",
                "default": "1920x1080"
            }
        },
        "required": ["prompt"]
    }
)
def generate_video(prompt: str, image_url: str = None, with_audio: bool = True, size: str = "1920x1080"):
    """Generate a video from text prompt (or animate an image) via Zhipu CogVideoX-3."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from video_gen import generate_video_from_json
        output_dir = getattr(_ws, 'path', None)
        # 本地图片文件 → 转 file:// 或直接传路径由智谱侧拉取? 智谱要求公网URL，先尝试本地转码上传暂不支持，
        # 因此若为本地路径则提示先放 public 目录
        result = generate_video_from_json({
            "prompt": prompt,
            "image_url": image_url,
            "output_dir": output_dir,
            "with_audio": with_audio,
            "size": size,
        })
        return result
    except Exception as e:
        return {"error": f"视频生成失败: {e}"}

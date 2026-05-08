import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import torch
from PIL import Image

import folder_paths


def safe_name(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"[^\w\-.一-龥 ]+", "_", text)
    text = text.replace(" ", "_")
    return text or "gif"


def format_frame_numbers(start: int, end: int) -> str:
    """
    把帧号范围格式化成逗号分隔：
    1..3 -> 1,2,3
    59..61 -> 59,60,61
    """
    if start > end:
        return "none"
    return ",".join(str(i) for i in range(start, end + 1))


def find_ffmpeg():
    """
    优先使用系统 ffmpeg。
    macOS App 启动时可能拿不到 /opt/homebrew/bin，所以手动补常见路径。
    如果都找不到，再尝试 imageio-ffmpeg。
    """
    candidates = [
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]

    for ffmpeg in candidates:
        if ffmpeg and Path(ffmpeg).exists():
            return ffmpeg

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    raise RuntimeError(
        "找不到 ffmpeg。请先安装 ffmpeg，或在 ComfyUI 的 Python 环境里安装 imageio-ffmpeg。"
    )


def run_cmd(cmd):
    """
    不使用 shell=True，避免 shell 字符串解释。
    """
    cmd = [str(x) for x in cmd]
    print("[LoopGif] RUN:", " ".join(cmd))

    try:
        return subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
    except subprocess.CalledProcessError as e:
        print("[LoopGif] ffmpeg stdout:")
        print(e.stdout)
        print("[LoopGif] ffmpeg stderr:")
        print(e.stderr)
        raise


def images_to_tensor(image_paths):
    """
    把 picked 帧读入内存，转换为 ComfyUI IMAGE batch。
    输出形状：[N, H, W, 3]，float32，范围 0~1。
    """
    tensors = []

    for path in image_paths:
        img = Image.open(path).convert("RGB")
        arr = np.asarray(img).astype(np.float32) / 255.0
        tensors.append(torch.from_numpy(arr))

    if not tensors:
        raise RuntimeError("没有可输出的 picked images。")

    return torch.stack(tensors, dim=0)


def tensor_images_to_png_files(images, out_dir: Path):
    """
    把内存中的 IMAGE / 视频帧 tensor 写成 png 序列。
    兼容 [N,H,W,C] 和 [H,W,C]。
    返回写出的文件路径列表。
    """
    if images is None:
        raise RuntimeError("Video components 中没有 images。")

    if not isinstance(images, torch.Tensor):
        images = torch.as_tensor(images)

    if images.ndim == 3:
        images = images.unsqueeze(0)

    if images.ndim != 4:
        raise RuntimeError(f"images 维度不正确，期望 [N,H,W,C]，实际为 {tuple(images.shape)}")

    files = []
    images = images.detach().cpu().float().clamp(0.0, 1.0)

    for i in range(images.shape[0]):
        arr = (images[i].numpy() * 255.0).round().clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        path = out_dir / f"frame_{i+1:03d}.png"
        img.save(path)
        files.append(path)

    return files


def apply_color_drift_correction(image_paths, color_drift_strength: float):
    """
    循环颜色漂移补偿。

    作用：
    - 在删除首尾帧之后，对最终参与循环的帧序列做颜色漂移校正
    - 不管 delete_ratio 是否为 0，只要 color_drift_strength > 0 就会生效
    - 主要用于修正“最后一帧整体偏色，跳回第一帧时闪一下”的问题

    做法：
    - 计算第一帧和最后一帧的 RGB 平均颜色差
    - 从第一帧到最后一帧逐渐增加修正量
    - 第一帧不动
    - 越靠后的帧修正越多
    - correction 内部带 4.0 放大系数，方便让面板参数更容易看到效果
    """
    try:
        strength = float(color_drift_strength)
    except Exception:
        strength = 0.0

    # 只限制不能为负，不限制最大值
    strength = max(0.0, strength)

    if strength <= 0:
        return 0.0

    count = len(image_paths)
    if count < 2:
        return 0.0

    first_img = Image.open(image_paths[0]).convert("RGB")
    last_img = Image.open(image_paths[-1]).convert("RGB")

    first_arr = np.asarray(first_img).astype(np.float32)
    last_arr = np.asarray(last_img).astype(np.float32)

    first_mean = first_arr.reshape(-1, 3).mean(axis=0)
    last_mean = last_arr.reshape(-1, 3).mean(axis=0)

    # 需要逐渐补偿的颜色差
    drift = first_mean - last_mean

    print(
        "[LoopGif] color_drift_strength=",
        strength,
        "first_mean=",
        first_mean,
        "last_mean=",
        last_mean,
        "drift=",
        drift,
        "effective_last_correction=",
        drift * strength * 4.0,
    )

    for i, path in enumerate(image_paths):
        if count == 1:
            t = 0.0
        else:
            t = float(i) / float(count - 1)

        correction = drift * t * strength * 4.0

        img = Image.open(path).convert("RGB")
        arr = np.asarray(img).astype(np.float32)
        arr = arr + correction.reshape(1, 1, 3)
        arr = np.clip(arr, 0, 255).astype(np.uint8)

        Image.fromarray(arr).save(path)

    return strength


def apply_loop_blend(image_paths, blend_frames: int, blend_strength: float):
    """
    在删除首尾帧之后，对新的首尾几帧做对称式循环叠化。

    做法：
    - 不增加帧数
    - 不删除帧
    - 同时修改头部 blend_frames 帧和尾部 blend_frames 帧
    - 越靠近循环接缝，混合越强
    - 越远离循环接缝，混合越弱
    """
    try:
        blend_frames = int(blend_frames)
    except Exception:
        blend_frames = 0

    try:
        blend_strength = float(blend_strength)
    except Exception:
        blend_strength = 0.0

    blend_strength = max(0.0, min(blend_strength, 1.0))

    if blend_frames <= 0 or blend_strength <= 0:
        return 0

    count = len(image_paths)
    if count < 4:
        return 0

    # 避免叠化范围过大，最多只处理一半以内
    blend_count = min(blend_frames, count // 2)

    if blend_count <= 0:
        return 0

    print(f"[LoopGif] blend_frames={blend_count}, blend_strength={blend_strength}")

    # 先把参与叠化的原始头尾帧读进内存，避免前面改了以后影响后面计算
    head_images = []
    tail_images = []

    for i in range(blend_count):
        head_images.append(Image.open(image_paths[i]).convert("RGB"))
        tail_images.append(Image.open(image_paths[count - blend_count + i]).convert("RGB"))

    # 修改头部：
    for i in range(blend_count):
        head_img = head_images[i]
        tail_img = tail_images[blend_count - 1 - i]
        alpha = blend_strength * float(blend_count - i) / float(blend_count)

        blended = Image.blend(head_img, tail_img, alpha)
        blended.save(image_paths[i])

    # 修改尾部：
    for i in range(blend_count):
        tail_path_index = count - blend_count + i
        tail_img = tail_images[i]
        head_img = head_images[blend_count - 1 - i]
        alpha = blend_strength * float(i + 1) / float(blend_count)

        blended = Image.blend(tail_img, head_img, alpha)
        blended.save(image_paths[tail_path_index])

    return blend_count


def resolve_video_components(video):
    """
    尝试从 ComfyUI VIDEO 对象里提取内存视频组件：
    - VideoFromComponents
    - 其他带 components / images / frame_rate 的对象
    """
    for method_name in ["get_components"]:
        method = getattr(video, method_name, None)
        if callable(method):
            try:
                comps = method()
                if comps is not None and hasattr(comps, "images"):
                    print(f"[LoopGif] {method_name}() -> components found")
                    return comps
            except Exception as e:
                print(f"[LoopGif] {method_name}() failed:", repr(e))

    for attr_name in ["components", "_components"]:
        if hasattr(video, attr_name):
            try:
                comps = getattr(video, attr_name)
                if comps is not None and hasattr(comps, "images"):
                    print(f"[LoopGif] attr {attr_name} -> components found")
                    return comps
            except Exception as e:
                print(f"[LoopGif] attr {attr_name} failed:", repr(e))

    obj_dict = getattr(video, "__dict__", None)
    if isinstance(obj_dict, dict):
        private_key = "_VideoFromComponents__components"
        if private_key in obj_dict:
            comps = obj_dict[private_key]
            if comps is not None and hasattr(comps, "images"):
                print(f"[LoopGif] __dict__ {private_key} -> components found")
                return comps

    return None


def resolve_video_path(video):
    """
    兼容常见 ComfyUI VIDEO 对象：
    - str / Path 路径
    - dict: path / fullpath / video_path / file_path
    - dict: filename + subfolder + type
    - object: path / filename / file / video_path 等属性
    - object: get_stream_source() / get_path() / get_file() 等方法

    注意：
    这里只解析“能落到磁盘路径”的 VIDEO。
    如果是 VideoFromComponents，应走 resolve_video_components。
    """

    def as_existing_path(value):
        if value is None:
            return None

        if isinstance(value, (bytes, bytearray)):
            return None

        try:
            p = Path(str(value)).expanduser()
            if p.exists():
                return str(p.resolve())
        except Exception:
            pass

        return None

    def resolve_from_filename_info(filename, subfolder="", file_type="output"):
        if not filename:
            return None

        if file_type == "input":
            base = folder_paths.get_input_directory()
        elif file_type == "temp":
            base = folder_paths.get_temp_directory()
        else:
            base = folder_paths.get_output_directory()

        p = Path(base) / str(subfolder) / str(filename)
        if p.exists():
            return str(p.resolve())

        return None

    print("[LoopGif] video object type:", type(video))
    print("[LoopGif] video object repr:", repr(video))

    path = as_existing_path(video)
    if path:
        return path

    if isinstance(video, dict):
        print("[LoopGif] video dict keys:", list(video.keys()))

        for key in ["path", "fullpath", "video_path", "file_path", "filename"]:
            path = as_existing_path(video.get(key))
            if path:
                return path

        filename = video.get("filename")
        subfolder = video.get("subfolder", "")
        file_type = video.get("type", "output")

        path = resolve_from_filename_info(filename, subfolder, file_type)
        if path:
            return path

        for value in video.values():
            path = as_existing_path(value)
            if path:
                return path

            if isinstance(value, dict):
                filename = value.get("filename")
                subfolder = value.get("subfolder", "")
                file_type = value.get("type", "output")
                path = resolve_from_filename_info(filename, subfolder, file_type)
                if path:
                    return path

    if isinstance(video, (list, tuple)):
        for item in video:
            try:
                return resolve_video_path(item)
            except Exception:
                pass

    for method_name in [
        "get_stream_source",
        "get_path",
        "get_file",
        "get_filename",
        "get_video_path",
    ]:
        method = getattr(video, method_name, None)
        if callable(method):
            try:
                value = method()
                print(f"[LoopGif] {method_name}() ->", repr(value))

                path = as_existing_path(value)
                if path:
                    return path

                if isinstance(value, dict):
                    return resolve_video_path(value)

            except Exception as e:
                print(f"[LoopGif] {method_name}() failed:", repr(e))

    for attr_name in [
        "path",
        "fullpath",
        "file_path",
        "video_path",
        "filename",
        "file",
        "name",
        "_path",
        "_file",
        "_filename",
        "__file",
    ]:
        if hasattr(video, attr_name):
            try:
                value = getattr(video, attr_name)
                print(f"[LoopGif] attr {attr_name} ->", repr(value))

                path = as_existing_path(value)
                if path:
                    return path

            except Exception as e:
                print(f"[LoopGif] attr {attr_name} failed:", repr(e))

    obj_dict = getattr(video, "__dict__", None)
    if isinstance(obj_dict, dict):
        print("[LoopGif] video object __dict__ keys:", list(obj_dict.keys()))

        for key, value in obj_dict.items():
            print(f"[LoopGif] __dict__ {key} ->", repr(value))

            path = as_existing_path(value)
            if path:
                return path

            if isinstance(value, dict):
                try:
                    return resolve_video_path(value)
                except Exception:
                    pass

    raise RuntimeError(
        "无法解析视频路径。当前 VIDEO 输入对象不兼容，且未找到可直接读取的文件路径。"
    )


def next_output_path(output_dir: Path, prefix: str) -> Path:
    """
    模仿 ComfyUI 保存节点的简洁命名：
    gif_00001_.gif
    gif_00002_.gif
    """
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d{{5}})_\.gif$")
    max_index = 0

    for f in output_dir.glob(f"{prefix}_*.gif"):
        m = pattern.match(f.name)
        if m:
            max_index = max(max_index, int(m.group(1)))

    index = max_index + 1
    return output_dir / f"{prefix}_{index:05d}_.gif"


class LoopGif:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "filename_prefix": ("STRING", {
                    "default": "gif",
                    "multiline": False,
                }),
                "delete_ratio": ("FLOAT", {
                    "default": 0.10,
                    "min": 0.0,
                    "max": 0.8,
                    "step": 0.01,
                    "display": "number",
                }),
                "gif_fps": ("FLOAT", {
                    "default": 24.0,
                    "min": 1.0,
                    "max": 60.0,
                    "step": 0.01,
                    "display": "number",
                }),
                "blend_frames": ("INT", {
                    "default": 2,
                    "min": 0,
                    "max": 8,
                    "step": 1,
                }),
                "blend_strength": ("FLOAT", {
                    "default": 0.10,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "display": "number",
                }),
                "color_drift_strength": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 3.0,
                    "step": 0.01,
                    "display": "number",
                }),
            },
        }

    @classmethod
    def IS_CHANGED(
        cls,
        video,
        filename_prefix,
        delete_ratio,
        gif_fps,
        blend_frames,
        blend_strength,
        color_drift_strength,
    ):
        import time
        return time.time()

    RETURN_TYPES = ("STRING", "IMAGE", "FLOAT")
    RETURN_NAMES = ("gif_path", "images", "frame_rate")
    FUNCTION = "make_gif"
    CATEGORY = "video/gif"
    OUTPUT_NODE = True

    def make_gif(
        self,
        video,
        filename_prefix,
        delete_ratio,
        gif_fps,
        blend_frames,
        blend_strength,
        color_drift_strength,
    ):
        ffmpeg = find_ffmpeg()
        prefix = safe_name(filename_prefix)

        output_dir = Path(folder_paths.get_output_directory())
        output_path = next_output_path(output_dir, prefix)

        work_dir = output_dir / f".{output_path.stem}_work"
        frames_dir = work_dir / "frames"
        picked_dir = work_dir / "picked"
        palette_path = work_dir / "palette.png"

        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

        frames_dir.mkdir(parents=True, exist_ok=True)
        picked_dir.mkdir(parents=True, exist_ok=True)

        picked_count = 0
        picked_images = None
        actual_blend_frames = 0
        actual_color_drift_strength = 0.0

        try:
            components = resolve_video_components(video)
            input_path = None

            if components is not None and hasattr(components, "images"):
                print("[LoopGif] using VideoFromComponents / in-memory frames")
                frame_files = tensor_images_to_png_files(components.images, frames_dir)
                print(f"[LoopGif] frames from components: {len(frame_files)}")
            else:
                input_path = resolve_video_path(video)
                print(f"[LoopGif] input: {input_path}")
                print(f"[LoopGif] output: {output_path}")

                run_cmd([
                    ffmpeg,
                    "-y",
                    "-i", input_path,
                    "-fps_mode", "passthrough",
                    str(frames_dir / "frame_%03d.png"),
                ])
                frame_files = sorted(frames_dir.glob("frame_*.png"))

            print(f"[LoopGif] output: {output_path}")

            count = len(frame_files)
            if count < 4:
                raise RuntimeError(f"视频帧数太少：{count}，无法生成循环 GIF。")

            delete_ratio = float(delete_ratio)

            # 按 delete_ratio 删除首尾帧，不做首尾调换
            # delete_ratio 近似为 0 时，不删帧
            if delete_ratio <= 0.0001:
                del_total = 0
                del_head = 0
                del_tail = 0
            else:
                del_total = round(count * delete_ratio)
                del_total = max(1, del_total)
                del_total = min(del_total, count - 3)

                del_head = del_total // 2
                del_tail = del_total - del_head

            start_index = del_head
            end_index = count - del_tail
            picked = frame_files[start_index:end_index]

            print(
                f"[LoopGif] delete info: "
                f"source_frames={count}, "
                f"delete_ratio={delete_ratio}, "
                f"delete_total={del_total}, "
                f"delete_head={del_head}, "
                f"delete_tail={del_tail}, "
                f"kept_original_index={start_index + 1}..{end_index}, "
                f"kept_frames={len(picked)}"
            )

            if del_head > 0:
                print(
                    "[LoopGif] deleted head original frames:",
                    format_frame_numbers(1, del_head),
                )
            else:
                print("[LoopGif] deleted head original frames: none")

            if del_tail > 0:
                print(
                    "[LoopGif] deleted tail original frames:",
                    format_frame_numbers(count - del_tail + 1, count),
                )
            else:
                print("[LoopGif] deleted tail original frames: none")

            picked_output_files = []
            for i, src in enumerate(picked, start=1):
                dst = picked_dir / f"pick_{i:03d}.png"
                shutil.copy2(src, dst)
                picked_output_files.append(dst)

            picked_count = len(picked_output_files)

            # 删除首尾帧之后，对最终参与循环的帧做颜色漂移补偿
            # 不管 delete_ratio 是否为 0，只要 color_drift_strength > 0 就会生效
            actual_color_drift_strength = apply_color_drift_correction(
                picked_output_files,
                color_drift_strength,
            )

            # 只有发生首尾删除后，才对新的首尾帧做对称式渐进叠化
            # delete_ratio=0 时不做叠化，避免破坏原始首尾结构
            if del_total > 0:
                actual_blend_frames = apply_loop_blend(
                    picked_output_files,
                    blend_frames,
                    blend_strength,
                )
            else:
                actual_blend_frames = 0

            picked_images = images_to_tensor(picked_output_files)

            run_cmd([
                ffmpeg,
                "-y",
                "-framerate", str(float(gif_fps)),
                "-i", str(picked_dir / "pick_%03d.png"),
                "-vf", "palettegen",
                "-frames:v", "1",
                "-update", "1",
                str(palette_path),
            ])

            run_cmd([
                ffmpeg,
                "-y",
                "-framerate", str(float(gif_fps)),
                "-i", str(picked_dir / "pick_%03d.png"),
                "-i", str(palette_path),
                "-lavfi", "[0:v][1:v]paletteuse",
                "-loop", "0",
                str(output_path),
            ])

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        print(f"[LoopGif] done: {output_path}")

        frame_rate = float(gif_fps)

        return {
            "ui": {
                "text": [
                    f"GIF saved: {output_path.name}",
                    f"delete_ratio={delete_ratio}",
                    f"gif_fps={frame_rate}",
                    f"picked_frames={picked_count}",
                    f"blend_frames={actual_blend_frames}",
                    f"blend_strength={blend_strength}",
                    f"color_drift_strength={actual_color_drift_strength}",
                ],
            },
            "result": (str(output_path), picked_images, frame_rate),
        }


NODE_CLASS_MAPPINGS = {
    "LoopGif": LoopGif,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoopGif": "Loop GIF"
}
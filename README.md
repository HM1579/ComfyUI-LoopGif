# ComfyUI-LoopGif

A simple ComfyUI custom node for creating looping GIFs from VIDEO inputs.

This node is mainly designed for short AI-generated videos, especially image-to-video workflows where the first and last frames are expected to be similar. It can trim frames from the beginning and end, apply mild loop blending, and optionally compensate for gradual color drift before exporting a GIF.

## Features

- Convert ComfyUI `VIDEO` input to looping GIF
- Supports video file based inputs
- Supports in-memory video components, such as videos created inside a workflow
- Output GIF path as `STRING`
- Output processed frames as `IMAGE`
- Output GIF frame rate as `FLOAT`
- Optional head/tail frame trimming
- Optional loop seam blending
- Optional gradual color drift correction
- Automatically cleans temporary working files after execution

## Installation

Clone this repository into your ComfyUI `custom_nodes` folder:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/HM1579/ComfyUI-LoopGif.git
```

Then install dependencies into your ComfyUI Python environment.

If you are using a normal ComfyUI installation with a virtual environment, activate it first:

```bash
cd ComfyUI
source .venv/bin/activate
cd custom_nodes/ComfyUI-LoopGif
python -m pip install -r requirements.txt
```

If you are using ComfyUI Desktop or another packaged ComfyUI version, use the Python executable that ComfyUI actually runs with. You can find it in the ComfyUI startup log, for example:

```text
** Python executable: /path/to/ComfyUI/.venv/bin/python
```

Then run:

```bash
/path/to/ComfyUI/.venv/bin/python -m pip install -r requirements.txt
```

Restart ComfyUI after installation.

## Requirements

This node requires:

- numpy
- Pillow
- imageio-ffmpeg

The node will try to use system `ffmpeg` first. If system `ffmpeg` is not found, it will fall back to `imageio-ffmpeg`.

On macOS, the node also checks common Homebrew paths:

```text
/opt/homebrew/bin/ffmpeg
/usr/local/bin/ffmpeg
```

## Node

### Loop GIF

Category:

```text
video/gif
```

Inputs:

| Name | Type | Description |
|---|---|---|
| video | VIDEO | Input video from ComfyUI workflow |
| filename_prefix | STRING | Output GIF filename prefix |
| delete_ratio | FLOAT | Ratio of frames to remove from the beginning and end |
| gif_fps | FLOAT | GIF frame rate |
| blend_frames | INT | Number of frames near the loop seam to blend |
| blend_strength | FLOAT | Strength of loop seam blending |
| color_drift_strength | FLOAT | Strength of gradual color drift correction |

Outputs:

| Name | Type | Description |
|---|---|---|
| gif_path | STRING | Saved GIF file path |
| images | IMAGE | Processed frame sequence |
| frame_rate | FLOAT | Output frame rate |

## Parameter Guide

### delete_ratio

Controls how many frames are removed from the beginning and end of the video.

This is usually the most important parameter for making a short video loop better.

Recommended range:

```text
0.05 - 0.15
```

Suggested default:

```text
0.10
```

If the loop has a long pause near the seam, increase this value slightly.  
If the loop feels too jumpy, reduce it slightly.

### gif_fps

Controls the playback speed of the exported GIF.

Recommended values:

```text
18 - 24
```

Higher FPS may look smoother, but also produces larger GIF files.

### blend_frames

Controls how many frames near the loop seam are blended.

Recommended range:

```text
0 - 2
```

This is a minor helper parameter. It may reduce small seam flickers, but high values can cause ghosting or unnatural transitions.

### blend_strength

Controls the strength of the loop seam blending.

Recommended range:

```text
0.05 - 0.20
```

Suggested default:

```text
0.10
```

Too high values may introduce visible ghosting.

### color_drift_strength

Compensates for gradual color drift across the video.

This can help when the final frame has a different overall color tone from the first frame, causing a visible flash when the GIF loops.

Recommended range:

```text
0.0 - 1.5
```

Suggested default:

```text
0.0
```

This parameter is experimental. It may help in some cases, but it is not a universal fix.

## Notes

This node is designed as a lightweight practical tool rather than a complex video post-processing system.

For most workflows, the recommended tuning order is:

1. Adjust `delete_ratio`
2. Adjust `gif_fps`
3. Optionally enable small `blend_frames`
4. Optionally adjust `color_drift_strength` if there is visible color drift

In many cases, `delete_ratio` alone is enough.

## Temporary Files

The node creates a temporary hidden working folder during execution, such as:

```text
.output_name_work
```

This folder is automatically removed after execution, whether the process succeeds or fails.

## Limitations

- GIF output does not preserve audio
- Large videos may consume more memory when outputting processed frames as `IMAGE`
- Color drift correction is experimental
- Loop quality still depends heavily on the source video and prompt quality

## License

MIT License
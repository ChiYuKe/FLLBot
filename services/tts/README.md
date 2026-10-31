# TTS 本地服务（IndexTTS-2.5）

给 FLLBot 的 `plugins/voice.py` 提供语音合成。独立进程运行，避免把 torch 和模型塞进 bot。

## 目录

```text
services/tts/
├── server.py          # FastAPI 服务（本仓库代码）
├── start.ps1          # 启动脚本
├── voices/            # 参考音频（自己放，已被 gitignore）
├── checkpoints/       # IndexTTS-2.5 权重（被 gitignore）
├── cache/             # 合成结果缓存（被 gitignore）
└── vendor/IndexTTS/   # 官方仓库 + 独立 venv（被 gitignore）
```

## 安装

```powershell
# 1. 克隆官方仓库（已做过可跳过）
git clone --depth 1 https://github.com/index-tts/index-tts.git services\tts\vendor\IndexTTS

# 2. 建环境（Python 3.11 + CUDA 12.8 版 torch，5070 Ti 必需）
cd services\tts\vendor\IndexTTS
uv sync --no-dev

# 3. 下载权重（5.1 GB，国内建议走 ModelScope）
uv run modelscope download --model IndexTeam/IndexTTS-2.5 --local_dir ..\..\checkpoints

# 4. 服务需要的 Web 依赖（官方仓库没带）
uv pip install "fastapi>=0.115" "uvicorn[standard]>=0.30"
```

> **中文路径注意**：wetext 依赖的 kaldifst 在 Windows 上打不开非 ASCII 路径，会报
> `Error opening input stream ... traditional_to_simple.fst`。`start.ps1` 检测到路径含中文时，
> 会自动在 `%LOCALAPPDATA%\fllbot-tts` 建 junction 再从那边启动，不需要手动处理。

## 放音色

把 5–15 秒干净人声放到 `voices/`，文件名就是插件里配置的音色名：

```text
voices/dongxuelian-soft.wav  ← 插件默认（杂谈切片，语气平稳）
voices/dongxuelian.wav       ← 锐评切片，语气更冲
voices/default.wav           ← 找不到时的回落
```

要求：无 BGM、无混响、情绪平稳、采样率 16k 以上。直播切片先用 UVR5 去伴奏。

仓库自带的 `vendor/IndexTTS/examples/voice_01.wav` 可以先用 `default.wav` 顶上，先把链路跑通。

## 启动

```powershell
services\tts\start.ps1
# 或指定参数
services\tts\start.ps1 --port 7861 --no-qwen-emo

# 停止
services\tts\stop.ps1
```

启动后：

```powershell
curl.exe http://127.0.0.1:7861/health
curl.exe http://127.0.0.1:7861/voices
```

首次启动要加载模型 + 预热，约 30–60 秒；`/health` 里 `ok=true` 才算就绪。

默认空闲 15 分钟后卸载模型释放显存，下次请求自动重载（重载约 1 分钟）；`--idle-timeout 0` 可保持常驻，`--idle-timeout 120` 可改阈值。`/health` 的 `state` 会显示 `ready` / `loading` / `idle`。

## 调音台

浏览器打开 **http://127.0.0.1:7861/lab**，可以直接：

- 选择/试听/删除参考音色，上传新 wav（自动转 24k 单声道并归一化）
- 调情绪与强度、语速、句间停顿、后期预设
- 展开「采样参数」调 `temperature` / `top_p` / `top_k` / `repetition_penalty`
- 合成后直接试听、下载；参数自动记在浏览器里

调音思路：参考音频决定七成音色（5–15 秒、干净、无 BGM）；`emo_alpha` 0.6–0.9 比较稳，`sad` 在高强度下容易退化；`temperature` / `top_p` 越高越有情绪也越容易飘，`repetition_penalty` 建议 8–12。

## 接口

`POST /tts`

```json
{
  "text": "今天好累啊",
  "voice": "dongxuelian",
  "lang": "zh",
  "emotion": "sad",
  "emo_alpha": 1.0,
  "duration_factor": 1.1,
  "interval_silence": 200
}
```

- `emotion`：`natural` / `happy` / `angry` / `sad` / `afraid` / `disgusted` / `melancholic` / `surprised` / `calm`
- `emotion_text`：自然语言描述情绪（需启用 QwenEmotion，默认开）
- `duration_factor`：0.5–2.0，越大越慢；插件里的「语速」是它的倒数
- `interval_silence`：模型内部分句之间的停顿毫秒数
- `fx`：`none` / `warm` / `warm-low` / `warm-low-space`
- `temperature` / `top_p` / `top_k` / `repetition_penalty`：采样参数，不传用模型默认值

响应是 `audio/wav`，相同参数第二次命中缓存（响应头 `X-Cache: hit`）。

## 显存

- 16G 卡（5070 Ti / 4080 级）默认配置够用：bf16 + QwenEmotion 约 9–10G；
- 显存紧张时加 `--no-qwen-emo`（省 1–2G，但 `emotion_text` 不可用）；
- 服务内部串行推理，多人同时触发会排队，不会 OOM。

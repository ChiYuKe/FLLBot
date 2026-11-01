# Qwen3-TTS 克隆服务

第二个 TTS 后端：先用 VoiceDesign 为每个预设生成一条**固定参考音频**，运行时用 Base 模型克隆这条参考，保证不同句子、不同段落始终是同一个人。

- 模型：`Qwen/Qwen3-TTS-12Hz-1.7B-Base`（克隆）+ `Qwen3-TTS-12Hz-1.7B-VoiceDesign`（只用来生成参考，不常驻）
- 输出：24kHz 单声道
- 显存：约 8G（只有 Base 常驻）
- 端口：`127.0.0.1:7862`

## 安装

```powershell
cd services\qwen-tts
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv\Scripts\python.exe qwen-tts "fastapi>=0.115" "uvicorn[standard]>=0.30"

# 运行时权重（3.6G）
uvx --from modelscope modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-Base --local_dir models\Base

# 只在生成参考音频时需要
uvx --from modelscope modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign --local_dir models\VoiceDesign
```

`voices/` 里已经带了十个预设的参考音频和对应文本；需要重新生成或改音色时：

```powershell
.\.venv\Scripts\python.exe make_refs.py            # 全部
.\.venv\Scripts\python.exe make_refs.py 04-奶音    # 单个
```

## 启动 / 停止

```powershell
services\qwen-tts\start.ps1        # 前台
services\qwen-tts\stop.ps1         # 停止（默认端口 7862）
```

`/health` 里 `ok=true` 即可用；加载约 4 秒。

**默认常驻**（`--idle-timeout 0`）：模型一直留在显存里。需要让出显卡时再传 `--idle-timeout 900` 之类的阈值。命中的缓存请求不需要模型，空闲时也能直接返回。

**开场白预热**：启动后会在后台预生成 `我在。` `怎么啦？` `好呀。` `晚安。` `早上好。`，消息进来直接命中缓存。默认预热 `07-温柔`，可用 `--prewarm-voices "07-温柔,04-奶音"` 指定多个音色。

## 预设音色

`01-软糯` `02-元气` `03-气声` `04-奶音` `05-傲娇` `06-慵懒` `07-温柔` `08-小恶魔` `09-安静` `10-微沙`

每个音色对应 `voices/<名字>.wav` + `.txt`（参考文本）。没有参考文件的音色会直接返回 400，不会悄悄换成别的音色。

## 接口

```json
POST /tts
{
  "text": "你回来啦",
  "voice": "04-奶音",
  "duration_factor": 1.1
}
```

- 音色只由 `voice` 决定，克隆同一条参考，因此跨句子、跨段落音色一致
- 情绪通过**多套参考音频**实现：`voices/emotions/<音色>/<happy|sad|angry|calm>.wav`。请求带 `emotion` 时优先用情绪参考，没有则回退基础参考；`emotion_text` 仍然不生效
- 生成情绪参考：`make_emotion_refs.py`，每种情绪生成 3 个候选，用说话人嵌入挑最接近基础音色的一条（相似度多数在 0.88–0.95）
- 语速 `duration_factor` 仍然可用
- 相同参数命中缓存，响应头 `X-Cache: hit`

`POST /tts/batch` 一次合成多段（最多 8 段），返回 base64 音频数组：

```json
{"items": [{"text": "第一段", "voice": "04-奶音"}, {"text": "第二段", "voice": "04-奶音"}]}
```

响应：`{"items": [{"audio_b64": "...", "cached": false}, ...]}`。批量会把多条序列放进同一次解码，摊薄每步开销；显存不足时自动退回逐条。

## 试听页

`services/qwen-tts/samples/listen/index.html`，十条带编号的试听；根目录 `萝莉声线-十连听-带编号.wav` 是合集。

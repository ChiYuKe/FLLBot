# Qwen3-TTS VoiceDesign 服务

第二个 TTS 后端：音色由**文字描述**固定，不依赖参考音频，输出稳定不漂。

- 模型：`Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`（Apache-2.0）
- 输出：24kHz 单声道
- 显存：约 4–5G
- 端口：`127.0.0.1:7862`

## 安装

```powershell
cd services\qwen-tts
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv\Scripts\python.exe qwen-tts "fastapi>=0.115" "uvicorn[standard]>=0.30"

# 权重（3.6G）
uvx --from modelscope modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign --local_dir models\VoiceDesign
```

## 启动 / 停止

```powershell
services\qwen-tts\start.ps1        # 前台
services\qwen-tts\stop.ps1         # 停止（默认端口 7862）
```

`/health` 里 `ok=true` 即可用；加载约 4 秒。

默认空闲 15 分钟后卸载模型释放显存，下次请求自动重载；`--idle-timeout 0` 可保持常驻，`--idle-timeout 120` 可改阈值。`/health` 的 `state` 会显示 `ready` / `loading` / `idle`，`idle_seconds` 和 `idle_timeout` 便于排查。命中的缓存请求不需要模型，空闲时也能直接返回。

## 预设音色

`01-软糯` `02-元气` `03-气声` `04-奶音` `05-傲娇` `06-慵懒` `07-温柔` `08-小恶魔` `09-安静` `10-微沙`

描述定义在 `server.py` 的 `PRESETS` 里；`voice` 也可以直接传一段自定义描述。

## 接口

与 IndexTTS 服务同构，插件可无缝切换：

```json
POST /tts
{
  "text": "你回来啦",
  "voice": "04-奶音",
  "emotion": "happy",
  "emotion_text": "温柔地撒娇",
  "emo_alpha": 0.9,
  "duration_factor": 1.1
}
```

- `emotion` / `emotion_text` 会作为「语气要求」拼进描述
- `duration_factor` 通过 ffmpeg `atempo` 实现（0.8–1.25 倍速内）
- 相同参数命中缓存，响应头 `X-Cache: hit`

## 试听页

`services/qwen-tts/samples/index.html`，十种预设各一句，浏览器直接打开。

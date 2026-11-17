# 变更记录

## 0.2.0

- `capture_screen` 在截图确认送达后、`capture_screen_file` 在保存完成后返回 `silent: true` 和一句简短中文
  `reply`，供宿主直出确认、省掉一次模型往返；原有 `sent` / `saved` / `path` / 尺寸等字段保持不变。
- 截屏失败、发送失败、回执缺失或写入失败时不返回 `silent` / `reply`，保持原错误返回。

## 0.1.0

- 新增 `capture_screen` 与 `capture_screen_file`，声明 `screen.capture` 高风险能力。
- 默认不开放截屏；自动参与、非超级管理员、未开启开关时由宿主沙盒拒绝。

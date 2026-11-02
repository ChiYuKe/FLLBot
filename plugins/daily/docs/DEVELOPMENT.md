# daily 开发维护

`plugin.py` 注册工具和 15 秒提醒周期；`store.py` 管理独立 SQLite 数据和作用域。
所有连接在单次操作后关闭，发送网络消息时不持有数据库事务。
定时器由宿主生命周期管理，取消与发送通过异步锁串行化。不得在导入时发起外部请求。
新增工具必须在回调内部保持账号、会话、用户边界，不信任模型传入的群号或用户号。

验证：

```powershell
.venv/Scripts/python.exe -m fllbot plugin check --name daily
.venv/Scripts/python.exe -m pytest -q tests/test_daily_plugin.py tests/test_web_plugin.py
```

离线测试覆盖作用域、管理员写入、直接引用范围、断线补发与关闭状态。
真实提醒需要在线 NapCat；网页提取沿用 web 插件现有测试。

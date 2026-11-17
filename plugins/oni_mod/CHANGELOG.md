# Changelog

## 0.1.0

- 新增 `oni_mod` 插件：只读浏览 NOIMods 与游戏 `mods/Dev` 参考目录，生成可编译的缺氧 Mod 工程。
- 新增 `oni_mod__build`：调用本机 .NET SDK 编译 `netstandard2.1` dll，返回结构化编译错误，并在成功后写入构建标记。
- 新增 `oni_mod__deliver`：把 dll、`mod.yaml`、`mod_info.yaml` 打包为 zip 并发送到当前会话；未编译或源码比 dll 新时拒绝发送。
- 通过提示词片段要求先查证 `oni_knowledge` 源码，再参考本地 Mod 写法，最后编译与发送。

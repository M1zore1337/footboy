# 参与 Footboy（足小子）

环境安装和检查命令见 [README](README.md#开发)。新检出后运行 `git config core.hooksPath .githooks` 启用提交检查。

## 提交与验证

- 按完成的阶段提交可运行版本。每次提交必须更新并暂存 [CHANGELOG.md](CHANGELOG.md)。
- 提交前运行与改动相关的检查，说明跳过的检查及原因。
- 区分合成源、真实直播、桌面模拟与实体设备测试。没有实测时不声称已验收。
- 两路对齐始终基于源 PTS：`D = K_B - K_V`。保持视频复制输出、手动设置优先和过期测量保护。

## 文档与隐私

- 脱敏后的 `docs/validation/*.md` 是历史测试记录，应继续跟踪。
- 不提交虚拟环境、运行状态、HLS 输出、Cookie、私有请求头、签名直播地址或原始测试媒体。提交前移除真实地址、房间号等可识别信息。
- 本地 agent 文件和工具配置不纳入仓库。`.gitignore` 不能清理已提交的历史。

## 许可

请仅提交可按项目 [MIT License](LICENSE) 分发的内容。新增第三方代码时，记录来源与版本，保留上游许可，并更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

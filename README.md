# Artifact Harbor

Archive AI artifacts to your NAS. Bring them back when needed.

一个面向 Codex 的本地优先 Skill：**会话留在本地检索，旧图片、视频和音频归档到 NAS，需要编辑时恢复到原路径。**

## 当前功能

- 本地 Dashboard 四步引导：归档期限、NAS 连接、扫描范围与缓冲期、候选预览。
- 按 7 天、1 个自然月、3 个自然月或自定义期限筛选已关联会话的媒体产出。
- 复制后 SHA-256 校验；释放本地副本是独立操作，默认保留 7 天缓冲期。
- 搜索归档、恢复原路径、保护重要文件；遇到不同本地内容不会覆盖。
- 展示待迁移、已归档、本地已释放空间估算和长期未使用归档的清理建议。
- NAS 删除先展示具体清单与本地副本状态，要求输入确认文字；保留删除记录。
- Skill 内包含“找不到的文件，先去 NAS 里找”的规则，不向用户长期记忆累积文件名。

## 安装

需要 **Python 3.10+、macOS 或 Linux、Codex 本地会话数据**。运行无第三方 Python 依赖。界面与 Skill 说明目前为中文。

```bash
git clone https://github.com/orange90/artifact-harbor.git
cd artifact-harbor
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/codex-nas-archive "${CODEX_HOME:-$HOME/.codex}/skills/"
```

若目标已安装，请先备份旧 Skill 目录，避免覆盖自己的修改。产品名为 Artifact Harbor，Skill 标识保留 `codex-nas-archive`，兼容已有安装与状态目录。

在新的 Codex 会话中输入：

> 使用 $codex-nas-archive 打开 Dashboard，引导我设置归档期限和 NAS，先预览候选文件。

也可从仓库直接启动：

```bash
python3 skills/codex-nas-archive/scripts/archive.py serve
```

打开终端打印的完整链接。服务仅监听 `127.0.0.1`，每次启动使用新的访问令牌；请保留服务进程运行。

## NAS 连接与首次使用

当前通过操作系统**已经挂载的 SMB / NFS / AFP 共享目录**连接 NAS，不绑定群晖 API。macOS 可在 Finder 中用 ⌘K 打开 `smb://NAS主机/共享名`，再填写挂载后的归档目录。凭据由操作系统管理，不填入 Dashboard。

1. 设置会话与文件的闲置期限。
2. 填写 NAS 链接和实际挂载目录，点击验证。验证会创建目标身份标识，并进行 4 KB 临时文件写入、读取和删除测试。
3. 设置扫描范围、本地缓冲期和独立的 NAS 清理建议期限。
4. 扫描预览、选择文件迁移；确认校验完成并满足缓冲期后，再单独释放本地空间。

可以跳过连接先扫描。扫描本身不上传、不删除文件。DSM / QuickConnect 管理网址不是挂载路径。

## 数据与边界

- 本地配置、SQLite 索引和操作日志默认保存在 `~/.local/share/codex-nas-archive`，可用 `--state` 或 `CODEX_NAS_ARCHIVE_HOME` 指定。请备份该目录，勿提交到公开仓库。
- NAS 保存媒体对象与恢复清单；清单包含原路径、会话标题和 ID 等信息，只应放在自己授权的共享目录。
- 只读 Codex `state_*.sqlite`，不修改会话正文或数据库。只处理关联会话的 `outputs` 媒体文件；置顶、近期活动、多个会话共用目录等会跳过。
- 自动找回依赖 Skill 被加载和本地索引可用，不是系统级文件访问拦截。
- 清理建议只依据本工具的成功恢复及用户手动登记，无法感知 Finder、NAS 网页等外部访问。
- 删除 NAS 归档不经过本工具回收站。确认前核对是否为唯一副本；NAS 快照可能影响实际空间回收。
- 空间释放是估算；APFS 克隆与快照可能影响系统可用空间。统计也不涵盖旧版本等未被当前索引引用的对象。
- 当前未接入 rclone，未实现 Windows、自动挂载、定时运行、断点续传或索引重建命令。

## 验证与开发

```bash
python3 -m unittest discover -s skills/codex-nas-archive/tests -v
```

现有 52 项测试使用隔离临时目录和模拟 Codex 数据库，覆盖迁移、恢复、校验失败、本地释放、访问记录、删除确认及中断状态核对。**隔离测试不等于真实 NAS 验收**；首次发布尚未完成真实群晖端到端测试。

实现细节见 [架构说明](skills/codex-nas-archive/references/architecture.md)，Agent 操作规则见 [SKILL.md](skills/codex-nas-archive/SKILL.md)。

## 后续方向

评估 rclone 作为 SMB / SFTP / WebDAV / S3 传输层，同时保持目标身份检查、完整性校验和独立的清理确认。此项尚未实现。

参考调研：[SessionHarbor](https://github.com/WangPeterXF/session-harbor)、[Backrest](https://github.com/garethgeorge/backrest)、[rclone](https://github.com/rclone/rclone)。当前发布未引入这些项目的代码或依赖。

## License

本仓库目前未授予开源许可证；公开源码不等同于授予自由复制、修改或分发许可。许可证可由维护者在后续版本中确定。

# FGO-MAA

Windows 上的 FGO 日常、周常与普通关卡自动化助手，使用 ADB、OCR 和 Chaldea Laplace 战斗模拟。

当前版本：**v1.0.0-preview.20261103**。这是预览版本，活动剧情自动推进尚未实现。

## 下载与运行

从 [Releases](https://github.com/FORSTNOVA/fgomaa/releases) 下载 Windows 压缩包，完整解压后运行 `FGO-MAA.exe`。请保留整个目录，程序依赖 `_internal/`、`data/`、`src/` 和 `runtime/`。

1. 启动模拟器并选择设备；多设备时明确选择目标设备。
2. 导入自己的完整 Chaldea／FGO 账号 JSON 或 HAR，包含从者、礼装和任务记录。
3. 从日常、周常、自动推关中选择任务；建议先使用“仅模拟预览”。
4. 从大厅或目标地图启动。识别、模拟、编队、装备或战斗失败时停止，后续队列保持未执行。

公开包不带账号、抓包数据、HTTPS 证书私钥、历史进度或运行日志。内置 HTTPS 抓包没有预置证书，请使用 JSON/HAR 导入账号；本仓库没有提供证书自动生成配置流程。

## 当前功能

- 日常免费召唤、友情召唤、日常本和任务奖励处理。
- 周常任务识别、最低 AP 路线规划与自动执行。
- 已通关自由关卡、日常本的自选关卡队列；逐关模拟、配队、装备、战斗和结算。
- 已开放普通自由关卡的自动首通：实机核对当前阶段，完成整关后再选择下一关。
- 关数、AP 预算、体力恢复和果实个数上限四个独立开关；取消上限勾选即不限。
- 赤铜果实按当前关卡的 AP 缺口一次选足；其余果实逐个使用，未知使用结果停止且不重复确认。
- 推荐从者与礼装自动选取、等级／满破／装备槽位复核，失败重试后停止。
- 战斗候选包含常规连发、三打手轮换和低血量普通攻击；初始 NP、色卡和宝威等礼装均须模拟通过。

### 关卡目录和导航

目录来自 [Atlas Academy](https://api.atlasacademy.io/)，启动时检查更新，每 24 小时条件刷新，也可点击“全量刷新关卡库”。敌人阶段数据按需下载。国服没有完整敌方配置时，可显式回退到同 ID、同阶段的日服数据；结果记录数据来源。

目录中存在关卡代表资料已收录。能否执行还取决于导航适配、开放时间、账号通关或前置记录和模拟结果；列表会显示不可执行原因。

已接入以下普通自由关卡的导航逻辑：第一部、第 1.5 部四章、异闻带 1～7（亚特兰蒂斯和奥林波斯独立识别）、平安京、Traum，以及奏章Ⅰ～Ⅳ。地点点击仍要求实际 OCR 确认，未知地图不会放行。不同剧情进度下的复杂地图切层尚未全覆盖。

### 万圣节 2026

单独适配“终结·万圣节2026～于南瓜星闪耀的歌姬～”的 9 个普通活动周回本。已通关本可按“活动关卡”筛选并加入队列；已解锁但未通关的普通本可使用自动首通。

先手动完成必要的活动剧情，再导入完整新账号记录。软件核对准确活动横幅、地图背景特征、地点改名、关卡名称和 AP。活动过期、未满足前置、一次性挑战和高难等会拦截。其他限时活动需要分别适配。

本次活动地图、首通和战斗尚未完成实机验收；普通本第一仓储库已通过本地原生 Laplace 的 3T 预览。不会自动推进活动剧情、选择剧情分支或刷点数解锁下一节。

## 源码运行

建议使用 Windows、Python 3.13；发布环境已用这一组合验证。源码仓库不存放大型 Flutter 运行二进制，请从同版本 Windows 发布包取出 `runtime/laplace/`，放入源码根目录的 `runtime/laplace/`。对应上游源码和许可证已随仓库及发布包保留。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

CLI 示例：

```powershell
# queue.json: [{"quest_id":"fuyuki_x_a","runs":1}]
python -m src.workflows.quest_runner --account account.json --queue queue.json --preview
python -m src.workflows.quest_runner --account account.json --queue queue.json --device 127.0.0.1:16416
```

运行记录只写入本地 `.app_profile/`。停止后重新核对游戏状态再启动，中断的战斗不会直接计为通关。账号文件和证书目录已从 Git 提交范围中排除。

## 测试与构建

```powershell
python -B -m unittest discover -s tests
```

功能快照已通过 473 项回归；部分测试需要 Windows 原生 Laplace 运行时和联网获取公开游戏数据。截图回归或本地模拟通过，不等同于所有关卡实机验收。

- Windows 打包：`python build_exe.py`（需安装 PyInstaller，生成目录在 `dist/FGO-MAA/`）。
- 同步当前源码与资源：`python sync_dist.py`。
- 生成排除个人数据的公开发布包：

```powershell
python scripts/package_release.py --version v1.0.0-preview.20261103 --output dist/FGO-MAA-v1.0.0-preview.20261103-windows-x64.zip
```

发布脚本会排除账号、证书、运行历史和重复外部资源，并保留依赖通知和 Laplace 对应源码；压缩完成后校验 ZIP，生成 SHA256 文件。

Laplace 构建和算法边界见 [integrations/laplace/README.md](integrations/laplace/README.md)，本次变化见 [CHANGELOG.md](CHANGELOG.md)，第三方来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目自有代码目前未另行指定统一开源许可证，第三方组件保留其原有许可证与署名。

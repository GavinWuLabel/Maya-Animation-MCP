# Maya Animation MCP 0.2.0

本地 Maya 动画工具：MCP 服务运行于外部 Python，Maya 端只使用自身 Python 和标准库。提供 13 个工具，支持绑定检查、控制器落键、动画层、曲线/姿态读取、场景副本与 GUI 视口输出。

这个 ZIP 包含源码、启动脚本、自动化测试、更新记录和验证证据。它不包含 Maya、Python 虚拟环境、角色资产、运行缓存或会话连接令牌。依赖安装需要网络；Maya 必须已安装且能够独立运行。

## 环境

- 外部 Python 3.10 或更高；依赖固定为 `mcp==1.30.0`。
- Maya 端已在 Windows Maya 2022 / Python 3.7.7 实测。其他 Maya 版本尚未验收。
- PowerShell 启动器可从明确参数、`MAYA_LOCATION`、PATH 或 Autodesk 默认安装目录寻找 Maya。非标准安装请传 `-MayaPython` 或 `-MayaExe`。
- 路径从解压位置计算；输出根目录、后台工作目录和连接文件可以显式指定。

## 安装

解压到固定目录，在该目录打开 PowerShell。有 uv 时运行：

```powershell
.\scripts\setup.ps1
```

没有 uv 时，使用外部 Python 3.10+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe .\scripts\configure.py
```

配置生成器会创建三个本机文件：`codex-mcp.toml`（GUI）、`codex-mcp-background.toml`（后台）、`START_MAYA_BRIDGE.mel`。不会修改应用设置；移动解压目录后重新运行生成器。

把所需模式的 TOML 段加入 MCP 客户端配置。两份配置使用同一服务名 `maya_animation`，选择其中一份，不要同时追加。已有同名服务应更新该段路径并保留其他设置。可选的 `scripts/register_codex.py` 只在没有同名服务时追加 GUI 配置，并备份原配置；不会覆盖已存在的同名段。

## GUI 模式

在 Maya Script Editor 中执行生成的 `START_MAYA_BRIDGE.mel` 内容，或在 Python 页运行解压目录中的 `maya_side/start_in_maya.py`。只启动 bridge，不打开或保存场景。

默认输出为包内 `output`，可先设置 Maya 进程的 `MAYA_MCP_OUTPUT_ROOT` 环境变量。默认关闭 `execute_python`。如确有可信代码执行需求，在 Maya Python 页停止并以指定输出根重启：

```python
from maya_side import maya_bridge
maya_bridge.stop()
maya_bridge.start(connection_file="你的解压目录/runtime/connection.json",
                  output_root="已经存在的动画工作目录", allow_python=True)
```

调用 `get_status` 确认连接、PID、场景及 `batch=false`。GUI 的 `capture_frame` / `playblast` 需要可用视口。

## 后台模式：不接管已有 Maya 窗口

使用后台 TOML 配置，运行隐藏的独立 mayapy：

```powershell
.\scripts\start_background.ps1 -MayaPython 'C:\你的Maya目录\bin\mayapy.exe' -OutputRoot 'E:\你的动画目录'
```

需要可信 Python 时增加 `-AllowPython`。可另外指定 `-WorkRoot` 和 `-ConnectionFile`；自定义连接文件时，MCP 配置里的 `MAYA_MCP_CONNECTION` 必须对应它。

默认连接为 `runtime/background_connection.json`，工作目录为 `runtime/background`。后台使用独立 Maya profile、主线程任务队列和自动端口；不会载入用户 GUI 场景。启动器返回 PID 不等于握手完成，请以 `get_status` 成功和 `batch=true` 确认。后台的截图/视口 playblast 返回 `GUI_REQUIRED`；可以通过可信 Python 使用 Maya 软件渲染，但这与视口输出是两种能力。

停止时，只在这个后台实例的工作目录创建 `stop.request`，它会自行结束。不要关闭用户 GUI Maya。若启动被已有连接文件挡住，先检查记录中的 PID 是否仍存活，不要覆盖活动会话。连接文件含会话 token，不应共享或加入 ZIP。

## 工具

| 工具 | 作用 |
|---|---|
| get_status / get_scene_info | 版本、当前场景、时间范围、单位、引用和动画层 |
| discover_rig / inspect_nodes | 控制器候选及通道锁、输入和枚举 |
| get_pose | 读取真实求值姿态，恢复原时间且不污染 undo 栈 |
| create_animation_layer | 为明确的控制器属性创建新动画层 |
| set_keyframes | 全批预检、落键、切线和失败回滚 |
| get_animation_curves / set_current_time | 读取曲线或评估指定帧 |
| save_scene_copy | 输出根目录内导出新的 .ma，保留当前场景身份与 dirty 状态 |
| capture_frame / playblast | GUI PNG 单帧或完整序列 |
| execute_python | 可选可信 Maya Python；可关闭自动 undo chunk |

写动画前依次检查状态、场景、绑定候选和具体控制器。使用完整 DAG 路径及可写控制器，不能把 NURBS 候选直接当作已验证的绑定映射，也不要给受驱动的变形骨骼强行落键。

动画层落键的 value 表示最终通道值；复杂权重、mute/solo 和多层混合仍需读取实际姿态验证。SDK 驱动曲线、锁定/非标量通道拒绝直接写入。副本保存拒绝覆盖已有路径；PNG 序列需要空目录。查询姿态即使不指定 frame 也会刷新当前帧求值。

`execute_python(code, undo_chunk=True)` 默认有自动事务。调用 undo/redo、打开文件或自行管理事务时使用 `undo_chunk=False`；此时异常不会自动回滚。可信 Python 具有本机文件权限，不是沙箱。

协议仅监听 127.0.0.1，JSONL 带请求 ID、token 与版本。连接前失败报 `CONNECT_FAILED`；发送后结果未知报 `EXECUTION_UNKNOWN`，不得盲目重试写入。待执行超时可取消，运行中不能冒充取消成功。停止或重启后的旧请求失效。

## 测试与验收范围

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\scripts\run_native_test.ps1 -MayaPython 'C:\你的Maya目录\bin\mayapy.exe'
```

原生测试创建自己的隐藏进程和测试角色，使用独立连接；不接管生产 Maya。外部依赖已在另一环境安装时，可传 `-ClientPython`。测试证据放在 `evidence/native/时间戳`。可选 GUI 测试入口为 `scripts/launch_test_maya.ps1`，本次归档没有运行它。

本次归档的最新结果见 `validation/summary.json`。此前六个角色动画制作的 32 项唯一原生检查保存在 `validation/previous_animation_run/`，属于历史集成证据，不能替代本包的新测试。GUI 视口截图/完整 playblast 在本次发布中未重新实测，不能将 `GUI_REQUIRED` 的正确拒绝等同于输出能力已验证。

本工具提供 Maya 执行与检查基础；通用 IK/FK 匹配、空间切换、脚底锁定与参考视频动画重建仍需针对具体绑定的映射和算法。

## 文件结构

- `src/maya_animation_mcp`：外部 MCP 服务和内部桥接客户端。
- `maya_side`：GUI bridge 和独立后台服务，兼容 Maya Python 3.7。
- `scripts`：安装、配置、启动及隔离测试入口。
- `tests`：离线回归与 Maya 原生测试。
- `validation`：当前及明确标注的历史测试证据。
- `CHANGELOG.md`：本版更新；`MANIFEST.json`：归档内容的 SHA-256 清单。

运行中的 Maya/MCP 进程不会自动热重载源码；保存工作后正常重启对应服务才能使用更新。复制或解压文件不会改变当前连接和场景。

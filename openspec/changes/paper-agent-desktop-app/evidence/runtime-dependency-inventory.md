# 桌面运行依赖输入与验收边界

记录日期：2026-10-14（Asia/Shanghai）。此文记录本机真实输入和复核，不是安装版本已交付的证明。

## 已取得输入

- 独立 CPython：3.12.14，Windows x64，managed Python 的 `sys.prefix == sys.base_prefix`，不依赖开发机 `E:/Anaconda3`。
- 开发 `.venv` 的 Python 3.12.7 基于本机 Anaconda，因此不能直接当作可迁移 runtime。
- managed Python 输入采用官方 python-build-standalone 发行，通过 `uv python install` 下载到本变更私有输入目录；没有写入全局 PATH 或 Python 注册表。
- 项目实际依赖已复制到 managed Python，排除开发环境的 `_virtualenv.py`、`_virtualenv.pth` 与缓存。当前输入有重复 pip 发行元数据，构建器须明确排除 standalone 自带 `Lib/site-packages/pip-26.2.1.dist-info`，以保留项目真实 pip 版本；不删除系统目录，不伪造包清单。
- 官方 Node：v24.21.0，Windows x64。下载 zip 的 SHA-256 已与同一官方发行目录的 SHASUMS256 核对：`158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541`。二进制实际执行输出 `v24.21.0`。

## 已实际执行的隔离检查

在 `PYTHONNOUSERSITE=1` 下执行 managed Python：

- `site.ENABLE_USER_SITE` 为 `False`，不读取当前用户的 Roaming Python 包。
- `torch`、`scipy`、`pymupdf`、`uvicorn`、`fastapi`、`pydantic`、`numpy` 和 `scripts.desktop_app` 实际导入成功。
- 当前输入的 88 个 distribution 包含上述额外 pip 元数据，尚不能用作最终 87 包的 release 清单。
- 宿主与其全部 Python child 必须禁用 user site，并移除非受控 `PYTHONHOME` / `PYTHONPATH`；最终 bundle 还需重做 dependency identity、native imports 与搬迁路径验证。

## 实际 runtime adapter 检查

- 将 data root 放在 workspace/resource root 内部被拒绝，返回 `desktop_app_roots_overlap`，未创建第二份伪业务资料。
- 在独立 `%LOCALAPPDATA%/Temp/AgoraPaperAgentVerification-20261014` 下执行 `status`，返回真实 `STOPPED`、空 owned process 列表；未停止或接管其他端口。
- `inventory` 返回真实 `UNINITIALIZED` 与缺失的 required owners，而不是生成业务样本或宣称就绪。
- 当前 memory inventory extension 尚待合并。其完成后必须重新执行 inventory、备份、隔离恢复；上面的旧输出不自动升级为通过。

## 尚未验收

- Electron 版本必须以官方完成发行且 Windows x64 binary 和 checksum 实际可取得为准，不能仅按 npm 元数据的最高版本决定。
- 真实 Electron 窗口测试不能以 runtime/binary 不存在的 skipped case 替代。
- bundle integrity、installation、真实 owner、离线阅读、native 导出、safe drain、完整 backup/restore 及约定非开发环境入口都仍需实测。
- 源码、依赖、配置、bundle 和安装输出必须绑定同一最终候选，不复用前端 fixture 或开发服务截图核销发布条件。

## 本地原始证据

私有输入及日志：`.tmp/paper-agent-desktop-app-20261014/runtime-inputs/`。该目录不是用户资料，不提交 runtime 二进制到源码仓库。来源记录包括 `node-runtime-provenance.json`、官方 release index、对应 SHASUMS256 与 Python 下载日志；最终构建应生成独立 release / bundle 资源清单。

### managed Python 的 Windows 权限实测

在本变更私有 `managed-python-acl-probe` 目录调用既有 `protect_private_path` 后，实际读取 Windows DACL，确认不存在 Everyone (`S-1-1-0`) 的授权条目，只保留当前 owner 的授权；未改动父目录、系统或用户现有资料。`win32api`、`win32security`、`pywintypes`、`sqlite3` 和 `ssl` 实际导入成功。该输入使用 SQLite 3.53.1 / OpenSSL 3.5.8，最终 bundle 须记录其实际运行身份并重新验证，而不是假定与 Anaconda 环境的 native runtime 相同。

## 输入与中间构建更新（2026-10-14）

- 当前已准备并实际读回 `portable-python.json` 与 `portable-node.json`。managed Python 为 CPython 3.12.14，受控 staging 精确排除额外的 `Lib/site-packages/pip-26.2.1.dist-info` 后仍需重新证明最终真实 package 集合；没有绕过运行时策略删除输入文件。
- Electron 固定为已实际取得 Windows x64 binary 的 44.7.1；官方 zip 为 157,870,284 bytes，SHA256 为 `ace7ce1580d33e9fe7284823d7e23ba687263c448d85d8dc4b5daa9bba73f97a`。真实窗口故障/待配置 E2E 已通过，但业务页面和安装仍未验收。
- inventory 已集中复查：只存在一个 `research_personal_memory` owner，schema、路径与 SQLite 一致性信息均真实；此前提及的 pending extension 不再是当前状态。
- 唯一 ReleaseManager 完成一次真实生产 Next.js 中间源码构建并验证激活：release `20261014T052955Z-c8fba72d6c2d`，source digest `c8fba72d6c2d269b1787625c3026b3bfd2f9f8dee86abbffcd3a29b0ecf7ad96`，42,539 个资源。该源码候选实际使用开发解释器 Python 3.12.7 与 Node v24.10.0，不能当作已完成的 managed / portable 构建。
- 上述中间候选 source revision 为 `dacdbbfd5c79ebe7a3ba75f3881d80dbdf63334c`，源码包含当前未提交的实现；以真实 source digest 区分，不声称该 Git revision 已独立包含所有 App 代码。
- 最终配置闭环、responsive 修复、release 路径 junction 防护仍在实施。最终冻结后必须重新构建、重新取得 dependency/config/bundle 身份与完整 gate，不复用中间候选核销发布条件。

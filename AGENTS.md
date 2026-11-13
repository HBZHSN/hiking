# 项目约定

## Python 虚拟环境

- 所有 Python 命令、依赖安装、服务启动和 Python 测试必须使用项目根目录下的 `.venv` 虚拟环境，不得使用系统 Python 执行项目任务，也不得向系统环境安装依赖。
- 如果 `.venv` 不存在，先使用 `python3 -m venv .venv` 创建。系统 Python 仅用于创建虚拟环境。
- 优先显式调用 `.venv/bin/python`，安装依赖使用 `.venv/bin/python -m pip install ...`，不依赖终端是否已激活虚拟环境。
- 本地静态服务启动命令：`.venv/bin/python -m http.server 8010 --bind 0.0.0.0`。
- 新增或修改启动脚本、文档和测试命令时，也必须遵守以上约定。Node.js 命令（如 `npm test`）不受 Python 虚拟环境要求影响。

## 功能提交

- 每个完成且通过验证的功能点单独创建 Git 提交，提交信息说明功能及验证内容。
- 不提交 `.env`、SQLite 数据库、个人 GPX 记录、会话信息或虚拟环境。
- 后端使用 Python 标准库与 SQLite；测试使用 `.venv/bin/python -m unittest discover -s tests`。

## 正式环境的测试隔离

- 当前运行的是正式环境。测试不得读取或修改正式 `data/`、`.env`、个人 GPX、照片、会话或上传凭证，不得连接正式服务执行测试，也不得安装、重启或停止正式服务。
- Python 测试在导入应用代码前先 `import safety`（`tests/safety.py`），数据库、文件和配置使用 `TemporaryDirectory()`；外部网络使用模拟响应。
- 浏览器测试必须自行创建临时数据库和虚构数据，启动监听 `127.0.0.1`、端口 `0` 的独立实例，使用测试密码、新浏览器上下文和模拟地图资源，结束后关闭实例并清理临时目录。

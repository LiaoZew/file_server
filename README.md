# file_server

基于 Python + Flet 的 HTTP 文件服务器，支持：

- 文件上传/下载
- 文件夹上传/下载（保持目录结构）
- 断点续传下载（HTTP Range）
- 分块并行上传（支持中断后续传）
- 局域网与公网访问（配合端口映射/反向代理）

## 1. 安装

```powershell
pip install -r requirements.txt
```

## 2. 启动

```powershell
python app.py
```

纯 Web 版本（不打开 Flet 窗口）：

```powershell
python web_server.py --host 0.0.0.0 --port 8080 --root .\data
```

或使用脚本：

```powershell
.\run_web.ps1
```

Linux 直接运行服务：

```bash
python3 web_server.py --host 0.0.0.0 --port 8080 --root ./data
```

启动后在 Flet 界面里配置：

- `Host`：建议 `0.0.0.0`（局域网可访问）
- `Port`：默认 `8080`
- `Storage Root`：文件存储目录
- `Access Token`：可选，建议公网场景设置

点击 `Start Server` 后可看到：

- 本机地址（127.0.0.1）
- 局域网地址（例如 192.168.x.x）
- 公网访问提示地址

## 3. Web 客户端

浏览器打开：

```text
http://<ip>:8080/
```

页面支持：

- 浏览目录
- 创建目录
- 选择文件上传
- 选择文件夹上传（保留目录层级）
- 自动上传（勾选后选择/拖拽即自动开始）
- 文件下载
- 文件删除（仅文件）
- 文件夹删除（递归删除）
- 文件夹打包下载（zip）
- 消息对话框（发送/查看消息）
- 前端一键关闭服务器

## 4. 断点续传与性能

- 下载断点续传：`/download` 支持 `Range` 请求头，下载工具可自动续传。
- 上传断点续传：前端按 4MB 分块并行上传，使用 `upload_id + chunk index`，中断后重试可跳过已上传块。
- 并发提速：上传默认并发 4 路分块，服务端 I/O 使用较大缓冲区以提升吞吐。

## 5. API 速览

- `GET /health`
- `GET /list?path=<rel_path>`
- `POST /mkdir`
- `POST /upload`（普通上传）
- `POST /upload/init`（初始化分块上传）
- `POST /upload/chunk`（上传分块）
- `GET /upload/status/{upload_id}`
- `POST /upload/complete`（合并分块）
- `DELETE /delete?path=<rel_file_path>`（删除文件）
- `DELETE /delete-folder?path=<rel_dir_path>`（删除文件夹，递归）
- `GET /messages`（获取消息）
- `POST /messages`（发送消息）
- `POST /server/shutdown`（关闭服务器）
- `GET /download?path=<rel_file_path>`（支持 Range）
- `GET /download-folder?path=<rel_dir_path>`（zip 下载）

## 6. 公网使用建议

- 生产环境建议放在 Nginx/Caddy 后面，启用 HTTPS。
- 开启访问令牌（`Access Token`）后，客户端需在 Header `x-token` 或 query `token` 传递令牌。
- 对公网开放时建议配合防火墙/IP 白名单/限流。

## 8. Linux 上传慢/偶发失败排查

- 建议把 `--root` 放在 Linux 本地文件系统路径（例如 `/home/<user>/data`），不要放在 `/mnt/d/...` 这类 Windows 挂载盘，后者 I/O 明显更慢。
- 新版本前端已启用分块重试、请求超时控制和多文件并发；若网络不稳定可先减少并发。

## 7. Linux 单文件打包

由于 PyInstaller 不能在 Windows 直接交叉编译 Linux 可执行文件，请在 Linux 环境打包：

```bash
chmod +x ./build_linux_onefile.sh
./build_linux_onefile.sh
```

产物：

```text
dist/file_server_web
```

也支持 GitHub Actions 自动构建（已提供）：

- `.github/workflows/build-linux-onefile.yml`
- 手动触发 `workflow_dispatch` 后，在 Artifacts 下载 `file_server_web-linux`

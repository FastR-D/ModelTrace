# Vercel 部署

在 Vercel 导入仓库，根目录使用仓库根目录。仓库提供 Flask 入口、Python 3.12 设置和静态资源构建命令。

- `vercel.json` 设置依赖安装、静态构建和 90 秒函数预算。
- `build_web.py` 将网页资源复制到 `public/static/`。
- 云端默认显示 API 测试，指纹库只读；每条上游请求最多等待 60 秒。
- Token 由使用者在网页填写，不需要配置到 Vercel 环境变量。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `MODELTRACE_PUBLIC=1` | 在本地启用与 Vercel 相同的公共模式 |
| `MODELTRACE_ALLOWED_HOSTS` | 可选，允许访问的接口域名，多个域名用逗号分隔 |
| `MODELTRACE_USER_AGENT` | 可选，覆盖发往上游的 User-Agent |

允许域名留空时，可访问通过地址校验的公网 HTTPS 接口。限制域名的示例：`api.example.com,second.example.com`。

## 本地复现云端模式

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MODELTRACE_PUBLIC = "1"
.\.venv\Scripts\python.exe app.py
```

打开 `http://127.0.0.1:7860/`。默认显示 API 自动测试。填写公开 HTTPS 接口的 Base URL、Token 和模型名；可选择自动识别、Responses、Chat Completions、Messages 协议。取得三条有效输出后统一归因。每条请求独立运行，最多尝试六条；不再把一条有效回答显示为完整测试成功。

结束后按 Ctrl+C，并运行 `Remove-Item Env:MODELTRACE_PUBLIC`。正常本地模式允许访问本机模拟 API，也允许采集指纹；公共模式仅允许公网 HTTPS 接口，并关闭创建指纹库与采集接口。

## 部署后验证

在同一份源码上执行：

```sh
python tests/verify_deployment.py https://YOUR_PROJECT.vercel.app
```

脚本核对首页、挑战、候选模型、静态脚本、只读接口和内网地址限制，不调用收费模型接口。候选模型和脚本哈希应与这份源码一致。

真实接口测试由使用者填写 Base URL、Token 和模型名。支持 Chat Completions、Responses、Messages；取得三条有效回答后归因。网络中断后可以继续采集，归因失败后可重试计算，已收到的回答保留在当前页面内存中。

自动识别会先试对应端点，再仅在明确的协议不兼容错误时换协议；授权失败、限流、超时不会盲目换协议。已知协议时直接选择它，速度更快。

## 运行边界

API Key 会经本服务内存转发给填写的接口，不写入文件，不写入浏览器存储，也不在错误中原样回显。API 响应设置 `Cache-Control: no-store`。公网模式使用已验证的公网 IP 建立连接，并保留原域名做 TLS 校验，拒绝内网地址和跳转。

云端不保存新增指纹，因为函数磁盘不是持久数据库。需要更新候选模型时，在本地采集和重建 `data/unified_bank.json`，测试后重新部署。

结果是现有候选库内的统计归因。未收录模型也会归到某个候选，第一名不能单独证明模型身份。CLI 的 `check` 使用直接 API 挑战，不验证运行中的 Codex 或 Claude Code 会话。

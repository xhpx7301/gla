# GLA

GLA 是面向个人和小型 Linux 服务器的轻量观测项目，用一台中心服务器集中查看多台服务器的运行状态、安全事件、Xray 访问日志和 3x-ui 流量。

中心端使用 Grafana、Loki、VictoriaMetrics 和 Alloy；其他服务器通常只运行 Alloy，以及按需启用的 3x-ui 指标导出器。

## 快速了解

| 角色 | 安装内容 | 管理命令 |
| --- | --- | --- |
| 中心服务器 | Grafana、Loki、VictoriaMetrics、Alloy、预置仪表盘 | `gla` |
| 普通采集服务器 | Alloy：SSH、Fail2ban、UFW 日志和主机指标 | `alloy` |
| 3x-ui 采集服务器 | 普通采集能力 + Xray 日志 + 3x-ui API 流量 | `alloy` |

```text
HY-248 等采集服务器
  SSH / Fail2ban / UFW / 系统指标 / Xray / 3x-ui API
                         |
                         | HTTPS + Basic Auth
                         v
HY-2314 中心服务器
  Loki（日志） + VictoriaMetrics（指标） -> Grafana（仪表盘）
```

项目默认提供三个 Grafana 仪表盘：

- `Xray 访问日志`：原始日志、客户端、入站和访问目标统计。
- `Xray Gateway`：在线用户、上下行速率、客户端和入站流量、来源 IP。
- `服务器安全与系统`：SSH 失败、Fail2ban、UFW、CPU、内存和网卡流量。

## 数据从哪里来

| 数据 | 来源 | 存储位置 |
| --- | --- | --- |
| Xray 访问记录、来源 IP、访问目标 | `/var/log/x-ui/access.log` | Loki |
| SSH 失败登录 | systemd journal | Loki |
| Fail2ban 发现、封禁、解封 | `/var/log/fail2ban.log` | Loki |
| UFW 拒绝记录 | `/var/log/ufw.log` | Loki |
| CPU、内存、网卡流量 | Alloy 内置 Unix exporter | VictoriaMetrics |
| 3x-ui 在线用户和上下行流量 | 3x-ui Panel API | VictoriaMetrics |

Xray 来源 IP 的“连接次数”和 3x-ui 的“流量字节数”是两种不同口径。普通 access log 没有每条连接的字节数，因此项目不会把来源 IP 连接次数伪装成精确流量。

## 环境要求

- Debian 或 Ubuntu。
- Docker 正常运行，并安装 `docker compose` 或 `docker-compose`。
- 中心服务器建议至少 `2C / 3G / 30G`。
- 采集服务器 `1C / 1G / 10G` 可以运行；建议配置 Swap。
- 服务器时间和时区应正确，建议启用 systemd-timesyncd 或其他 NTP 服务。
- 远程采集服务器只需要出站 HTTPS，不需要为 Alloy 开放公网入站端口。

## 首次安装中心服务器

```bash
SERVER_NAME=HY-2314 \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-grafana-loki-alloy.sh)
```

脚本会自动检测 `/var/log/x-ui/access.log`。存在时采集 Xray，不存在时仍可正常安装系统与安全观测功能。

如果中心服务器本身也运行 3x-ui，并且需要 API 流量统计：

```bash
SERVER_NAME=HY-2314 \
XUI_API_URL=https://panel.example.com/面板路径/panel/api/inbounds/list \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-grafana-loki-alloy.sh)
```

脚本会交互询问 3x-ui API Token，输入内容不会显示。安装后运行：

```bash
gla
```

## 配置远程写入入口

中心容器默认不直接向公网开放 Loki 和 VictoriaMetrics。使用 Nginx Proxy Manager 时，建议创建两个独立 HTTPS Proxy Host：

| 用途 | 转发主机 | 端口 | 采集端路径 |
| --- | --- | --- | --- |
| 日志 | `xray-loki` | `3100` | `/loki/api/v1/push` |
| 指标 | `gla-victoriametrics` | `8428` | `/api/v1/write` |

两个入口都必须启用 SSL 和 Basic Auth。建议使用独立域名，例如：

```text
https://loki.example.com/loki/api/v1/push
https://metrics.example.com/api/v1/write
```

不要直接在 UFW 或云防火墙中开放 `3100`、`8428`。Grafana 也不应无认证暴露公网。

VictoriaMetrics 健康检查示例：

```bash
curl -u alloy-agent https://metrics.example.com/-/healthy
```

## 安装采集服务器

### 只采集日志

适用于暂时不需要 CPU、内存和流量曲线的服务器：

```bash
SERVER_NAME=HY-248 \
LOKI_URL=https://loki.example.com/loki/api/v1/push \
LOKI_USERNAME=alloy-agent \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh)
```

### 增加主机指标

```bash
SERVER_NAME=HY-248 \
LOKI_URL=https://loki.example.com/loki/api/v1/push \
LOKI_USERNAME=alloy-agent \
METRICS_URL=https://metrics.example.com/api/v1/write \
METRICS_USERNAME=alloy-agent \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh)
```

### 增加 3x-ui API 流量

```bash
SERVER_NAME=HY-248 \
LOKI_URL=https://loki.example.com/loki/api/v1/push \
LOKI_USERNAME=alloy-agent \
METRICS_URL=https://metrics.example.com/api/v1/write \
METRICS_USERNAME=alloy-agent \
XUI_API_URL=https://panel.example.com/面板路径/panel/api/inbounds/list \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh)
```

API Token 在 3x-ui 的“面板设置 -> 安全 -> API Token”中创建。它具有完整管理权限，不要直接写入命令、截图或 GitHub。GLA 会将 Token 保存为：

```text
/opt/xray-alloy-collector/secrets/xui-api-token
```

文件权限为 `600`。安装后运行 `alloy` 打开采集端管理菜单。已安装的采集器也可以直接选择菜单 `11. 配置或关闭 3x-ui API 流量采集`，不需要手写环境变量。

## 已有安装如何升级

以 HY-2314 为中心、HY-248 为采集端时：

1. 先在 HY-2314 运行 `gla`，选择编号 `1`。旧版菜单显示“安装或更新脚本并重新部署”，新版显示“更新配置、脚本与仪表盘”。
2. HY-2314 本身运行 3x-ui 时，使用下面方式进入菜单，再选择编号 `1`：

```bash
XUI_API_URL=https://panel.example.com/面板路径/panel/api/inbounds/list gla
```

3. 在 Nginx Proxy Manager 创建 VictoriaMetrics 的 HTTPS 入口并配置 Basic Auth。
4. 在 HY-248 使用下面命令进入旧管理菜单：

```bash
METRICS_URL=https://metrics.example.com/api/v1/write \
METRICS_USERNAME=alloy-agent \
alloy
```

5. 选择编号 `1`，安装器会询问 Loki 和指标接口的密码。
6. HY-248 同时运行 3x-ui 时，改为：

```bash
METRICS_URL=https://metrics.example.com/api/v1/write \
METRICS_USERNAME=alloy-agent \
XUI_API_URL=https://panel.example.com/面板路径/panel/api/inbounds/list \
alloy
```

7. 更新完成后先检查 `alloy` 状态，再进入 Grafana 查看新仪表盘。

升级会保留 Grafana、Loki、VictoriaMetrics 数据卷以及已保存的 3x-ui API Token。远程写入密码不会写入 `.install.env`，更新时需要重新输入。

## `gla` 菜单说明

运行 `gla` 后，顶部会直接显示 Grafana、Loki、VictoriaMetrics、Alloy 和 3x-ui 流量采集的状态。

| 选项 | 作用 |
| --- | --- |
| 更新配置、脚本与仪表盘 | 下载当前仓库脚本，重新生成配置并保留数据卷 |
| 服务控制 | 启停全部组件，或单独重启某个组件 |
| 查看运行状态与资源占用 | 查看容器状态、镜像、数据卷和日志文件占用 |
| 查看服务日志 | 分别查看五个组件的最近日志 |
| 查看访问地址、凭据与模块 | 查看 Grafana 地址、密码、内部接口和已安装仪表盘 |
| 更新容器镜像 | 拉取最新镜像并重建容器，不修改采集参数 |
| 卸载并删除全部数据 | 删除容器、配置和项目数据卷，执行前需要二次确认 |

“更新配置、脚本与仪表盘”和“更新容器镜像”不是同一操作：前者更新 GLA 项目配置，后者只更新 Docker 镜像。

## 常用安装参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_NAME` | 中心端为 `central` | Grafana 中显示的服务器名称，只能使用字母、数字、点、下划线和连字符 |
| `XRAY_LOG` | `/var/log/x-ui/access.log` | Xray access log 路径 |
| `ENABLE_XRAY` | `auto` | `auto`、`true` 或 `false` |
| `LOKI_URL` | 无 | 远程 Loki Push API 的 HTTPS 地址 |
| `LOKI_USERNAME` | `alloy-agent` | Loki 反向代理认证用户名 |
| `METRICS_URL` | 无 | VictoriaMetrics Remote Write HTTPS 地址 |
| `METRICS_USERNAME` | `alloy-agent` | 指标反向代理认证用户名 |
| `XUI_API_URL` | 无 | 3x-ui `/panel/api/inbounds/list` 完整 HTTPS 地址 |
| `LOKI_RETENTION` | `168h` | 中心端 Loki 日志保留时间 |
| `METRICS_RETENTION` | `14d` | 中心端指标保留时间 |

## 验证与排查

中心服务器：

```bash
gla
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

采集服务器：

```bash
alloy
docker logs --tail=100 xray-alloy
```

常见情况：

- **Fail2ban 面板为 0**：不一定是故障，可能当前时间范围内没有 `Found`、`Ban` 或 `Unban` 事件。
- **UFW 面板无数据**：确认 UFW 已启用日志，并检查 `/var/log/ufw.log` 是否存在。
- **系统曲线为空**：检查是否设置了 `METRICS_URL`，以及指标域名的 SSL、Basic Auth 和 `/api/v1/write` 转发。
- **3x-ui API 状态不可用**：检查 API URL、Token、证书，以及采集服务器能否访问面板域名。
- **传入 `XUI_API_URL` 后仍未启用**：确认反斜杠是该行最后一个字符，后面不能有空格；也可以直接使用 `alloy` 菜单第 `11` 项。
- **Xray 面板无日志**：检查 `XRAY_LOG`、3x-ui 的访问日志设置和文件读取权限。
- **旧日志没有全部出现**：systemd journal 首次默认读取最近数据；GLA 主要保证安装后的持续采集。

## 数据保留与资源

- Loki 默认保留 7 天日志。
- VictoriaMetrics 默认保留 14 天指标。
- `30G` 中心服务器建议先保持默认，不要长期保存大量 Debug 日志。
- 采集端不会运行 Grafana、Loki 或 VictoriaMetrics，适合 `1C / 1G` 小型服务器。
- 3x-ui 指标默认每 30 秒采集一次，不会记录 API Token，也不会把来源 IP 设为 Loki Label。

## 安全说明

- Grafana、Loki、VictoriaMetrics 和 3x-ui API 都必须经过认证或限制来源 IP。
- 3x-ui API Token 具有完整管理权限，只保存在对应服务器。
- 不要在命令行参数中传入密码或 API Token；脚本会使用隐藏输入。
- 仪表盘中的来源 IP、用户名和访问目标属于敏感运维数据。
- 启用 UFW 前必须先放行当前 SSH 端口和必要业务端口，并用第二个 SSH 会话验证。
- 来源 IP 不写入 Loki Label，避免高基数拖慢 Loki。

## 项目文件

```text
deploy-xray-grafana-loki-alloy.sh  中心服务器安装与 gla 菜单
deploy-xray-alloy-collector.sh     采集服务器安装与 alloy 菜单
assets/xui_exporter.py             3x-ui API Prometheus 导出器
dashboards/xray-gateway.json       Xray / 3x-ui 流量仪表盘
dashboards/server-security.json    服务器安全与系统仪表盘
tests/test_xui_exporter.py         3x-ui 导出器测试
tests/test_generated_alloy_regex.sh Bash 到 Alloy 的正则转义测试
```

国家地图需要单独配置 MaxMind GeoLite2 等本地 GeoIP 数据库。本版本不会把来源 IP 发送给第三方在线查询服务。

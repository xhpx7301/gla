# GLA

GLA 是面向个人和小型 Linux 服务器的轻量观测项目，用一台中心服务器集中查看多台服务器的运行状态、安全事件、Xray 访问日志和 3x-ui 流量。

中心端使用 Grafana、Loki、VictoriaMetrics 和 Alloy；其他服务器通常只运行 Alloy，以及按需启用的 3x-ui 指标导出器。

## 快速了解

| 角色 | 安装内容 | 管理命令 |
| --- | --- | --- |
| 中心服务器 | Grafana、Loki、VictoriaMetrics、Alloy、预置仪表盘 | `gla` |
| 普通采集服务器 | Alloy：SSH、Fail2ban、防火墙日志和主机指标 | `alloy` |
| 3x-ui 采集服务器 | 普通采集能力 + Xray 日志 + 3x-ui API 流量 | `alloy` |

```text
HY-248 等采集服务器
  SSH / Fail2ban / 防火墙 / 系统指标 / Xray / 3x-ui API
                         |
                         | HTTPS + Basic Auth
                         v
HY-2314 中心服务器
  Loki（日志） + VictoriaMetrics（指标） -> Grafana（仪表盘）
```

项目默认提供三个 Grafana 仪表盘：

- `Xray 访问日志`：原始日志、最新访问记录、客户端、入站和访问目标统计。
- `Xray 网关流量与连接`：在线用户、3x-ui API 状态、指标最新时间、上下行速率、客户端和入站流量、来源 IP 连接数。
- `服务器安全事件与系统资源`：SSH 失败、Fail2ban、防火墙、指标最新时间、CPU、内存、网卡、SSH 端口入站和默认拒绝流量。

## 数据从哪里来

| 数据 | 来源 | 存储位置 |
| --- | --- | --- |
| Xray 访问记录、来源 IP、访问目标 | `/var/log/x-ui/access.log` | Loki |
| SSH 失败登录 | Debian/Ubuntu：systemd journal；Alpine：`/var/log/messages` | Loki |
| Fail2ban 发现、封禁、解封 | `/var/log/fail2ban.log` | Loki |
| UFW 拒绝记录 | `/var/log/ufw.log` | Loki |
| CPU、内存、网卡流量 | Alloy 内置 Unix exporter | VictoriaMetrics |
| SSH 端口入站流量 | UFW/iptables 或 SUF/nftables 宿主机计数器 | VictoriaMetrics |
| 防火墙默认拒绝流量 | UFW 默认 INPUT 策略或 SUF nftables input 链计数器 | VictoriaMetrics |
| 3x-ui 在线用户和上下行流量 | 3x-ui Panel API | VictoriaMetrics |

SSH 端口入站流量和防火墙默认拒绝流量仅在配置了指标写入，且 Debian/Ubuntu 的 UFW 或 Alpine 的 SUF nftables 规则已启用时自动采集。采集器每 30 秒读取仅计数规则生成聚合字节数，不会创建来源 IP 标签，也不改变放行或拒绝决定。SSH 端口流量包含正常会话和扫描；默认拒绝流量仅代表最终落入默认 INPUT 拒绝策略的流量。

Xray 来源 IP 的“连接次数”和 3x-ui 的“流量字节数”是两种不同口径。普通 access log 没有每条连接的字节数，因此项目不会把来源 IP 连接次数伪装成精确流量。

仪表盘表格会将 Grafana 原始字段名转换为中文，并默认按主要数值降序排列：SSH 使用“来源 IP / 国家/地区 / 省份 / 城市 / 失败次数”，Fail2ban 使用“封禁 IP / 国家/地区 / 省份 / 城市 / 封禁次数”，3x-ui 使用“客户端 / 流量”和“入站 / 端口 / 流量 / 协议”，Xray access log 使用“来源 IP / 国家/地区 / 省份 / 城市 / 连接次数”。“协议”用于区分 `vless`、`vmess` 等入站类型，不能替代“流量”；流量由独立数值列显示。

## 环境要求

- 中心服务器：Debian 或 Ubuntu。
- 采集服务器：Debian、Ubuntu，或使用 OpenRC 的 Alpine Linux。
- Docker 正常运行，并安装 `docker compose` 或 `docker-compose`。
- 中心服务器建议至少 `2C / 3G / 30G`。
- 采集服务器 `1C / 1G / 10G` 可以运行；建议配置 Swap。
- 服务器时间和时区应正确，建议启用 systemd-timesyncd、chrony 或其他 NTP 服务。
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

已有中心平台可以直接运行 `gla`，选择 `8. 配置或关闭本机 3x-ui API 流量采集`。输入 API 地址后脚本会安全询问 Token、重新部署导出器，并重启 Alloy 使抓取配置立即生效。

中央服务器采集自己的 3x-ui 时，导出器容器会保留 API 域名用于 HTTPS 证书校验，同时将该域名解析到 Docker 宿主机网关，避免服务器通过公网地址访问自身时发生回环超时。此行为只用于中央服务器的本机 3x-ui，不影响远程采集器。

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

Alpine 需要先准备 Bash、Docker Compose、OpenRC syslog；建议先使用 SUF Alpine 脚本配置 OpenSSH、`/var/log/messages`、Fail2ban 和 nftables：

```sh
apk add bash curl docker docker-cli-compose
rc-update add docker default
rc-service docker start
```

安装器会自动识别 Alpine。安全日志从 `/var/log/messages` 和 `/var/log/fail2ban.log` 读取；安全流量采集仅支持 SUF 创建的 `inet suf input` 链，并通过 OpenRC 服务 `gla-security-traffic` 每 30 秒更新一次指标。没有启用 SUF nftables 时，日志和主机指标仍可采集，防火墙流量指标会自动关闭。

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

文件权限为 `600`。安装后运行 `alloy` 打开采集端管理菜单。菜单顶部会显示 Alloy 容器、Xray 日志、安全日志、主机指标和 3x-ui 流量采集的当前状态。已安装的采集器也可以直接选择菜单 `11. 配置或关闭 3x-ui API 流量采集`，不需要手写环境变量。菜单 `6` 可分别查看 Alloy 与 3x-ui API 导出器日志；导出器容器处于“运行中”只代表进程已启动，是否成功访问面板 API 应以导出器日志和 Grafana 指标为准。

## 已有安装如何升级

以 HY-2314 为中心、HY-248 为采集端时：

1. 先在 HY-2314 运行 `gla`，选择编号 `1`。旧版菜单显示“安装或更新脚本并重新部署”，新版显示“更新配置、脚本与仪表盘”。
2. HY-2314 本身运行 3x-ui 时，运行 `gla` 并选择编号 `8`，输入完整 API 地址；无需在命令行传入 Token。

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

采集端 `alloy` 菜单的编号 `1` 只更新脚本与配置，并复用本地已有镜像；首次安装或本地缺少镜像时，Docker Compose 仍会自动拉取所需镜像。编号 `8` 才会从镜像仓库拉取并单独重建 Alloy，不会更新或重建 3x-ui API 导出器。

## `gla` 菜单说明

运行 `gla` 后，顶部会直接显示 Grafana、Loki、VictoriaMetrics、Alloy 和中央服务器本机 3x-ui 流量采集的状态。这里的“本机 3x-ui”不代表远程采集服务器；远程服务器的 3x-ui API 状态应在“Xray 网关流量与连接”仪表盘中查看。

| 选项 | 作用 |
| --- | --- |
| 更新配置、脚本与仪表盘 | 下载当前仓库脚本，重新生成配置并保留数据卷 |
| 服务控制 | 启停全部组件，或单独重启某个组件 |
| 查看运行状态与资源占用 | 查看容器状态、镜像、数据卷和日志文件占用 |
| 查看服务日志 | 分别查看五个组件的最近日志 |
| 查看访问地址、凭据与模块 | 查看 Grafana 地址、密码、内部接口和已安装仪表盘 |
| 更新容器镜像 | 拉取最新镜像并重建容器，不修改采集参数 |
| 卸载并删除全部数据 | 删除容器、配置和项目数据卷，执行前需要二次确认 |
| 配置或关闭本机 3x-ui API 流量采集 | 为中央服务器启用、修改或关闭 3x-ui Panel API 指标采集 |

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
| `ENABLE_SECURITY_TRAFFIC` | `auto` | `auto` 在 UFW/iptables 或 SUF/nftables 活跃且已配置指标写入时启用；`true` 强制启用；`false` 停止并清理计数器 |
| `XUI_API_URL` | 无 | 3x-ui `/panel/api/inbounds/list` 完整 HTTPS 地址 |
| `ENABLE_GEOIP` | `auto` | `auto`、`true` 或 `false`；检测到本地数据库时自动启用 |
| `GEOIP_DB_PATH` | 采集端为 `/opt/xray-alloy-collector/geoip/GeoLite2-City.mmdb`，中心端为 `/opt/xray-log-dashboard/geoip/GeoLite2-City.mmdb` | 本地 GeoLite2-City 数据库路径 |
| `GEOIP_MIRROR_URL` | `https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb` | 菜单自动下载使用的 GitHub 镜像地址 |
| `LOKI_RETENTION` | `168h` | 中心端 Loki 日志保留时间 |
| `METRICS_RETENTION` | `14d` | 中心端指标保留时间 |

## 验证与排查

### 启用第一阶段 GeoIP

项目支持通过 GitHub 镜像自动下载 `GeoLite2-City.mmdb`，也支持使用本地已有文件。

运行 `alloy` 或 `gla`，选择菜单 `1`。部署时如果没有检测到数据库，脚本会提供三个选项：

```text
1. 从 GitHub GeoLite.mmdb 镜像下载（无需 MaxMind 密钥）
2. 使用服务器上已有的 GeoLite2-City.mmdb
0. 跳过 GeoIP
```

选择 `1` 时，脚本从 `GEOIP_MIRROR_URL` 下载数据库，检查 HTTP 状态和文件大小后复制到 GLA 默认目录。该镜像不是 MaxMind 官方源，数据新鲜度、完整性和授权条款需要自行确认。选择 `2` 时，输入服务器上已有文件路径，例如 `/tmp/GeoLite2-City.mmdb`，脚本会自动复制到采集端 `/opt/xray-alloy-collector/geoip/` 或中心端 `/opt/xray-log-dashboard/geoip/`。选择 `0` 可跳过。重新部署后，新的 SSH、Fail2ban 和 Xray 日志会带有国家/地区、省份和城市字段；历史日志不会自动补齐。

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
- **Alpine SSH/Fail2ban 面板无数据**：确认 syslog 正在向 `/var/log/messages` 写入，并检查 `/var/log/fail2ban.log`；SUF Alpine 会配置这两个日志来源。
- **Alpine 防火墙流量无数据**：确认 `nft list chain inet suf input` 成功，并检查 `rc-service gla-security-traffic status`。
- **系统曲线为空**：检查是否设置了 `METRICS_URL`，以及指标域名的 SSL、Basic Auth 和 `/api/v1/write` 转发。
- **3x-ui API 状态不可用**：检查 API URL、Token、证书，以及采集服务器能否访问面板域名。
- **Xray 网关流量与连接的服务器列表只有 All**：更新中心和采集端脚本后重新部署。新版导出器会在每条 `xui_*` 指标中写入 `server` 标签；可用 `count by (server) (xui_exporter_up)` 验证。
- **中央服务器本机 3x-ui 显示超时**：更新中央脚本并重新部署，新版会让本机面板域名在导出器容器内直连宿主机网关，解决 Docker 访问本机公网地址的回环超时。
- **传入 `XUI_API_URL` 后仍未启用**：确认反斜杠是该行最后一个字符，后面不能有空格；也可以直接使用 `alloy` 菜单第 `11` 项。
- **Xray 面板无日志**：检查 `XRAY_LOG`、3x-ui 的访问日志设置和文件读取权限。
- **旧日志没有全部出现**：systemd journal 和 Alpine 文件日志首次默认读取最近数据；GLA 主要保证安装后的持续采集。

## 数据保留与资源

- Loki 默认保留 7 天日志。
- VictoriaMetrics 默认保留 14 天指标。
- `30G` 中心服务器建议先保持默认，不要长期保存大量 Debug 日志。
- 采集端不会运行 Grafana、Loki 或 VictoriaMetrics，适合 `1C / 1G` 小型服务器。
- 3x-ui 指标默认每 30 秒采集一次，不会记录 API Token，也不会把来源 IP 设为 Loki Label。
- GeoIP 默认关闭；可通过菜单 `1` 从 GitHub 镜像下载，或将 `GeoLite2-City.mmdb` 放到 `GEOIP_DB_PATH` 后重新部署。国家、省份和城市来自本地离线数据库，不会把 IP 发给在线查询服务。
- GeoIP 会优先读取 MMDB 中的 `zh-CN` 国家、地区和城市名称，缺少中文条目时回退英文。它只适合展示来源归属参考，代理、VPN、移动网络和云服务器的城市级结果可能不准确。Xray access log 仍统计连接次数，不会凭空产生 IP 级精确流量。

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
assets/security_traffic_collector.sh SSH 与防火墙聚合流量采集器
dashboards/xray-gateway.json       Xray / 3x-ui 流量仪表盘
dashboards/server-security.json    服务器安全事件与系统资源仪表盘
tests/test_xui_exporter.py         3x-ui 导出器测试
tests/test_generated_alloy_regex.sh Bash 到 Alloy 的正则转义测试
tests/test_security_traffic_collector.sh 安全流量采集器配置测试
tests/test_alpine_collector_config.sh Alpine/systemd 配置生成回归测试
```

原始来源 IP 会直接显示在表格中。第一阶段支持在采集阶段使用 GeoLite2-City 本地数据库离线解析国家/地区、省份和城市。默认下载地址是用户指定的 `P3TERX/GeoLite.mmdb` GitHub 镜像；如果未配置数据库，相关地理列会为空，原有 IP、失败次数、封禁次数和连接次数统计仍可用。

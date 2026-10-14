# GLA

GLA 是面向个人和小型 Linux 服务器的轻量观测项目，用一台中心服务器集中查看多台服务器的运行状态、安全事件、Xray 访问日志和 3x-ui 流量。

中心端运行 Grafana、Loki、VictoriaMetrics、Alloy 和 3x-ui API 采集服务。GLA 采集端只采集服务器安全事件与系统资源；所有 Xray 数据，包括中心服务器自己的 Xray，都由中心端通过 3x-ui API 收集。

## 快速了解

| 角色 | 安装内容 | 管理命令 |
| --- | --- | --- |
| 中心服务器 | Grafana、Loki、VictoriaMetrics、Alloy、统一 3x-ui API 采集服务 | `gla` |
| 普通采集服务器 | Alloy：SSH、Fail2ban、UFW 日志和主机指标 | `alloy` |
| 运行 3x-ui 的服务器 | Alloy 采集安全与系统资源；Xray 数据由中心端访问面板 API | `alloy` / 中心端 `gla` |

```text
采集端 Alloy --HTTPS + Basic Auth--> 中心端 Loki / VictoriaMetrics
  SSH / Fail2ban / UFW / 系统指标

各服务器 3x-ui API <---HTTPS + Token--- 中心端 API 采集服务
  Xray 日志 / 在线状态 / 流量                |
                                       Loki / VictoriaMetrics --> Grafana
```

项目默认提供三个 Grafana 仪表盘：

- `Xray 访问日志`：API 解析访问记录、最新访问记录、客户端、入站和访问目标统计。
- `Xray 网关流量与连接`：在线用户、3x-ui API 状态、指标最新时间、上下行速率、客户端和入站流量、来源 IP 连接数。
- `服务器安全事件与系统资源`：SSH 失败、Fail2ban、UFW、指标最新时间、CPU、内存、网卡、SSH 端口入站和 UFW 默认拒绝流量。

## 数据从哪里来

| 数据 | 来源 | 存储位置 |
| --- | --- | --- |
| Xray 访问记录、来源 IP、访问目标 | 中心端轮询各服务器的 3x-ui API（含中心服务器本机） | Loki |
| SSH 失败登录 | systemd journal | Loki |
| Fail2ban 发现、封禁、解封 | `/var/log/fail2ban.log` | Loki |
| UFW 拒绝记录 | `/var/log/ufw.log` | Loki |
| CPU、内存、网卡流量 | Alloy 内置 Unix exporter | VictoriaMetrics |
| SSH 端口入站流量 | UFW 活跃时的宿主机防火墙计数器 | VictoriaMetrics |
| UFW 默认拒绝流量 | UFW 默认 INPUT 拒绝策略的宿主机计数器 | VictoriaMetrics |
| 3x-ui 在线用户和上下行流量 | 3x-ui Panel API | VictoriaMetrics |

## 中心端通过 API 监控 3x-ui

在 `gla -> 2` 中，`1. 添加面板` 创建新配置，`2. 修改面板` 按 `1`、`2` 等编号选择已有面板，`3. 删除面板` 按编号删除。修改时显示当前名称、API 地址、日志开关、采集间隔和日志窗口；每项直接回车保留原值，Token 留空也保留。添加时使用默认 30 秒间隔、1000 条日志窗口并开启日志采集，名称、API 地址和 Token 必须填写。

3x-ui 3.3.0、3.3.1 和 3.5.0 可使用同一组接口。在中心服务器运行 `gla`，选择 `1` 更新部署，再选择 `2. 管理 3x-ui API 面板`，逐个输入服务器名称、完整 HTTPS 地址、Token 和日志采集选项。地址示例：`https://example.com/面板路径/panel/api/inbounds/list`。Token 从面板设置中的 API Token 功能创建；它有管理权限，应只通过 HTTPS 使用。面板配置保存在中心端的 `secrets/xui-panels.json`，Token 分别写入权限为 `600` 的文件。输入 Token 时直接显示字符，方便核对；面板列表不显示已保存的 Token。只监控 Xray 时，被监控服务器只需运行已有的 3x-ui；需要安全事件和系统资源时，再安装 GLA 采集端。中心服务器自己的面板也通过此菜单添加，名称应与 `SERVER_NAME` 一致。

中心端每个面板独立轮询入站、客户端、在线名单和访问日志。流量与在线状态写入 VictoriaMetrics；启用日志采集后，最近的解析访问记录经 Alloy 补充 GeoIP 写入 Loki。两个 Xray 仪表盘均可按服务器筛选。采集状态和日志窗口风险显示在仪表盘底部，服务错误可在 `gla -> 5. 查看服务日志 -> 5` 查看。旧采集端升级后会停用 Xray 文件日志和本地 3x-ui 导出器，中心端升级后也会停用 Xray 文件日志。为避免过渡期间重复计数，按下文升级顺序迁移。

使用 API 采集日志时，必须在 3x-ui/Xray 开启 access log，并确保面板能读取它。`/server/xraylogs/:count` 扫描当前日志文件，再返回最近 N 条解析记录；文件很大时轮询也会消耗服务器资源，应配置日志轮转。原始行和 `[api -> api]` 不会完整保留，接口没有增量游标。采集器使用持久化去重记录，Loki 写入失败时重试，并在窗口已满或与上次采集没有重叠时提示风险，但日志轮转、长时间断连和高吞吐仍可能漏采。默认每 30 秒取 1000 条，首次启用只回填最近 1 小时。API 状态正常代表请求和转发成功，不证明此时有新访问记录。来源 IP 的“连接数”始终是日志记录数，无法从 API 得到每条连接的精确字节数或持续时间。

SSH 端口入站流量和 UFW 默认拒绝流量仅在 UFW 已启用且配置了指标写入时自动采集。采集器每 30 秒使用仅计数、随后立即返回的防火墙链生成聚合字节数，不会创建来源 IP 标签，也不改变放行或拒绝决定。SSH 端口流量包含正常会话和扫描；UFW 默认拒绝流量仅代表最终落入默认 INPUT 拒绝策略的流量。

Xray 来源 IP 的“连接次数”和 3x-ui 的“流量字节数”是两种不同口径。普通 access log 没有每条连接的字节数，因此项目不会把来源 IP 连接次数伪装成精确流量。

仪表盘表格会将 Grafana 原始字段名转换为中文，并默认按主要数值降序排列：SSH 使用“来源 IP / 国家/地区 / 省份 / 城市 / 失败次数”，Fail2ban 使用“封禁 IP / 国家/地区 / 省份 / 城市 / 封禁次数”，3x-ui 使用“客户端 / 流量”和“入站 / 端口 / 流量 / 协议”，Xray access log 使用“来源 IP / 国家/地区 / 省份 / 城市 / 连接次数”。“协议”用于区分 `vless`、`vmess` 等入站类型，不能替代“流量”；流量由独立数值列显示。

## 环境要求

- Debian 或 Ubuntu。
- Docker 正常运行，并安装 `docker compose` 或 `docker-compose`。
- 中心服务器建议至少 `2C / 3G / 30G`。
- 采集服务器 `1C / 1G / 10G` 可以运行；建议配置 Swap。
- 服务器时钟应准确，建议启用 systemd-timesyncd 或其他 NTP 服务。GLA 的仪表盘、管理器诊断和日志查看统一使用 `Asia/Shanghai`（UTC+8）显示时间。
- 远程采集服务器只需要出站 HTTPS，不需要为 Alloy 开放公网入站端口。

## 首次安装中心服务器

```bash
SERVER_NAME=central-server \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-grafana-loki-alloy.sh)
```

安装后运行 `gla`，选择 `2. 管理 3x-ui API 面板`，添加中心服务器本机和其他服务器的面板。每台面板需提供完整 HTTPS API 地址和 Token，并在 3x-ui 中开启访问日志。中心端直接读取本机 access.log 的方式已移除。

中心服务器访问自己的面板域名若有公网回环问题，可在安装时设置 `XUI_LOCAL_HOST=panel.example.com`。采集服务将这个域名解析到 Docker 宿主机网关，同时保持原域名用于 HTTPS 证书校验。远程面板按正常 DNS 解析。

旧版中心服务器通过 `XUI_API_URL` 配置的本机面板，会在升级时自动迁移到统一列表并开启 API 日志采集。旧 Token 文件仍保留，统一列表已存在相同名称时保留现有配置。

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
SERVER_NAME=collector-server \
LOKI_URL=https://loki.example.com/loki/api/v1/push \
LOKI_USERNAME=alloy-agent \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh)
```

### 增加主机指标

已安装采集器时，运行 `alloy`，选择 `3. 配置采集模块 -> 2. 主机指标`，输入 VictoriaMetrics Remote Write 地址和认证用户名。重新部署时脚本会安全询问指标接口密码。

首次安装或自动化部署也可以使用环境变量：

```bash
SERVER_NAME=collector-server \
LOKI_URL=https://loki.example.com/loki/api/v1/push \
LOKI_USERNAME=alloy-agent \
METRICS_URL=https://metrics.example.com/api/v1/write \
METRICS_USERNAME=alloy-agent \
bash <(wget -qO- https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh)
```

采集端只包含安全日志、主机指标、GeoIP 安全日志增强和 SSH/UFW 聚合流量四个模块。运行 `alloy`，选择 `3. 配置采集模块` 调整；菜单 `8` 查看 Alloy 日志。Xray 访问日志、在线状态和流量统一在中心服务器的 `gla -> 2` 配置，采集端不再下载或运行 3x-ui 导出器，也不询问面板 Token。

菜单 `9. 查看采集器设置` 会用中文显示服务器标识、日志与指标写入地址、认证用户名、各模块状态、日志和数据库路径、SSH 端口及采集间隔。进入任一模块配置页也会先显示该模块当前使用的配置和依赖状态。认证密码只显示“已配置/未配置”，不会回显内容。

菜单 `7. 检测中央写入状态` 会检查 Alloy 容器和最近 10 分钟的 Loki、VictoriaMetrics 写入错误。按提示临时输入 Basic Auth 密码后，还会验证中央服务连接与认证、查询本服务器最近日志，并检查主机指标的最新样本时间。检测密码只在内存中使用，不会保存或回显；HTTP `401/403` 会明确提示检查用户名和密码。

Alloy 内部日志原本使用 UTC 的 `Z` 时间，管理菜单会在显示时转换为 UTC+8。Fail2ban 这类不带时区的文本日志会按 `Asia/Shanghai` 解析；API 访问记录保留面板返回的事件时间并按 UTC+8 显示；Grafana 预置仪表盘也固定使用 UTC+8。修改时区不会重写 Loki 中已经采集的历史日志。

模块菜单会在重新部署前检查依赖并给出提示：主机指标需要 VictoriaMetrics；安全日志需要 Loki；GeoIP 需要 `GeoLite2-City.mmdb`；安全日志需要 systemd journal；SSH/UFW 聚合流量还需要主机指标、安全日志、systemd、iptables 和已启用的 UFW。

## 已有安装如何升级

中心服务器和采集服务器均为已有安装时，按以下顺序升级：

1. 在中心服务器运行 `gla`，选择 `1` 更新。新版停用本机 Xray 文件采集；旧本机 API 配置自动迁移。
2. 在各采集服务器运行 `alloy`，选择 `1` 更新。新版生成安全与系统资源配置，并用 Compose 的 `--remove-orphans` 移除该项目旧的 3x-ui 导出器。旧 Token 不会自动上传到中心端，需要自行在中心端配置。
3. 在中心端 `gla -> 2` 添加所有服务器的面板，服务器名称与对应采集端 `SERVER_NAME` 一致。填写 Token 并启用访问日志采集。若中心端已有这些面板的 API 日志采集配置，可先关闭其日志采集，待对应采集端升级后再开启，以减少过渡期间重复采集。
4. 需要系统资源时，在采集端 `3. 配置采集模块 -> 2. 主机指标` 配置 VictoriaMetrics 地址；随后检查安全仪表盘及两个 Xray 仪表盘。

历史 Grafana、Loki、VictoriaMetrics 数据卷保留。采集端旧的 Xray 环境变量会被忽略，新菜单不再提供相应开关。远程写入密码不会写入 `.install.env`，更新时需要重新输入。中心端和采集端都升级后，不再产生文件日志与 API 日志的重复采集。

## `gla` 菜单说明

运行 `gla` 后，顶部会显示 Grafana、Loki、VictoriaMetrics、Alloy 和中心端 3x-ui API 采集服务的状态。每台面板的 API、日志状态应在 Xray 仪表盘中查看；容器“运行中”本身不代表面板可访问。

| 选项 | 作用 |
| --- | --- |
| 更新配置、脚本与仪表盘 | 下载当前仓库脚本，重新生成配置并保留数据卷 |
| 管理 3x-ui API 面板 | 在中心端管理本机和远程面板的流量、在线状态与访问日志采集 |
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
| `LOKI_URL` | 无 | 远程 Loki Push API 的 HTTPS 地址 |
| `LOKI_USERNAME` | `alloy-agent` | Loki 反向代理认证用户名 |
| `METRICS_URL` | 无 | VictoriaMetrics Remote Write HTTPS 地址 |
| `METRICS_USERNAME` | `alloy-agent` | 指标反向代理认证用户名 |
| `ENABLE_HOST_METRICS` | `auto` | `auto` 在配置 `METRICS_URL` 时兼容启用；`true` 或 `false` 控制采集端主机指标 |
| `ENABLE_SECURITY_TRAFFIC` | `auto` | `auto` 仅在主机指标、安全日志、UFW 和指标写入均可用时启用；`true` 强制启用；`false` 停止并清理计数器 |
| `XUI_LOCAL_HOST` | 无 | 中心服务器本机面板域名；仅此域名映射到 Docker 宿主机网关 |
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
- **系统曲线为空**：检查是否设置了 `METRICS_URL`，以及指标域名的 SSL、Basic Auth 和 `/api/v1/write` 转发。
- **3x-ui API 状态不可用**：检查 API URL、Token、证书，以及中心服务器能否访问面板域名。
- **Xray 网关流量与连接的服务器列表只有 All**：在中心端 `gla -> 2` 添加面板，检查 API 采集服务和指标写入；可用 `count by (server) (xui_exporter_up)` 验证。
- **中心服务器本机 3x-ui 显示超时**：设置 `XUI_LOCAL_HOST=本机面板域名` 后重新部署，使该域名在 API 采集容器内直连宿主机网关；同时确认宿主机上的 HTTPS 反向代理可从 Docker 网络访问。
- **面板尚未开始采集**：在中心端 `gla -> 2` 确认面板已添加，再查看 `gla -> 5 -> 5` 的 API 采集服务日志。
- **Xray 面板无日志**：在中心端检查面板 API 配置、Token、日志状态和采集服务错误；确认 3x-ui 已启用访问日志且能读取它。
- **旧日志没有全部出现**：systemd journal 首次默认读取最近数据；GLA 主要保证安装后的持续采集。

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
- 3x-ui API Token 具有完整管理权限，统一采集所用 Token 保存在中心服务器。
- 不要在命令行参数中传入密码或 API Token；通过脚本提示输入。密码使用隐藏输入，API Token 输入时直接显示字符以便核对。
- 仪表盘中的来源 IP、用户名和访问目标属于敏感运维数据。
- 启用 UFW 前必须先放行当前 SSH 端口和必要业务端口，并用第二个 SSH 会话验证。
- 来源 IP 不写入 Loki Label，避免高基数拖慢 Loki。

## 项目文件

```text
deploy-xray-grafana-loki-alloy.sh  中心服务器安装与 gla 菜单
deploy-xray-alloy-collector.sh     采集服务器安装与 alloy 菜单
assets/xui_exporter.py             中心端 3x-ui API 指标与日志采集服务
assets/security_traffic_collector.sh SSH/UFW 聚合流量采集器
dashboards/xray-gateway.json       Xray / 3x-ui 流量仪表盘
dashboards/server-security.json    服务器安全事件与系统资源仪表盘
tests/test_xui_exporter.py         3x-ui 导出器测试
tests/test_generated_alloy_regex.sh Bash 到 Alloy 的正则转义测试
tests/test_security_traffic_collector.sh 安全流量采集器配置测试
```

原始来源 IP 会直接显示在表格中。第一阶段支持在采集阶段使用 GeoLite2-City 本地数据库离线解析国家/地区、省份和城市。默认下载地址是用户指定的 `P3TERX/GeoLite.mmdb` GitHub 镜像；如果未配置数据库，相关地理列会为空，原有 IP、失败次数、封禁次数和连接次数统计仍可用。

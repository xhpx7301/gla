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

两个 Xray 仪表盘都有“客户端分组”筛选，直接读取 3x-ui 客户端的 `group` 字段，支持多选。选择“全部服务器 + 同一分组”会汇总各面板该组的客户端；选择某台服务器后只看该面板。同名分组按名称汇总，客户端与分组的匹配在各面板内独立完成。未设置分组的客户端显示为“未分组”；日志缺少客户端或客户端已删除、无法匹配时显示为“未识别客户端”。网关仪表盘的在线人数、客户端流量及两个仪表盘的访问记录和连接次数都支持分组筛选；API 健康状态按服务器展示，入站总流量仍包含该入站全部分组，并在表格中标明。

分组标签从升级后新采集的数据开始生效。历史日志不会补写分组，选择“全部”仍可查看旧数据。修改客户端分组后，新日志与指标使用新分组，历史数据保留原标签；计数器更换分组标签后需要积累新样本，速率可能暂时为空。日志记录的分组以采集时面板返回的配置为准，客户端 API 临时不可用时使用上次成功获取的分组映射。

在 `gla -> 2` 中，`1. 添加面板` 创建新配置，`2. 修改面板` 按 `1`、`2` 等编号选择已有面板，`3. 删除面板` 按编号删除。修改时显示当前名称、API 地址、日志开关、采集间隔和日志窗口；每项直接回车保留原值，Token 留空也保留。添加时使用默认 30 秒间隔、1000 条日志窗口并开启日志采集，名称、API 地址和 Token 必须填写。

3x-ui 3.3.0、3.3.1 和 3.5.0 可使用同一组接口。在中心服务器运行 `gla`，选择 `1` 更新部署，再选择 `2. 管理 3x-ui API 面板`，逐个输入服务器名称、完整 HTTPS 地址、Token 和日志采集选项。地址示例：`https://example.com/面板路径/panel/api/inbounds/list`。Token 从面板设置中的 API Token 功能创建；它有管理权限，应只通过 HTTPS 使用。面板配置保存在中心端的 `secrets/xui-panels.json`，Token 分别写入权限为 `600` 的文件。输入 Token 时直接显示字符，方便核对；面板列表不显示已保存的 Token。只监控 Xray 时，被监控服务器只需运行已有的 3x-ui；需要安全事件和系统资源时，再安装 GLA 采集端。中心服务器自己的面板也通过此菜单添加，名称应与 `SERVER_NAME` 一致。

中心端每个面板独立轮询入站、客户端、在线名单和访问日志。流量与在线状态写入 VictoriaMetrics；启用日志采集后，最近的解析访问记录经 Alloy 补充 GeoIP 写入 Loki。两个 Xray 仪表盘均可按服务器筛选。采集状态和日志窗口风险显示在仪表盘底部，服务错误可在 `gla -> 6. 查看服务日志 -> 5` 查看。旧采集端升级后会停用 Xray 文件日志和本地 3x-ui 导出器，中心端升级后也会停用 Xray 文件日志。为避免过渡期间重复计数，按下文升级顺序迁移。

中心端可在 `gla -> 3. 管理 Telegram Bot` 配置机器人。输入 BotFather 创建的 Token 和允许访问的 Telegram 用户 ID（可用逗号分隔多个 ID；也可以在机器人里发送 `/id` 查询）；Token 保存为权限 `600` 的独立文件，不写入 Compose。机器人使用长轮询，不需要开放公网入站端口。启动后会自动注册 Telegram 原生斜杠命令菜单：`/start`、`/menu`、`/help`、`/status`、`/id`、`/groups`、`/online`、`/traffic`、`/online_notify`、`/traffic_notify`、`/watch`、`/unwatch`、`/watches`。`/start` 或 `/menu` 会在聊天页面发送主菜单消息，操作按钮附在这条消息下方，不会占用输入框下方的键盘区域。主菜单的“上线通知”和“流量通知”可分别管理开关、查看订阅分组、添加或移除分组及发送测试消息；也可继续使用 `/watch 分组` 和 `/unwatch 分组` 管理上线订阅。`/id` 仅返回发送者自己的 Telegram 用户 ID 和当前 Chat ID，不受白名单限制；其余命令仅对白名单用户开放。`/groups` 列出当前分组，`/online [分组]` 查看在线客户端，`/traffic [分组]` 查看累计上行/下行/总流量，`/watches` 分类别显示上线通知和流量通知的订阅分组及开关状态；`/status` 显示面板 API 状态和在线客户端数。首次启动只建立在线状态基线，不会把已有在线客户端误报为刚上线；状态、订阅、上线通知冷却和流量通知设置保存在中心端 `xui-state` 卷。流量是 3X-UI API 返回的累计值，机器人不把它当作实时速率。

上线通知菜单可开关通知、查看订阅分组、逐个新增或移除分组，并发送测试通知。关闭通知会保留分组订阅，重新开启后继续监控。测试从已订阅分组的当前客户端中选择一个样例，立即发送含 IP、归属地和关联入站的模拟通知，即使客户端当前离线也可测试；消息会标明当前状态，不写入在线状态、确认次数或冷却记录。也可发送 `/online_notify test` 测试，或用 `/online_notify` 打开设置。通知默认按每个聊天、面板和客户端分别冷却 60 分钟；冷却期内该客户端再次上线不重复通知，不同客户端互不影响。冷却记录在 Telegram 消息发送成功后持久化，机器人重启后继续生效。可在 `secrets/telegram-config.json` 中设置 `notification_cooldown_seconds`（允许 `0` 到 `86400` 秒，`0` 表示关闭冷却）；旧默认值 `600` 秒和 `1800` 秒会升级为 `3600` 秒，其他自定义值保留；通过菜单更新 Bot 配置会保留该设置。

上线通知要求同一客户端在连续十次不同的成功 API 采集结果中均在线。前九次只记录待确认，第十次仍在线才通知；中间采集到离线会重新计数，重复读取同一份结果不计数，API 失败不计数也不视为离线。默认采集间隔为 30 秒，通知通常比第一次发现在线晚约 270 秒；FlClash 等延迟测试造成的短暂在线不容易触发通知，连续在线达到十次采集才会通知。在线查询仍直接显示最新采集结果，确认后发送通知继续受上述冷却限制。

客户端上线通知会按需调用该客户端的 3X-UI IP 日志接口，显示最近记录 IP、日志时间以及离线数据库解析的归属地和网络。IP 单独使用代码格式，可直接点击 IP 复制 IPv4 或 IPv6 地址；无记录时不显示 IP。3.3.0/3.3.1 的字符串列表和 3.5.0 的对象列表都会解析；不带时区的日志时间按面板的北京时间（UTC+8）显示，不再额外加 8 小时，带时区的时间及 Unix 秒/毫秒时间戳统一转换为 UTC+8。IP 日志是历史记录，不能保证就是触发本次上线的连接 IP。GeoLite2-City 有省级记录和城市时中国地址显示为“广东深圳”，只有省份时显示为“中国广东”，无法识别省份时显示“中国大陆”；香港、澳门、台湾只显示地区名，不重复附加城市。境外优先显示国家/地区与城市。GeoLite2-ASN 用于显示中国三大运营商或境外网络组织，识别为中国电信、中国联通、中国移动时字段名显示“运营商”，其他网络仍显示“网络组织”。数据库缺失或查询失败时不影响上线通知，只会显示 IP 或“暂无记录”。IP 和归属地通过本机数据库解析，不会发送给第三方查询服务。客户端 IP 日志可能包含敏感信息，请限制 Bot 白名单和数据库/备份访问。

通知同时显示该客户端在 3X-UI 配置中关联的入站名称。只有一个入站时名称与“关联入站”在同一行，多个入站时换行逐个列出；这是面板配置关联关系，3X-UI 在线名单接口不会提供本次会话实际使用的入站，因此不能据此判断当前连接的具体入站。面板入站配置暂时无法读取时显示“未找到配置关联”。

升级机器人后发送 `/start` 可清除旧版输入框键盘。旧按钮发送的完整文字也会映射到对应命令，并自动清除旧键盘、显示消息内菜单。新菜单按钮通过回调执行与斜杠命令相同的功能，点击时不会在聊天里发送一条用户命令消息。

`/help` 会发送简短的功能介绍，并在消息下方提供操作按钮。点击“命令说明”或发送 `/commands` 可查看完整命令、参数用法和分组查询示例。

“📊 流量通知”按钮或 `/traffic_notify` 可配置每日流量通知：按北京时间设置通知时间（默认每天 20:00），开启/关闭通知，查看订阅分组，并把指定分组加入或移出通知列表。日报默认与分组累计流量查询一样，将同名分组跨面板汇总，按累计总量排序，分行显示上行、下行和总计，并显示较上一次成功通知的新增流量；第一次通知显示“首次记录”。设置中的“开启分组面板明细”可在每个分组汇总下，额外列出该分组在各面板的累计和新增流量，组内面板按累计总量排序；只展示已订阅的通知分组。明细默认关闭，设置按聊天保存，重启后保留。也可使用 `/traffic_notify panels on` 或 `/traffic_notify panels off`。切换明细不会重发当天日报或改变流量对比基线。

设置中提供“发送测试通知”，也支持直接发送 `/traffic_notify test`，使用与日报相同的分组汇总和面板明细设置预览当前数据与新增量；测试不会更新“上次通知”基线或当天发送状态。其他命令包括 `/traffic_notify on`、`/traffic_notify off`、`/traffic_notify time 20:00`、`/traffic_notify groups`、`/traffic_notify add 分组` 和 `/traffic_notify remove 分组`。新增量先按分组、面板分别计算，再汇总，流量计数重置时该项新增按零处理，不抵消其他面板的增长。部分数据首次出现时会提示新增仅统计已有记录的流量。面板 API 暂时不可用时不计入本次累计和新增量，保留其上次成功记录，成功发送后才更新可用数据的对比基线。

“📈 累计流量”或 `/traffic [分组]` 默认显示分组汇总，消息下方的“查看分组面板明细”按钮可打开独立的明细查询，按“分组 → 各面板”列出累计上行、下行和总计，再点击“查看分组累计流量”返回。也支持 `/traffic_panels [分组]`；两种视图切换时保留分组筛选。未指定分组时展示所有分组及各自的面板流量，分组按跨面板累计总量排序，组内面板按该分组累计总量排序。这些查询不受日报明细开关影响，也不会改变通知对比基线。

实际修改流量通知时间后，会按新时间重新安排通知，即使当天已经发过日报也会再次发送；重复保存相同时间不会重复发送。上一次成功通知的流量对比基线保留。若新时间当天已经过去，机器人会在下一次检查时补发；定时检查通常有数十秒延迟，面板全部不可用时会等待恢复后再发送。

Telegram 左下角的原生菜单按钮由 Telegram 客户端控制文字，Bot API 不能把它强制改成自定义中文；机器人已注册 `/menu` 命令，并在聊天消息中提供“📊 流量通知”等内联菜单按钮。这样不需要恢复输入框下方的旧键盘。

状态、在线客户端和流量查询使用图标与分行字段展示，底部标注 UTC+8 查询时间（不是面板数据的采集时间）。每个在线客户端单独列出客户端名称、分组、面板及累计上下行/总流量；分组流量按累计总量降序排列。面板不可用时会提示该面板未计入结果。较长的查询结果自动分条发送，避免截断客户端和分组信息。

使用 API 采集日志时，必须在 3x-ui/Xray 开启 access log，并确保面板能读取它。`/server/xraylogs/:count` 扫描当前日志文件，再返回最近 N 条解析记录；文件很大时轮询也会消耗服务器资源，应配置日志轮转。原始行和 `[api -> api]` 不会完整保留，接口没有增量游标。采集器使用持久化去重记录，Loki 写入失败时重试，并在窗口已满或与上次采集没有重叠时提示风险，但日志轮转、长时间断连和高吞吐仍可能漏采。默认每 30 秒取 1000 条，首次启用只回填最近 1 小时。API 状态正常代表请求和转发成功，不证明此时有新访问记录。来源 IP 的“连接数”始终是日志记录数，无法从 API 得到每条连接的精确字节数或持续时间。

## 轻量 SSH 登录告警

SSH 登录告警可不在子服务器运行 Alloy。PAM 在 SSH 会话成功打开时触发本地脚本，脚本用 `systemd-run` 异步发送一条 HTTPS 事件到中心端；发送失败不会阻止 SSH 登录。中心端将事件写入现有 Loki，并按服务器独立的 CIDR 白名单决定是否向 Telegram Bot 授权用户发送告警。Grafana 的“SSH 登录记录”仪表盘可按服务器筛选全部成功会话。

在中心服务器运行 `gla` 并选择“3. 管理 SSH 登录告警”，使用“管理可信来源 IP 组”查询、添加、修改或删除常用公网 IP/CIDR；添加服务器或修改服务器的可信来源时，通过编号多选已有 IP 组即可复用。部署时脚本会自动发现运行中的 Nginx Proxy Manager，并将其接入 GLA 使用的共享 Docker 网络，使其可访问 `gla-xui-exporter:9105`。每台服务器自动使用独立随机密钥，安全保存在中心端 `secrets/ssh-login-config.json` 中；不需要手动编辑配置文件或重启容器。菜单状态栏会显示已登记的服务器数量。

接收地址要求指向 Nginx Proxy Manager 的 HTTPS Proxy Host：转发主机 `gla-xui-exporter`、端口 `9105`，域名路径由该 Host 根路径转发。公网访问必须启用 TLS；不要配置会重写 `/api/ssh-login` 路径的规则。每个可信来源 IP 组可关联多个 CIDR；服务器未选择任何组时，该服务器的所有成功登录都会通知。删除 IP 组会自动解除所有服务器对它的引用。菜单生成的安装命令需在对应子服务器以 root 执行。

安装脚本会在 `/etc/pam.d/sshd` 增加可选的 PAM session hook，并启用 OpenSSH `ExposeAuthInfo`，以便识别密钥、密码和交互式认证；密钥保存在 root-only 的 `/etc/gla/ssh-login.env`。告警会显示服务器 SSH 端口，并在仪表盘保留客户端来源端口。卸载时运行 `bash <(curl -fsSL https://raw.githubusercontent.com/xhpx7301/gla/main/install-ssh-login-agent.sh) uninstall`，PAM 原文件备份保留在 `/etc/pam.d/sshd.gla-backup`。建议先保持当前 SSH 管理会话，再用第二个会话验证配置，避免误配置 PAM 影响登录。

此方案记录成功打开的 SSH 会话，不记录认证失败。若 OpenSSH 无法提供认证信息文件，认证方式会显示为未知。Loki 只用 `job` 和 `server` 作标签，IP 与用户名留在 JSON 日志正文中，避免高基数标签。首次启用不会补录历史事件。

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
| 管理 Telegram Bot | 配置 Bot Token 和用户白名单；Bot 支持分组上线通知、在线客户端和累计流量查询 |
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
| `ENABLE_ASN` | `auto` | 中心端为 `auto`、`true` 或 `false`；控制 Telegram IP 归属中的 ASN 网络信息 |
| `GEOIP_DB_PATH` | 采集端为 `/opt/xray-alloy-collector/geoip/GeoLite2-City.mmdb`，中心端为 `/opt/xray-log-dashboard/geoip/GeoLite2-City.mmdb` | 本地 GeoLite2-City 数据库路径 |
| `GEOIP_MIRROR_URL` | `https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb` | 菜单自动下载使用的 GitHub 镜像地址 |
| `ASN_DB_PATH` | `/opt/xray-log-dashboard/geoip/GeoLite2-ASN.mmdb` | 中心端 Telegram IP 归属显示使用的本地 ASN 数据库 |
| `ASN_MIRROR_URL` | `https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-ASN.mmdb` | 中心端 ASN 数据库自动下载地址 |
| `LOKI_RETENTION` | `168h` | 中心端 Loki 日志保留时间 |
| `METRICS_RETENTION` | `14d` | 中心端指标保留时间 |

## 验证与排查

### 启用第一阶段 GeoIP

项目支持通过 GitHub 镜像自动下载 `GeoLite2-City.mmdb`，也支持使用本地已有文件。中心端部署时还会单独询问是否配置 `GeoLite2-ASN.mmdb`，用于 Telegram 上线通知的运营商/网络组织显示。

运行 `alloy` 或 `gla`，选择菜单 `1`。部署时如果没有检测到数据库，脚本会提供三个选项：

```text
1. 从 GitHub GeoLite.mmdb 镜像下载（无需 MaxMind 密钥）
2. 使用服务器上已有的 GeoLite2-City.mmdb
0. 跳过 GeoIP
```

选择 `1` 时，脚本从 `GEOIP_MIRROR_URL` 下载数据库，检查 HTTP 状态和文件大小后复制到 GLA 默认目录。该镜像不是 MaxMind 官方源，数据新鲜度、完整性和授权条款需要自行确认。选择 `2` 时，输入服务器上已有文件路径，例如 `/tmp/GeoLite2-City.mmdb`，脚本会自动复制到采集端 `/opt/xray-alloy-collector/geoip/` 或中心端 `/opt/xray-log-dashboard/geoip/`。选择 `0` 可跳过。重新部署后，新的 SSH、Fail2ban 和 Xray 日志会带有国家/地区、省份和城市字段；历史日志不会自动补齐。

中心端 ASN 提示中选择 `1` 会从 `ASN_MIRROR_URL` 下载 `GeoLite2-ASN.mmdb`；选择 `2` 可输入已有数据库路径；选择 `0` 跳过。P3TERX 仓库提供 ASN 文件，但不是 MaxMind 官方源，使用前应自行确认更新频率与许可条款。中心端会把 City 和 ASN 数据库只读挂载给 exporter；构建 exporter 镜像时安装固定版本的 `maxminddb` Python reader。被监控服务器无需安装数据库或额外程序。

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
- **面板尚未开始采集**：在中心端 `gla -> 2` 确认面板已添加，再查看 `gla -> 6 -> 5` 的 API 采集服务日志。
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

# ProxyNet — 异地组网代理管理工具

基于终端 TUI 的跨地域代理节点管理与组网工具。支持 7 种代理协议的分享链接一键导入，底层使用 [sing-box](https://github.com/SagerNet/sing-box) 作为代理核心，通过分流路由实现内网 IP 走异地节点组网、公网 IP 直连或转发到其他本地代理。

```
┌─ ProxyNet 异地组网 ─────────────────────────────────────────────┐
│ Engine: 🟢 Running | Proxy: 127.0.0.1:10810 | SysProxy: OFF    │
├─────────────────────────────────────────────────────────────────┤
│ Nodes                           │ Details                      │
│ 🟢 Test-Node  VLESS  45ms      │ Name: Test-Node              │
│                                 │ Type: VLESS                  │
│                                 │ Host: andyserv.com:50081     │
│ [i] Import  [d] Delete  [t] Test│                              │
│ [s] Start   [r] Routing [p] Sys │ ⚙️ Engine Control            │
│ [l] Logs    [q] Quit            │ ▶ Start    ⏹ Stop           │
│                                 │                              │
│                                 │ 📋 Open Log Viewer           │
└─────────────────────────────────┴──────────────────────────────┘
```

## 功能

| 功能 | 说明 |
|------|------|
| **7 种协议** | SS / VMess / Trojan / VLESS / Hysteria2 / TUIC |
| **分享链接导入** | 粘贴 `ss://` `vmess://` `trojan://` `vless://` `hysteria2://` `hy2://` `tuic://` 自动识别 |
| **分流路由** | 内网 IP 走异地节点，公网 IP 可选直连 / 走节点 / 转发到本地代理 |
| **系统代理** | `p` 键一键开关 Windows 系统代理，退出自动关闭 |
| **TUN 模式** | 全局代理（含 ICMP ping），需管理员权限 |
| **浮动日志** | `l` 键弹出实时日志窗口，格式：`源IP:端口 -> 目标:端口 [策略]` |
| **崩溃自愈** | sing-box 异常退出自动重启（最多 3 次） |
| **零依赖启动** | 首次运行自动装 pip 包 + 下载 sing-box 到 `bin/` |

## 快速开始

### 要求
- Python 3.10+
- Windows / Linux / macOS

### 启动

```bash
# Windows 双击
run.bat

# 或命令行
python main.py

# Web 模式（可选）
python main.py --web
```

首次运行自动：
1. 检测 Python 版本
2. `pip install` 缺失依赖
3. 下载 sing-box 到 `bin/` 目录

### 使用流程

1. 按 `i` → 粘贴分享链接 → 导入节点
2. 按 `r` → 配置路由：
   - **左列**：内网 IP 走哪个节点（组网）
   - **右列**：公网 IP 走 Direct / 节点 / IP Forward
3. 按 `s` → 启动代理（监听 `127.0.0.1:10810`）
4. 按 `p` → 开系统代理，浏览器自动走代理
5. 按 `l` → 查看实时连接日志

## 快捷键

| 按键 | 功能 |
|------|------|
| `i` | 导入分享链接（支持批量粘贴） |
| `e` | 编辑选中节点 |
| `d` | 删除选中节点 |
| `t` | 测试选中节点延迟 |
| `T` | 测试全部节点延迟 |
| `r` | 分流路由配置 |
| `s` | 启动 / 停止代理引擎 |
| `p` | 开关 Windows 系统代理 |
| `l` | 浮动日志窗口 |
| `q` | 退出（自动关系统代理 + 停引擎） |

## 路由配置

按 `r` 打开双列配置界面：

```
左列                        右列
🏠 Private IP Traffic      🌐 Public IP Traffic
  [节点 / Direct]            [Direct / 节点 / IP Forward]
                              ↓ 选 IP Forward 时展开：
🔧 TUN Mode                 🔀 IP Forward Proxy
  [开关]                      Local: [127.0.0.1:10808]
                              Type: [SOCKS5]
```

| 公网模式 | 行为 |
|---------|------|
| Direct | 公网直连 |
| 选节点 | 公网全部走该节点 |
| IP Forward | 公网全部转发到其他代理软件 |

## 日志格式

```
[2026/06/01 12:46] [INFO] 127.0.0.1:2220 -> beacons.gvt2.com:443 [PROXY: socks]
[2026/06/01 12:46] [INFO] 127.0.0.1:4467 -> 192.168.1.1:80 [PROXY: vless]
[2026/06/01 12:46] [ERROR] 192.168.1.1:80 via vless: connection refused
```

## 项目结构

```
proxy_net/
├── main.py              # 入口 (TUI 默认, --web 切换)
├── bootstrap.py         # 环境自举 (装依赖 + 下载 sing-box)
├── run.bat              # Windows 一键启动
├── .gitignore
├── models/node.py       # 数据模型 (7 协议 + 路由配置)
├── parser/              # 分享链接解析器 (ss/vmess/trojan/vless/hysteria2/tuic)
├── storage/manager.py   # JSON 持久化
├── core/
│   ├── config_generator.py  # sing-box config.json 生成
│   ├── engine.py            # sing-box 进程管理 (含自动重启)
│   └── monitor.py           # 节点延迟检测
├── tui/app.py           # Textual TUI (仪表盘/导入/路由/日志)
└── web/                 # Web UI (可选, --web)
    ├── server.py
    └── templates/index.html
```

## 数据存储

- **Windows**: `%APPDATA%/proxynet/`
- **Linux**: `~/.config/proxynet/`
- **macOS**: `~/Library/Application Support/proxynet/`

文件：`nodes.json`（节点）、`config.json`（路由配置）

## 注意事项
- **务必**在代理服务端把出站路由**geoip:private**设置为**direct**而不是blocked

## 许可证

MIT

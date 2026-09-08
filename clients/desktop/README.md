# cagent Desktop

基于 Tauri 2 的桌面客户端。

## 前置要求

1. **Rust 环境**
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```

2. **系统依赖** (macOS)
   - Xcode Command Line Tools: `xcode-select --install`

3. **Node.js 依赖**
   ```bash
   cd clients/desktop
   npm install
   ```

## 开发模式

```bash
# 启动 Python 后端（终端 1）
cd /path/to/cagent
cagent-web

# 启动 Tauri 桌面应用（终端 2）
cd clients/desktop
npm run tauri:dev
```

## 构建生产版本

```bash
cd clients/desktop
npm run tauri:build
```

构建产物位于 `src-tauri/target/release/bundle/`。

## 项目结构

```
clients/desktop/
├── package.json           # Node.js 依赖
├── vite.config.ts         # Vite 配置（复用 web/frontend）
├── tsconfig.json          # TypeScript 配置
└── src-tauri/
    ├── Cargo.toml         # Rust 依赖
    ├── tauri.conf.json    # Tauri 配置
    ├── capabilities/      # 权限配置
    ├── src/
    │   ├── main.rs        # 入口
    │   └── lib.rs         # Tauri 应用逻辑
    └── icons/             # 应用图标
```

## 功能特性

- ✅ 复用 Web 前端（React + Vite）
- ✅ 原生窗口（可调整大小、最小化）
- ✅ 自动启动 Python 后端
- ✅ SSE 实时事件流
- ⏳ 系统托盘（待实现）
- ⏳ 全局快捷键（待实现）
- ⏳ 自动更新（待实现）

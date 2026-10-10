# 部署到 Streamlit Community Cloud 指南

## 一、Streamlit Cloud 的特性与影响

| 特性 | 对项目的影响 | 本项目适配 |
| --- | --- | --- |
| 文件系统**临时性**（重启/空闲回收后清空） | SQLite 数据库会丢失 | ✅ 已支持外部 PostgreSQL（DATABASE_URL） |
| Linux 容器（无 Windows 字体） | PDF 报告中文会乱码 | ✅ 已内置开源字体 assets/fonts/wqy-microhei.ttc |
| 无 .env 文件 | 配置走 **Streamlit secrets** | ✅ config.py 已支持 st.secrets 读取 |
| 1GB 内存 / 应用休眠 | OCR 依赖大、首次构建慢 | 可接受；无访问时休眠属正常 |
| 依赖从 requirements.txt 安装 | 需含全部运行依赖 | ✅ 已包含（含 psycopg 支持外部数据库） |

## 二、部署步骤（约 5 分钟）

1. 打开 https://share.streamlit.io ，用 GitHub 账号登录（仓库 kenton1215/ClauseGuard 已就绪）；
2. 点击 **New app** → 选择仓库 `kenton1215/ClauseGuard` / 分支 `main` / 主文件 **`app.py`**；
3. 点击 **Advanced settings** → Python 版本选 **3.12**（默认即可）；
4. 先点 **Deploy**（此时用临时 SQLite，可先验证应用跑通）；
5. 再配置 Secrets（见下节），重启应用。

## 三、Secrets 配置（App Settings → Secrets）

```toml
# LLM 大模型（系统默认 Key，页面显示掩码；不配则自动降级纯规则引擎）
DEEPSEEK_API_KEY = "sk-xxxxxxxxxxxxxxxx"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 外部数据库（PostgreSQL 连接串，用于持久化，见下节）
DATABASE_URL = "postgresql://user:password@host:5432/dbname"
```

保存后应用自动重启生效。也可在应用内「设置 → LLM 接口配置」为每个账号配置自有 Key（加密存储）。

## 四、数据库方案（重要）

### 方案 A：免费托管 PostgreSQL（推荐，数据持久化）

1. 注册免费 Postgres（任选其一，均提供免费额度）：
   - **Supabase**：https://supabase.com → 新建项目 → Settings → Database → Connection string（URI）
   - **Neon**：https://neon.tech → 新建项目 → 复制连接串
2. 把连接串填入 Secrets 的 `DATABASE_URL`（注意用 `postgresql://` 开头）；
3. 重启应用 → 首次运行自动建表（users/reviews/findings/audit_log/paid_unlocks），
   用户注册、审核历史、审计日志全部持久化，多实例共享。

### 方案 B：临时 SQLite（演示模式，不配 DATABASE_URL）

不配置 `DATABASE_URL` 时自动使用本地 SQLite：
- ✅ 应用功能完整可用，适合演示与体验；
- ⚠️ **容器重启/空闲回收后数据清空**（用户账号与审核记录丢失）；
- ⚠️ 用户自有 LLM Key 的加密密钥 `secret.key` 同样丢失，重启后需重新保存 Key。

### 数据库表结构

系统首次启动自动建表，无需手工操作：`users`（用户/额度/LLM配置）、`reviews`（审核记录）、
`findings`（风险批注）、`audit_log`（审计日志）、`paid_unlocks`（付费解锁流水）。

## 五、常见问题

| 问题 | 处理 |
| --- | --- |
| 首次部署构建慢 | rapidocr-onnxruntime 依赖较大（约 200MB），耐心等待 5-10 分钟 |
| 应用无访问自动休眠 | 正常现象，下次访问自动唤醒（约 30 秒）；如需保持在线需付费方案 |
| 上传扫描件审核慢 | OCR 在 CPU 容器上较慢，扫描件建议先本地转换或耐心等待 |
| 中文乱码 | 字体已内置，若升级依赖导致异常，检查 assets/fonts/wqy-microhei.ttc 是否随仓库部署 |
| 提示"免费额度已用尽" | 在应用内「设置」切换为自有 API Key 模式即可免费开放使用 |

# 把 GameTrans 发布到 GitHub（首次发布完整教程）

> 面向第一次用 GitHub 的情况，从注册到推送、再到日常更新，逐步来。
> 本地仓库已经初始化完成（分支 `main`，首次提交 `3721a39`），你只差"创建远程仓库 + 推送"。

---

## 0. 准备

**已完成的（无需操作）**
- 本地已是 git 仓库，分支 `main`，首次提交已完成
- `.gitignore` 已排除：虚拟环境（`.venv*`）、`__pycache__`、模型、配置、缓存、日志、临时脚本
- git 身份已配置（`git config --global user.name / user.email`）

**还需要**
1. 一个 GitHub 账号：https://github.com/signup
2. 本地能联网访问 GitHub（国内网络可能需要代理，见 §6）

---

## 1. 在 GitHub 上创建仓库

1. 打开 https://github.com/new
2. **Repository name**：`GameTrans`（名字随意，全站唯一）
3. **Description**（可选）：`游戏实时屏幕翻译助手（Windows）`
4. **Public / Private**：
   - 建议先选 **Private**：先自己确认内容没问题，随时可改成 Public
   - 想直接开源就选 **Public**
5. ⚠️ **下面三个选项都不要勾选**：
   - ❌ Add a README file
   - ❌ Add .gitignore
   - ❌ Choose a license
   
   > 原因：本地已经有这三样东西了。勾选会让远程多出一次提交，导致首次推送被拒绝（报 `rejected ... fetch first`）。

6. 点 **Create repository**。创建后页面会显示仓库地址，形如：
   ```
   https://github.com/你的用户名/GameTrans.git
   ```

---

## 2. 关联远程仓库并推送

在项目目录（`d:\Tran\gametrans`）执行：

```bash
git remote add origin https://github.com/你的用户名/GameTrans.git
git push -u origin main
```

- `origin` 是给远程仓库起的名字（惯例叫这个）
- `-u` 表示记住"本地 main ↔ 远程 main"的对应关系，以后再推送直接 `git push` 即可

---

## 3. 身份验证（首次推送会遇到）

推送时 GitHub 要验证你的身份。**不能用账号密码**，三种方式任选：

### 方式 A：浏览器登录（推荐，最省事）
Windows 上 git 自带 **Git Credential Manager**。执行 `git push` 时会弹出窗口：

1. 选择 **Sign in with your browser**
2. 浏览器里点 **Authorize**
3. 回到命令行，推送自动继续

凭据会被安全保存，之后不用再登录。

### 方式 B：Personal Access Token（PAT，弹窗登录失败时用）

1. GitHub 右上角头像 → **Settings**
2. 左侧拉到底 → **Developer settings**
3. **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**
4. Note 填 `GameTrans`，Expiration 选 90 天或 No expiration
5. 勾选 **`repo`**（整个大项）
6. 点 **Generate token**，**立即复制**（离开页面就再也看不到）
7. 推送时：
   - Username：你的 GitHub 用户名
   - Password：**粘贴刚复制的 token**（不是账号密码！）

### 方式 C：SSH 密钥（进阶，一次配置长期免密）

```bash
ssh-keygen -t ed25519 -C "你的邮箱"        # 一路回车
cat ~/.ssh/id_ed25519.pub                  # 复制输出的公钥
```
GitHub → Settings → **SSH and GPG keys** → New SSH key → 粘贴 → 保存。
然后把远程地址换成 SSH：
```bash
git remote set-url origin git@github.com:你的用户名/GameTrans.git
```

---

## 4. 验证发布成功

1. 刷新 GitHub 仓库页面，应该能看到 `gametrans/`、`tests/`、`README.md`、`LICENSE` 等
2. 点进 `README.md`，说明排版正常（GitHub 会自动渲染）
3. 本地确认状态：
   ```bash
   git status          # 应显示 "nothing to commit, working tree clean"
   git log --oneline   # 应看到你的首次提交
   ```

---

## 5. 后续日常更新（改完代码怎么发）

```bash
cd d:\Tran\gametrans
git status                      # 看改了哪些文件
git add -A                      # 暂存全部改动
git commit -m "fix: 修复某某问题"   # 提交
git push                        # 推送到 GitHub
```

**提交信息约定**（可选，但推荐，看起来专业）：
- `feat:` 新功能　`fix:` 修 bug　`docs:` 文档　`perf:` 性能　`refactor:` 重构　`test:` 测试
- 例：`fix: 提示条不再跟随控制条跑到画面中间`

---

## 6. 常见问题

| 报错 / 现象 | 原因与解决 |
|---|---|
| `remote origin already exists` | 已经加过远程了。改地址：`git remote set-url origin 新地址` |
| `rejected ... fetch first` | 远程有本地没有的提交（多是建仓库时勾了 README）。执行 `git pull --rebase origin main` 后再 `git push` |
| `Authentication failed` | 密码栏要填 **PAT**，不是账号密码；或删掉旧凭据（Windows：控制面板 → 凭据管理器 → 删除 github.com）|
| 推送卡住 / 超时 | 网络问题。可配代理：`git config --global http.proxy http://127.0.0.1:端口`（用完记得 `git config --global --unset http.proxy`）|
| 提示某个文件太大 | 本仓库已排除模型/虚拟环境；若自己新增了大文件，加进 `.gitignore` 并 `git rm --cached 文件名` |
| 想让仓库变公开 | 仓库 → Settings → 最下方 Danger Zone → Change visibility → Public |

---

## 7. 安全须知（重要）

1. **API Key 绝不入库**
   - 你的 DeepSeek Key 存在 `D:\GameTrans\config.json`，**不在仓库里**（`.gitignore` 已隔离）
   - 提交前可自查：`git status` 里如果出现 `config.json`、`*.key`、`.env`，立刻停下
   - 万一误提交：**先去平台把 Key 作废**（改了才安全），再用 `git filter-repo` 清理历史

2. **模型文件不进仓库**
   - 本地翻译模型约 2.3 GB，放在 `D:\GameTrans\models`，与仓库无关
   - 别人克隆后按 README 执行 `python -m gametrans.tools.download_models` 自行下载

3. **数据目录**
   - 默认 `D:\GameTrans`（无 D 盘会自动回退 `%APPDATA%\GameTrans`），可用环境变量 `GAMETRANS_HOME` 改

4. **模型授权**
   - 本项目代码是 MIT，但默认模型 NLLB-200 是 **CC-BY-NC-4.0（禁止商用）**
   - 商用请改用 API 引擎或更换可商用的翻译模型（README 已注明）

---

## 8. 可选：让仓库更完整

- **Topics（标签）**：仓库页右上齿轮 → 添加 `python`、`translation`、`ocr`、`overlay`、`game`
- **Release（发布版）**：右侧 Releases → Draft a new release → 打 tag `v0.1.0`，可附打包好的 exe（后续打包完成再做）
- **About 简介**：仓库页右侧 About 里填描述和主页链接
- **LICENSE 识别**：仓库页会显示 "MIT license" 徽章（已包含 LICENSE 文件）

---

## 9. 图形化替代方案

如果不想用命令行，可装 [GitHub Desktop](https://desktop.github.com/)：
1. 安装后用 GitHub 账号登录
2. `File → Add local repository` → 选择 `d:\Tran\gametrans`
3. 右上角 **Publish repository** → 取消勾选 "Keep this code private"（若要公开）→ Publish Repository
4. 以后改动会在左侧列出，填一句说明点 **Commit to main**，再点 **Push origin** 即可
